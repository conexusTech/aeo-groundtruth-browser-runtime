"""The wire contract between aeo-agent-service and this runtime.

Both halves of this contract are defined in the consumer, not here:
`aeo-agent-service/app/adapters/sov/_agentcore.py` builds the request and normalizes
the response. **The field names below must match that module exactly.** A rename on
either side does not raise — it produces `answer_text=""`, which the consumer scores
as "the AI never mentioned this business", indistinguishable from a genuine
visibility loss. That is the single most expensive way to be wrong here, so the names
are duplicated deliberately rather than shared via a package: a shared package would
couple deploy cycles, and this list is short enough to keep in step by hand.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class BasicAuthRef(BaseModel):
    """Where the proxy credentials live. Never the credentials themselves."""

    secret_arn: str


class ProxySpec(BaseModel):
    """An external proxy for the browser session.

    No password field exists, and that is the point. AgentCore Browser accepts
    `externalProxy.credentials.basicAuth.secretArn` and reads Secrets Manager itself;
    an inline credential is not expressible in the API. The geo targeting that makes
    Tier 3 meaningful is encoded in the Bright Data *username*, which lives inside
    that secret and never transits this service.
    """

    server: str
    port: int
    secret_arn: str | None = None


class Selectors(BaseModel):
    """CSS selectors for one consumer surface, supplied per invocation.

    Data, not configuration: this runtime is generic and drives whatever it is told to
    drive. The selectors and their `SELECTORS_VERIFIED` safety flag stay in
    aeo-agent-service, where the test enforcing them already lives. Keeping them here
    would put a safety gate in a different repo from the code it protects.
    """

    input: str
    submit: str | None = None
    answer: str
    streaming: str | None = None
    consent: list[str] = Field(default_factory=list)
    login_wall: list[str] = Field(default_factory=list)
    challenge: list[str] = Field(default_factory=list)
    citation: list[str] = Field(default_factory=list)


class InvocationRequest(BaseModel):
    prompt: str
    surface: str = "unknown"
    url: str
    selectors: Selectors
    proxy: ProxySpec | None = None
    #: The geography we INTEND to egress from, echoed for diagnosis. Never used for
    #: targeting — targeting is entirely inside the secret's username. The consumer
    #: compares this against `observed_egress` and fails the job on a mismatch.
    proxy_target: str | None = None
    #: Dump what each selector class really matches, and do not mutate the page.
    #: Operator-only: aeo-agent-service never sets it. Discovery runs deliberately broad
    #: selectors, so a production run with this on would report a page inventory instead
    #: of measuring anything — and would skip the consent click that real runs need.
    discover: bool = False
    #: Overall budget, and it must sit under BOTH the consumer's
    #: AGENTCORE_SESSION_TIMEOUT_SECONDS and AgentCore's own synchronous-invocation
    #: ceiling. An abandoned invocation tells the operator nothing about WHY, and on this
    #: path every attempt is already paid for.
    #:
    #: **Lowered from 165 on 2026-08-03 because 165 does not come back.** Measured twice: a
    #: run with this at 165 completed server-side in 164s, logged cleanly, stopped its
    #: browser session — and the client received NOTHING, 74 seconds before its own 240s
    #: read timeout expired. The same invocation with 75 returned an envelope in 83s. So
    #: `InvokeAgentRuntime` drops a long *synchronous* response somewhere between 83s and
    #: 164s.
    #:
    #: The exact ceiling is UNMEASURED: `servicequotas:ListServiceQuotas` and
    #: `ce:GetCostAndUsage` are both denied to this account, and AWS's quota page could not
    #: be read. 90 is chosen inside the proven-good range rather than just below a guessed
    #: quota. If a real proxied run needs longer than this, the answer is **async
    #: invocation with /ping polling**, not a bigger number here — that is the pattern the
    #: earlier AgentCore scan work landed on for exactly this reason.
    timeout_seconds: float = 90.0


class Citation(BaseModel):
    url: str
    title: str | None = None


class ObservedEgress(BaseModel):
    """Where the session actually egressed from, measured from inside the browser.

    The most important field in this file. AWS documents the browser proxy as "a
    browser-level setting ... not a network-level control" that "does not guarantee
    that all traffic will transit the proxy", and does not validate proxy
    connectivity at session creation — it is fail-OPEN. So a misconfigured proxy
    produces no error: the browser egresses from AWS, the surface returns a perfectly
    good answer about the wrong metro, and the consumer stamps it
    `location_method='ground_truth'`. Nothing downstream can detect that. This field
    is what converts it into a hard failure.
    """

    city: str | None = None
    region: str | None = None
    ip: str | None = None
    #: Which IP-geolocation provider answered, so a disagreement between providers is
    #: distinguishable from a genuinely wrong exit.
    source: str | None = None
    #: Where the provider places that IP. **Added 2026-08-06, and the consumer prefers
    #: these over `city` when both sides have them.**
    #:
    #: A city NAME comparison false-fails a correct session: a residential exit in
    #: Franklin, TN was reported as "Nashville" by ipinfo (the metro) while ip-api and
    #: Bright Data's own view both said Franklin. `geo_egress_mismatch` is terminal and
    #: deliberately not retried, so that discards a correctly-targeted PAID session. It
    #: was ~1 in 7 exits in sampling — intermittent, which on a quarterly job reads as a
    #: flaky proxy rather than a units problem.
    #:
    #: Distance has no such failure mode: a suburb reported as its metro centre is a few
    #: km away, while the case the check exists for — Franklin KY, or Franklin OH — is
    #: hundreds. Populating these is therefore not a diagnostic nicety; it is what lets
    #: the consumer stop comparing strings. If a provider omits them the consumer falls
    #: back to the name comparison, so this stays optional.
    lat: float | None = None
    lon: float | None = None


class InvocationResponse(BaseModel):
    answer_text: str = ""
    citations: list[Citation] = Field(default_factory=list)
    #: Page-state flags. Both are TERMINAL for the consumer: retrying a login wall
    #: spends another paid session against the same wall, and on a residential exit
    #: repeated challenges raise the chance the IP gets burned for the next tenant.
    login_wall: bool = False
    challenge: bool = False
    observed_egress: ObservedEgress | None = None
    #: Diagnostic only. The consumer classifies on `answer_text` and the two flags, so
    #: this never changes its decision — it exists so a human reading CloudWatch can
    #: tell "the selector missed" from "the surface was slow" without another session.
    error: str | None = None
    #: How the prompt actually got typed and submitted, and how completion was
    #: detected. Written for the live selector pass: when an answer comes back empty,
    #: this says which step silently no-opped. Carries `step`, the phase that was in
    #: progress, so a failure envelope names its own location.
    trace: dict[str, Any] = Field(default_factory=dict)
    #: Only populated when the request asked to `discover`. A per-selector-class
    #: inventory of what actually matched and which of those were visible — the pairing
    #: that distinguishes "the selector is wrong" from "the selector points at a node
    #: that is never visible", which a Playwright timeout does not.
    discovery: dict[str, Any] | None = None
