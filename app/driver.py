"""Drive one consumer web UI through an AgentCore browser session.

Read the four rules before changing anything here. Each one exists because breaking
it produces a plausible number rather than an error, and this runtime feeds the tier
the product presents to customers as the trustworthy one.

1. **The egress self-check must run from inside the BROWSER PAGE.** Measuring it with
   `httpx` or `boto3` from this container would report the container's own AWS egress
   and therefore always agree with itself — a check that can never fail, which is
   worse than no check because it looks like assurance. The proxy is a Chromium
   `--proxy-server` flag; only the page sees it.
1b. **Whatever the egress check established must survive a later failure.** See
   `_DriveState`: a null `observed_egress` is not "unknown" to the consumer, it is a
   terminal geography mismatch, so discarding a successful reading when a *selector*
   fails reports the wrong fault entirely.
2. **`stop_browser_session` runs in a `finally`, always.** The consumer's abort can
   only cancel the *runtime invocation*; the browser session is created here, so this
   is the only place that can release it. A leaked session is a paid browser left
   running at 10-50x an API call.
3. **Completion is the streaming indicator DISAPPEARING, not a sleep.** A fixed wait
   either truncates a long answer — and a partial answer scores as a complete one —
   or burns paid session time on every short one.
4. **Never report an empty answer as a success with no explanation.** The consumer
   retries an unexplained empty answer, which buys a second paid session against the
   same wall. If the page was blocked, say which; otherwise put the reason in `error`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from bedrock_agentcore.tools.browser_client import BrowserClient
from bedrock_agentcore.tools.config import (
    BasicAuth,
    ExternalProxy,
    ProxyConfiguration,
    ProxyCredentials,
)
from playwright.async_api import Locator, Page, async_playwright

from app.models import (
    Citation,
    InvocationRequest,
    InvocationResponse,
    ObservedEgress,
    Selectors,
)

logger = logging.getLogger(__name__)

#: IP-geolocation endpoints, tried in order. Two providers rather than one because a
#: single provider cannot distinguish "the proxy exited in the wrong city" from "this
#: provider's IP database is stale for this range", and those have completely
#: different fixes. `source` records which answered.
_EGRESS_PROVIDERS: tuple[tuple[str, str, tuple[str, str, str]], ...] = (
    ("ipinfo.io", "https://ipinfo.io/json", ("city", "region", "ip")),
    ("ip-api.com", "http://ip-api.com/json", ("city", "regionName", "query")),
)

#: Hard ceiling on the browser session itself, independent of our own deadline. The
#: SDK's default is 3600s. If this process dies between `start` and `stop` — OOM, a
#: SIGKILL, a bug in the `finally` — this is the only thing that stops the meter, so
#: it is deliberately just above our own budget rather than generous.
_SESSION_TIMEOUT_SECONDS = 300

_NAV_TIMEOUT_MS = 45_000
_EGRESS_TIMEOUT_MS = 20_000
_CONSENT_TIMEOUT_MS = 3_000
_STREAM_APPEAR_TIMEOUT_MS = 15_000
#: Deliberately shorter than navigation: a composer that has not mounted 30s after
#: `domcontentloaded` is not going to, and the remaining budget is better spent
#: reporting that than waiting on it.
_INPUT_TIMEOUT_MS = 30_000
#: Per selector class in a discovery dump. Enough to see the shape of the page without
#: turning `a[href^='http']` into a megabyte of envelope.
_DISCOVERY_SAMPLE_LIMIT = 25


class DeadlineExceeded(Exception):
    """Our own budget ran out. Distinct from a Playwright timeout on one step."""


class _DriveState:
    """Diagnostics accumulated as the drive progresses, readable after it RAISES.

    `_drive` used to hold `trace` and `egress` as locals, so any exception unwound both
    and the outer handler answered with `trace={}` and `observed_egress=None`. That is
    not merely a lost diagnostic. **The consumer treats a null `observed_egress` as a
    geography mismatch and fails the job terminally as `geo_egress_mismatch`** — so a
    selector that had stopped matching was reported as a proxy exiting in the wrong
    city. Wrong fault, wrong owner, on a quarterly path where the next look is three
    months away. The first live invocation failed exactly this way and its envelope
    pointed at the network.

    Owning the diagnostics here is what lets the outer handler answer with everything
    that *was* established before the failure.
    """

    def __init__(self, surface: str) -> None:
        self.trace: dict[str, Any] = {"surface": surface, "step": "starting"}
        self.egress: ObservedEgress | None = None
        self.discovery: dict[str, Any] | None = None

    def step(self, name: str) -> None:
        """Name the step about to be attempted, so a raise reports its own location.

        A Playwright timeout says which selector it was waiting on but not which phase
        asked for it, and the same selector is consulted from several phases.
        """
        self.trace["step"] = name


class _Deadline:
    def __init__(self, seconds: float) -> None:
        self._end = time.monotonic() + seconds

    def remaining_ms(self, cap_ms: int) -> int:
        left = (self._end - time.monotonic()) * 1000
        if left <= 0:
            raise DeadlineExceeded("invocation budget exhausted")
        return int(min(cap_ms, left))

    @property
    def expired(self) -> bool:
        return time.monotonic() >= self._end


def build_proxy_configuration(req: InvocationRequest) -> ProxyConfiguration | None:
    """Translate the request's proxy spec into the SDK's typed configuration.

    Returns None when no proxy was requested. That is a legitimate mode — it is how
    the surface can be driven for DOM/selector discovery before Bright Data exists —
    but it egresses from AWS, so the consumer's egress comparison will reject the
    result. That rejection is the check working, not a defect.

    **No `bypass` patterns are set.** The AWS docs suggest bypassing `.amazonaws.com`
    for latency, and that would be safe today, but a bypass list is one edit away from
    covering the IP-geolocation host — at which point the egress self-check would
    measure this container's own egress and silently pass forever. The latency saving
    is not worth putting that edit within reach.
    """
    if req.proxy is None:
        return None
    credentials = None
    if req.proxy.secret_arn:
        credentials = ProxyCredentials(basic_auth=BasicAuth(secret_arn=req.proxy.secret_arn))
    return ProxyConfiguration(
        proxies=[
            ExternalProxy(
                server=req.proxy.server,
                port=req.proxy.port,
                credentials=credentials,
            )
        ]
    )


async def _read_egress(page: Page, deadline: _Deadline) -> ObservedEgress | None:
    """Ask an IP-geolocation endpoint, from inside the session, where we are.

    Returns None only when every provider failed, which the consumer treats as a
    mismatch — unverifiable geography is not a result.
    """
    for name, url, (city_key, region_key, ip_key) in _EGRESS_PROVIDERS:
        try:
            response = await page.goto(
                url,
                timeout=deadline.remaining_ms(_EGRESS_TIMEOUT_MS),
                wait_until="domcontentloaded",
            )
            if response is None or not response.ok:
                logger.warning("egress provider %s returned no usable response", name)
                continue
            body = await response.text()
            payload = json.loads(body)
        except DeadlineExceeded:
            raise
        except Exception as exc:  # noqa: BLE001 - a provider failing is expected
            # A proxy that is unreachable renders a browser error page rather than
            # failing the navigation, so a parse error here is a likely symptom of
            # exactly the misconfiguration we are testing for. Try the next provider
            # before concluding anything.
            logger.warning("egress provider %s failed: %s", name, exc)
            continue
        egress = ObservedEgress(
            city=payload.get(city_key),
            region=payload.get(region_key),
            ip=payload.get(ip_key),
            source=name,
        )
        if egress.city:
            return egress
        logger.warning("egress provider %s answered without a city", name)
    return None


async def _dismiss_consent(
    page: Page, selectors: list[str], trace: dict, *, discover: bool
) -> None:
    """Click away cookie/consent walls.

    On a residential exit in a new geography these are the norm rather than an edge
    case, which is why failing to find one is not an error: absent is the common case.

    **In discovery mode it reports and clicks nothing.** Discovery runs deliberately
    broad selectors — `["button"]` for this class — and the old code clicked the first
    visible match of each. On a real page that is as likely to be "Log in" as "Accept",
    so the very run whose job is to observe the page's default state was mutating it,
    and could manufacture the login wall it then reported.
    """
    if discover:
        trace["consent_skipped_for_discovery"] = selectors
        return
    dismissed = []
    for selector in selectors:
        try:
            # Visible-filtered, then clicked directly: `click` already auto-waits for
            # visible-and-enabled, so the separate `is_visible` probe was a round trip
            # that also reintroduced `.first`.
            await page.locator(selector).filter(visible=True).first.click(
                timeout=_CONSENT_TIMEOUT_MS
            )
            dismissed.append(selector)
        except Exception:  # noqa: BLE001 - absence is the normal case
            continue
    trace["consent_dismissed"] = dismissed


async def _safe_count(locator: Locator) -> int | None:
    """Count matches without letting the count itself become the reported failure."""
    try:
        return await locator.count()
    except Exception:  # noqa: BLE001
        return None


async def _wait_visible(
    page: Page, selector: str, timeout_ms: int, trace: dict, label: str
) -> Locator:
    """Resolve `selector` to its first VISIBLE match, not to its first match.

    This is the defect that cost the first live session. ChatGPT renders a hidden
    fallback `<textarea name="prompt-textarea">` alongside the contenteditable composer
    it actually uses, and the fallback comes first in document order. So `.first`
    resolved to a node that can never satisfy `wait_for(state="visible")`, and the wait
    burned its whole timeout re-resolving to the same hidden element 86 times — while a
    perfectly good composer sat on the page.

    It is the same mistake as taking `.first` in `_read_answer`, at the other end of the
    list: **positional selection over a multi-match selector is wrong in both
    directions, and visibility is the filter.** `filter(visible=True)` re-evaluates on
    every auto-wait retry, so this still waits for a composer that mounts late.
    """
    all_matches = page.locator(selector)
    first_visible = all_matches.filter(visible=True).first
    try:
        await first_visible.wait_for(state="visible", timeout=timeout_ms)
    except Exception:
        # Recorded on the failure path too, because "42 matched, none visible" and
        # "nothing matched at all" need completely different fixes and a bare Playwright
        # timeout message does not distinguish them.
        trace[f"{label}_matched"] = await _safe_count(all_matches)
        trace[f"{label}_visible"] = 0
        raise
    trace[f"{label}_matched"] = await _safe_count(all_matches)
    trace[f"{label}_visible"] = await _safe_count(all_matches.filter(visible=True))
    return first_visible


async def _first_visible(page: Page, selectors: list[str]) -> str | None:
    """Return the first selector in the list with at least one VISIBLE match.

    Also `.first`-free, and here the old behaviour failed in the more expensive
    direction: a login wall whose first DOM match happened to be hidden read as "no
    wall", so the run reported an empty answer instead. The consumer retries an empty
    answer and stops on a wall — meaning the bug bought a second paid session against
    the same wall every time.
    """
    for selector in selectors:
        try:
            if await page.locator(selector).filter(visible=True).count() > 0:
                return selector
        except Exception:  # noqa: BLE001
            continue
    return None


async def _read_field_text(field: Locator) -> str:
    """Read back whatever is in the composer, whichever kind of node it is.

    `input_value` is the right reader for a `textarea`/`input` and raises on anything
    else; a contenteditable div holds its text as content. Try both rather than
    branching on the tag, because which one the surface uses is exactly the thing we do
    not want to hard-code per surface.
    """
    try:
        return (await field.input_value(timeout=2_000)).strip()
    except Exception:  # noqa: BLE001
        try:
            return (await field.inner_text(timeout=2_000)).strip()
        except Exception:  # noqa: BLE001
            return ""


async def _enter_prompt(
    page: Page, sel: Selectors, prompt: str, deadline: _Deadline, trace: dict
) -> None:
    """Type the prompt and submit it.

    Two fallbacks, both load-bearing across these surfaces. `fill` works on a
    `textarea`/`input` but silently does nothing useful on the contenteditable div
    ChatGPT uses, so a click-then-type path is required; and typing (rather than
    inserting) is what emits the key events an SPA needs before it will enable its own
    send button. Submitting via Enter is the fallback because a send button is often
    disabled until those events fire.
    """
    field = await _wait_visible(
        page, sel.input, deadline.remaining_ms(_INPUT_TIMEOUT_MS), trace, "input"
    )
    try:
        await field.fill(prompt, timeout=10_000)
        trace["input_method"] = "fill"
    except Exception:  # noqa: BLE001
        await field.click(timeout=10_000)
        await page.keyboard.type(prompt, delay=5)
        trace["input_method"] = "click+type"

    # Read back before submitting. `fill` reports success against a React-controlled
    # node whose next render reverts it, and an empty composer does not error: the
    # surface answers whatever the page was already showing, or answers nothing, and
    # either way the result is filed as a measured answer to OUR prompt. Retyping is
    # cheap; a confident answer to a question we never asked is not detectable
    # downstream at all.
    if prompt[:40] not in await _read_field_text(field):
        trace["input_readback"] = f"lost_after_{trace['input_method']}"
        await field.click(timeout=10_000)
        # Select-all first: a partial fill leaves text behind, and typing after it
        # would submit the prompt twice over.
        await page.keyboard.press("ControlOrMeta+a")
        await page.keyboard.type(prompt, delay=5)
        trace["input_method"] += "+retyped"
        trace["input_readback_2"] = (
            "ok" if prompt[:40] in await _read_field_text(field) else "still_lost"
        )
    else:
        trace["input_readback"] = "ok"

    submitted = False
    if sel.submit:
        try:
            button = page.locator(sel.submit).filter(visible=True).first
            if await button.is_enabled(timeout=5_000):
                await button.click(timeout=5_000)
                submitted = True
                trace["submit_method"] = "button"
        except Exception:  # noqa: BLE001
            submitted = False
    if not submitted:
        await page.keyboard.press("Enter")
        trace["submit_method"] = "enter"


async def _await_completion(
    page: Page, sel: Selectors, deadline: _Deadline, trace: dict
) -> None:
    """Wait for the answer to finish rendering.

    The order matters. We wait for the streaming indicator to APPEAR first, then to
    detach. Waiting only for it to disappear would return instantly when it has not
    yet appeared — reading a half-rendered or empty answer and scoring it as complete.
    When it never appears at all (a cached answer, or a changed DOM) we fall back to
    waiting for the answer text to stop growing, which is weaker but honest, and
    `trace` records which path was taken so the selector pass can tell them apart.

    Both waits are visible-filtered. The failure that buys is quieter than the input
    one: a hidden first match makes the APPEAR wait time out, which does not error — it
    silently demotes the run to the text-stability fallback. And stability is the path
    that can truncate, because a surface that pauses ~4.5s mid-answer reads as finished,
    and a truncated answer scores as a complete one. So the filter is what keeps the
    precise signal rather than quietly trading it for the lossy one.

    (Note for anyone re-deriving this: the disappear wait canNOT be satisfied
    immediately by a permanently-hidden `.first`, which is what an earlier version of
    this comment claimed. The appear wait guards it — reaching `state="hidden"` at all
    means something was visible. A mutation test caught the wrong reasoning.)
    """
    if sel.streaming:
        streaming = page.locator(sel.streaming).filter(visible=True).first
        try:
            await streaming.wait_for(
                state="visible",
                timeout=deadline.remaining_ms(_STREAM_APPEAR_TIMEOUT_MS),
            )
            trace["stream_appeared"] = True
            await streaming.wait_for(
                state="hidden", timeout=deadline.remaining_ms(120_000)
            )
            trace["completion"] = "streaming_selector_hidden"
            return
        except DeadlineExceeded:
            raise
        except Exception:  # noqa: BLE001
            trace["stream_appeared"] = trace.get("stream_appeared", False)

    # Stability fallback: the answer is done when its length stops changing.
    trace["completion"] = "text_stabilized"
    previous = -1
    stable_rounds = 0
    while stable_rounds < 3:
        if deadline.expired:
            trace["completion"] = "text_still_growing_at_deadline"
            return
        await asyncio.sleep(1.5)
        current = len(await _read_answer(page, sel))
        if current == previous and current > 0:
            stable_rounds += 1
        else:
            stable_rounds = 0
        previous = current


#: Runs in the page. Reports what a selector ACTUALLY matches, including the elements
#: Playwright would have skipped, because "matched but hidden" is the signature we were
#: blind to and it is invisible in a timeout message.
#:
#: Deliberately `querySelectorAll` and not a Playwright locator: this must report the
#: raw DOM truth without any of the auto-waiting or filtering whose behaviour is the
#: thing under investigation. The cost is that Playwright-only engines
#: (`:has-text()`, `visible=`) are not valid here — those come back as `unsupported`,
#: which is itself worth knowing before one is put in a production selector.
_DISCOVERY_JS = """
([selector, limit]) => {
  let nodes;
  try { nodes = document.querySelectorAll(selector); }
  catch (e) { return { unsupported: String(e && e.message || e) }; }
  const attrs = ['name', 'placeholder', 'aria-label', 'data-testid', 'role', 'href',
                 'contenteditable', 'type', 'data-message-author-role'];
  const sample = [];
  let visibleCount = 0;
  for (const el of nodes) {
    const cs = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    const visible = r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' &&
                    cs.display !== 'none' && cs.opacity !== '0';
    if (visible) visibleCount++;
    if (sample.length >= limit) continue;
    const found = {};
    for (const a of attrs) {
      const v = el.getAttribute(a);
      if (v !== null && v !== '') found[a] = v.slice(0, 80);
    }
    sample.push({
      tag: el.tagName.toLowerCase(),
      id: el.id || undefined,
      cls: (typeof el.className === 'string' && el.className)
             ? el.className.slice(0, 120) : undefined,
      attrs: Object.keys(found).length ? found : undefined,
      visible: visible,
      box: visible ? Math.round(r.width) + 'x' + Math.round(r.height) : undefined,
      text: (el.innerText || '').trim().slice(0, 100) || undefined,
    });
  }
  return { matched: nodes.length, visible: visibleCount, sample: sample };
}
"""


async def _discover(page: Page, sel: Selectors, phase: str) -> dict[str, Any]:
    """Dump what each selector class really matches on the page as it stands now.

    Exists because `--discover` previously only swapped in broader selectors and then
    drove the page normally — so when the very first step timed out, a paid session
    returned nothing about the page at all. The one fact we did learn came from an
    element that happened to be quoted in a Playwright error string. A discovery run
    has to answer independently of whether the drive succeeds.
    """
    classes: dict[str, list[str]] = {
        "input": [sel.input],
        "submit": [sel.submit] if sel.submit else [],
        "answer": [sel.answer],
        "streaming": [sel.streaming] if sel.streaming else [],
        "consent": sel.consent,
        "login_wall": sel.login_wall,
        "challenge": sel.challenge,
        "citation": sel.citation,
    }
    out: dict[str, Any] = {"phase": phase, "url": page.url, "title": None}
    try:
        out["title"] = await page.title()
    except Exception:  # noqa: BLE001
        pass
    for name, selectors in classes.items():
        results = {}
        for selector in selectors:
            try:
                results[selector] = await page.evaluate(
                    _DISCOVERY_JS, [selector, _DISCOVERY_SAMPLE_LIMIT]
                )
            except Exception as exc:  # noqa: BLE001 - a dump must never fail the run
                results[selector] = {"error": f"{type(exc).__name__}: {exc}"}
        out[name] = results
    return out


async def _read_answer(page: Page, sel: Selectors) -> str:
    """Read the answer text.

    Takes the LAST match, not the first. The answer selector matches every assistant
    turn on the page, so `.first` would return a previous message — on a fresh session
    usually a welcome or example prompt, which is a real string and would be scored as
    the answer.
    """
    try:
        nodes = page.locator(sel.answer)
        count = await nodes.count()
        if count == 0:
            return ""
        return (await nodes.nth(count - 1).inner_text()).strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("reading the answer failed: %s", exc)
        return ""


async def _read_citations(page: Page, sel: Selectors) -> list[Citation]:
    found: dict[str, Citation] = {}
    for selector in sel.citation:
        try:
            links = page.locator(selector)
            for i in range(await links.count()):
                node = links.nth(i)
                href = await node.get_attribute("href")
                if not href or href.startswith("javascript:"):
                    continue
                title = (await node.inner_text() or "").strip() or None
                # De-duplicated on URL: these surfaces render the same source both
                # inline and in a footer list, and counting it twice would inflate
                # every citation-frequency metric downstream.
                found.setdefault(href, Citation(url=href, title=title))
        except Exception as exc:  # noqa: BLE001
            logger.warning("citation selector %r failed: %s", selector, exc)
    return list(found.values())


async def _drive(
    page: Page, req: InvocationRequest, deadline: _Deadline, state: _DriveState
) -> InvocationResponse:
    trace = state.trace
    sel = req.selectors

    # FIRST, before the target surface is ever loaded. If the session is egressing
    # from the wrong place, nothing observed afterwards is worth having, and finding
    # out before we spend the remaining budget is free.
    state.step("egress")
    state.egress = egress = await _read_egress(page, deadline)
    trace["egress_source"] = egress.source if egress else None

    state.step("navigate")
    await page.goto(
        req.url, timeout=deadline.remaining_ms(_NAV_TIMEOUT_MS), wait_until="domcontentloaded"
    )
    state.step("consent")
    await _dismiss_consent(page, sel.consent, trace, discover=req.discover)

    if req.discover:
        # Before the login/challenge checks and before anything is typed, so this is the
        # page in its default state. It is also the dump that survives if the very next
        # step raises, which is what happened on the first live invocation.
        state.step("discover_on_load")
        state.discovery = {"on_load": await _discover(page, sel, "on_load")}

    state.step("login_wall_check")
    blocked_by = await _first_visible(page, sel.login_wall)
    if blocked_by:
        trace["login_wall_selector"] = blocked_by
        return InvocationResponse(
            login_wall=True, observed_egress=egress, trace=trace,
            discovery=state.discovery,
            error=f"login wall matched {blocked_by!r}",
        )
    state.step("challenge_check")
    challenged_by = await _first_visible(page, sel.challenge)
    if challenged_by:
        trace["challenge_selector"] = challenged_by
        return InvocationResponse(
            challenge=True, observed_egress=egress, trace=trace,
            discovery=state.discovery,
            error=f"challenge matched {challenged_by!r}",
        )

    state.step("enter_prompt")
    await _enter_prompt(page, sel, req.prompt, deadline, trace)
    state.step("await_completion")
    await _await_completion(page, sel, deadline, trace)

    state.step("read_answer")
    answer = await _read_answer(page, sel)
    citations = await _read_citations(page, sel)

    if req.discover:
        # The second dump is where the answer / streaming / citation classes become
        # readable: none of them exist on the page before a prompt has been answered.
        state.step("discover_after_answer")
        state.discovery = dict(state.discovery or {})
        state.discovery["after_answer"] = await _discover(page, sel, "after_answer")

    # A wall can appear AFTER the prompt is submitted — several surfaces allow one
    # anonymous question and then demand an account. Re-checking here is what stops
    # that being reported as "the AI answered with nothing".
    if not answer:
        state.step("post_answer_wall_check")
        blocked_by = await _first_visible(page, sel.login_wall)
        if blocked_by:
            trace["login_wall_selector"] = blocked_by
            return InvocationResponse(
                login_wall=True, observed_egress=egress, trace=trace,
                discovery=state.discovery,
                error=f"login wall appeared after submitting; matched {blocked_by!r}",
            )
        challenged_by = await _first_visible(page, sel.challenge)
        if challenged_by:
            trace["challenge_selector"] = challenged_by
            return InvocationResponse(
                challenge=True, observed_egress=egress, trace=trace,
                discovery=state.discovery,
                error=f"challenge appeared after submitting; matched {challenged_by!r}",
            )

    state.step("done")
    return InvocationResponse(
        answer_text=answer,
        citations=citations,
        observed_egress=egress,
        trace=trace,
        discovery=state.discovery,
        # Rule 4: an empty answer never leaves here unexplained.
        error=None if answer else "no answer text matched the answer selector",
    )


async def run_invocation(req: InvocationRequest, region: str) -> InvocationResponse:
    """Open a session, drive the surface, and always release the session."""
    deadline = _Deadline(req.timeout_seconds)
    client = BrowserClient(region=region)
    proxy_configuration = build_proxy_configuration(req)
    # Created before the session, so even a failure in `client.start` answers with a
    # trace that names the step rather than with an empty dict.
    state = _DriveState(req.surface)

    # `start` and `stop` are blocking boto3 calls. They run in a worker thread because
    # this coroutine shares its event loop with the `/ping` handler, and AgentCore
    # health-checks that endpoint: a multi-second blocking call here would make a
    # working runtime look unhealthy while it is doing exactly what it was asked to.
    #
    # `start` is INSIDE the try. It used to sit above it, so a failure here escaped
    # `run_invocation` and FastAPI answered 500 — breaking the one invariant this
    # runtime has ("always 200 with an envelope"), on what is also the likeliest failure
    # in production: the execution role can lack `StartBrowserSession`, in which case the
    # runtime deploys cleanly, passes /ping, and only ever fails at invocation. The
    # consumer would have seen an opaque boto3 error with no step and no envelope.
    session_id: str | None = None
    try:
        state.step("start_browser_session")
        session_id = await asyncio.to_thread(
            client.start,
            identifier="aws.browser.v1",
            session_timeout_seconds=_SESSION_TIMEOUT_SECONDS,
            proxy_configuration=proxy_configuration,
        )
        logger.info(
            "browser session %s started (surface=%s, proxied=%s, target=%s)",
            session_id,
            req.surface,
            proxy_configuration is not None,
            req.proxy_target,
        )
        state.step("attach_cdp")
        ws_url, headers = client.generate_ws_headers()
        async with async_playwright() as playwright:
            browser = await playwright.chromium.connect_over_cdp(ws_url, headers=headers)
            try:
                # The session already has a context and a page. Creating our own would
                # open a second browser context that the proxy flag was not applied
                # to at startup.
                context = browser.contexts[0]
                page = context.pages[0] if context.pages else await context.new_page()
                page.set_default_timeout(_NAV_TIMEOUT_MS)
                return await _drive(page, req, deadline, state)
            finally:
                await browser.close()
    except DeadlineExceeded as exc:
        logger.error("invocation exceeded its budget: %s (step=%s)", exc, state.trace.get("step"))
        return InvocationResponse(
            error=f"timeout: {exc}",
            # Rule 1b. Everything already established is reported, above all the egress
            # reading: dropping it turns "we ran out of time" into "the proxy exited in
            # the wrong city", and only one of those is the operator's problem.
            observed_egress=state.egress,
            trace=state.trace,
            discovery=state.discovery,
        )
    except Exception as exc:  # noqa: BLE001 - the envelope IS the error channel
        # Returned rather than raised, so the consumer parses a real envelope. A 500
        # reaches it as an opaque boto3 error with no page state and no observed
        # egress, which is strictly less than this.
        logger.exception("invocation failed at step=%s", state.trace.get("step"))
        return InvocationResponse(
            error=f"{type(exc).__name__}: {exc}",
            observed_egress=state.egress,
            trace=state.trace,
            discovery=state.discovery,
        )
    finally:
        # Rule 2. This is the only place that can release the browser session: the
        # consumer's abort cancels the runtime invocation, which cannot reach in here.
        if session_id is None:
            # `start` never returned, so there is nothing to release and `stop` would
            # raise a second error over the top of the real one — which is how a
            # permissions problem gets reported as a teardown problem.
            logger.info("no browser session was started; nothing to release")
        else:
            try:
                await asyncio.to_thread(client.stop)
                logger.info("browser session %s stopped", session_id)
            except Exception as exc:  # noqa: BLE001
                # Logged loudly rather than raised: raising here would replace the real
                # result with a teardown error, but a leak is money, so it must be
                # greppable. `_SESSION_TIMEOUT_SECONDS` is the backstop.
                logger.error(
                    "FAILED TO STOP browser session %s - it will run until the %ss "
                    "session timeout expires: %s",
                    session_id,
                    _SESSION_TIMEOUT_SECONDS,
                    exc,
                )
