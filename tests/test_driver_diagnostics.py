"""Regression tests for the defects the FIRST live invocation exposed (2026-08-03).

That session cost a paid browser and came back with `trace={}`, `observed_egress=null`
and a 45s timeout on the input selector. Every test here encodes one of the three
findings, and each is written so that reverting the fix makes it fail:

1. The envelope discarded everything `_drive` had established. A null `observed_egress`
   is not "unknown" to aeo-agent-service, it is a terminal `geo_egress_mismatch` — so a
   selector fault was reported as a proxy exiting in the wrong city.
2. `.first` resolved to a hidden node. ChatGPT ships a hidden fallback textarea before
   the real composer, so the wait could never be satisfied while a usable field sat on
   the page.
3. `--discover` only widened the selectors; it did not make the page legible, so a
   failed discovery run reported nothing about the page at all.

The fakes are deliberately dumb. They model exactly two behaviours that matter and that
no unit test previously covered: a selector can match several nodes, and a matched node
can be invisible. What they cannot cover is `_DISCOVERY_JS`, which only a real browser
executes — that is what the live `--discover` run is for.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app import driver
from app.models import InvocationRequest

# --- fakes ----------------------------------------------------------------------


class FakeTimeout(Exception):
    """Stands in for playwright's TimeoutError, which the driver only ever catches."""


class FakeNode:
    def __init__(self, *, visible=True, text="", value=None, enabled=True):
        self.visible = visible
        self.text = text
        self.value = value
        self.enabled = enabled
        self.clicked = False


class _TransientNode(FakeNode):
    """Visible on first inspection, gone afterwards: an indicator that appeared and then
    finished. Models the only sequence `_await_completion`'s fast path can observe."""

    def __init__(self):
        super().__init__(visible=True)
        self._reads = 0

    @property  # type: ignore[override]
    def visible(self):
        self._reads += 1
        return self._reads <= 1

    @visible.setter
    def visible(self, _value):
        pass


class FakeLocator:
    """Resolves LAZILY, like a real Playwright locator.

    The first version applied `filter(visible=True)` eagerly at construction. That is
    both unfaithful — Playwright re-evaluates on every action, which is exactly why
    `filter(visible=True)` can still auto-wait for a composer that mounts late — and it
    broke the one test that needed an element to change state between two waits.
    """

    def __init__(self, nodes, visible_only=False, index=None):
        self._all = list(nodes)
        self._visible_only = visible_only
        self._index = index

    def _resolve(self):
        nodes = [n for n in self._all if n.visible] if self._visible_only else list(self._all)
        if self._index is not None:
            nodes = nodes[self._index : self._index + 1]
        return nodes

    # `type(self)` throughout, not `FakeLocator`: subclasses override a single method to
    # model one misbehaving surface, and hardcoding the base class silently discarded
    # that override the moment `.filter(...).first` was chained - which is every call
    # site in the driver. A test built on it passed for the wrong reason.
    def filter(self, visible=None):
        if visible:
            return type(self)(self._all, visible_only=True, index=self._index)
        return self

    @property
    def first(self):
        return type(self)(self._all, visible_only=self._visible_only, index=0)

    def nth(self, index):
        return type(self)(self._all, visible_only=self._visible_only, index=index)

    async def count(self):
        return len(self._resolve())

    async def wait_for(self, state="visible", timeout=None):
        nodes = self._resolve()
        # A visible-filtered locator has already applied the predicate; re-reading it
        # here would inspect the node a second time per wait, which a node that changes
        # state between the appear and disappear waits cannot survive.
        present = bool(nodes) if self._visible_only else (bool(nodes) and nodes[0].visible)
        if state == "visible" and not present:
            raise FakeTimeout(f"Timeout {timeout}ms exceeded waiting for visible")
        if state == "hidden" and present:
            raise FakeTimeout(f"Timeout {timeout}ms exceeded waiting for hidden")

    def _one(self):
        nodes = self._resolve()
        if not nodes:
            raise FakeTimeout("no match")
        return nodes[0]

    async def click(self, timeout=None):
        node = self._one()
        if not node.visible:
            raise FakeTimeout("not visible")
        node.clicked = True

    async def fill(self, value, timeout=None):
        node = self._one()
        if not node.visible:
            raise FakeTimeout("not visible")
        node.value = value

    async def is_enabled(self, timeout=None):
        return self._one().enabled

    async def inner_text(self, timeout=None):
        return self._one().text

    async def input_value(self, timeout=None):
        node = self._one()
        if node.value is None:
            raise FakeTimeout("not an input")
        return node.value


class FakeKeyboard:
    def __init__(self):
        self.typed: list[str] = []
        self.pressed: list[str] = []

    async def type(self, text, delay=None):
        self.typed.append(text)

    async def press(self, key):
        self.pressed.append(key)


class FakeHttpResponse:
    ok = True

    def __init__(self, payload):
        self._payload = payload

    async def text(self):
        return json.dumps(self._payload)


class FakePage:
    """A page whose DOM is a selector -> nodes mapping."""

    #: What the egress providers answer. A real reading, so a test can prove it SURVIVES
    #: a later failure rather than merely that the field exists.
    EGRESS = {"city": "Franklin", "region": "Tennessee", "ip": "1.2.3.4"}

    def __init__(self, dom, *, egress_ok=True):
        self.dom = dom
        self.egress_ok = egress_ok
        self.url = "about:blank"
        self.keyboard = FakeKeyboard()
        self.navigations: list[str] = []
        self.evaluated = 0

    def locator(self, selector):
        return FakeLocator(self.dom.get(selector, []))

    async def goto(self, url, timeout=None, wait_until=None):
        self.navigations.append(url)
        if "ipinfo.io" in url or "ip-api.com" in url:
            if not self.egress_ok:
                raise FakeTimeout("proxy unreachable")
            payload = dict(self.EGRESS)
            if "ip-api.com" in url:
                payload = {"city": payload["city"], "regionName": payload["region"],
                           "query": payload["ip"]}
            return FakeHttpResponse(payload)
        self.url = url
        return FakeHttpResponse({})

    async def title(self):
        return "Fake Surface"

    async def evaluate(self, script, args):
        # `_DISCOVERY_JS` itself needs a real browser. What is asserted here is that the
        # dump is requested, shaped and RETURNED even when the drive fails.
        self.evaluated += 1
        selector, limit = args
        nodes = self.dom.get(selector, [])
        return {
            "matched": len(nodes),
            "visible": sum(1 for n in nodes if n.visible),
            "sample": [
                {"tag": "textarea", "visible": n.visible, "text": n.text}
                for n in nodes[:limit]
            ],
        }

    def set_default_timeout(self, ms):
        pass


class FakeBrowser:
    def __init__(self, page):
        self._page = page
        self.closed = False

    @property
    def contexts(self):
        page = self._page

        class Ctx:
            pages = [page]

        return [Ctx()]

    async def close(self):
        self.closed = True


class FakeBrowserClient:
    """Records that the session is always released — rule 2."""

    instances: list["FakeBrowserClient"] = []

    def __init__(self, region=None):
        self.region = region
        self.stopped = False
        self.start_error: Exception | None = None
        FakeBrowserClient.instances.append(self)

    def start(self, **kwargs):
        if self.start_error:
            raise self.start_error
        return "sess-1"

    def generate_ws_headers(self):
        return "ws://fake", {}

    def stop(self):
        self.stopped = True


def _install(monkeypatch, page, *, start_error=None):
    FakeBrowserClient.instances.clear()

    def make_client(region=None):
        client = FakeBrowserClient(region=region)
        client.start_error = start_error
        return client

    monkeypatch.setattr(driver, "BrowserClient", make_client)

    class FakePlaywright:
        chromium = None

    class Chromium:
        async def connect_over_cdp(self, url, headers=None):
            return FakeBrowser(page)

    FakePlaywright.chromium = Chromium()

    class Ctx:
        async def __aenter__(self):
            return FakePlaywright()

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(driver, "async_playwright", lambda: Ctx())


@pytest.fixture(autouse=True)
def _no_real_sleeping(monkeypatch):
    """Collapse `_await_completion`'s stability fallback to zero wall-clock.

    It polls every 1.5s until the answer length stops changing, and when the answer
    selector matches nothing at all the length never satisfies `> 0` — so the loop runs
    to the full 165s budget. That is correct in production (a late answer is still an
    answer, and the deadline bounds it) and intolerable in a unit test: it hung the first
    run of this file.
    """
    # Capture the real sleep BEFORE patching: `driver.asyncio` is the one shared asyncio
    # module, so a lambda that calls `asyncio.sleep` calls the patch and recurses.
    real_sleep = asyncio.sleep
    monkeypatch.setattr(driver.asyncio, "sleep", lambda *_a, **_k: real_sleep(0))


def _request(dom_selectors, **overrides):
    payload = {
        "prompt": "Who are the best auto repair shops in Franklin, TN?",
        "surface": "chatgpt.com",
        "url": "https://chatgpt.com/",
        "selectors": {
            "input": "#composer",
            "submit": "#send",
            "answer": "#answer",
            "streaming": "#streaming",
            "consent": ["#accept"],
            "login_wall": ["#login"],
            "challenge": ["#captcha"],
            "citation": ["#cite"],
        },
    }
    payload["selectors"].update(dom_selectors)
    payload.update(overrides)
    return InvocationRequest.model_validate(payload)


def _run(req, page, monkeypatch, **kw):
    _install(monkeypatch, page, **kw)
    return asyncio.run(driver.run_invocation(req, region="us-east-1"))


# --- finding 1: the envelope threw away what it had already learned --------------


def test_a_selector_failure_still_reports_the_egress_that_was_measured(monkeypatch):
    """THE regression. The live run reported `observed_egress=null` after a selector
    timeout, and null is not "unknown" downstream - `egress_mismatch_reason` treats a
    missing city as a mismatch and fails the job terminally as `geo_egress_mismatch`.
    So the operator was handed a proxy fault to chase for a DOM change."""
    page = FakePage({"#composer": [FakeNode(visible=False)]})
    result = _run(_request({}), page, monkeypatch)

    assert result.answer_text == ""
    assert result.observed_egress is not None, (
        "the egress reading was discarded when a later step failed; the consumer will "
        "fail this job as geo_egress_mismatch and blame the proxy for a selector bug"
    )
    assert result.observed_egress.city == "Franklin"
    assert result.observed_egress.region == "Tennessee"


def test_a_failure_envelope_names_the_step_that_failed(monkeypatch):
    page = FakePage({"#composer": [FakeNode(visible=False)]})
    result = _run(_request({}), page, monkeypatch)

    assert result.trace, "trace came back empty, which is how the live failure read"
    assert result.trace["step"] == "enter_prompt"
    assert result.trace["surface"] == "chatgpt.com"
    assert result.trace["egress_source"] == "ipinfo.io"


def test_matched_but_invisible_is_distinguishable_from_matched_nothing(monkeypatch):
    """A bare Playwright timeout does not separate these two, and they have completely
    different fixes: a wrong selector versus a selector aimed at a node that is never
    visible. The live failure was the second and read as the first."""
    page = FakePage({"#composer": [FakeNode(visible=False) for _ in range(3)]})
    hidden = _run(_request({}), page, monkeypatch)
    assert hidden.trace["input_matched"] == 3
    assert hidden.trace["input_visible"] == 0

    missing = _run(_request({}), FakePage({}), monkeypatch)
    assert missing.trace["input_matched"] == 0


def test_a_failure_before_the_egress_check_reports_no_egress_and_says_so(monkeypatch):
    """The other direction: when the session never started there is genuinely nothing to
    report, and `step` is what tells the two apart.

    This test found a real bug. `client.start` sat OUTSIDE `run_invocation`'s try, so a
    failure escaped the function entirely and FastAPI answered 500 — breaking the "always
    200 with an envelope" invariant on the likeliest production failure of all: an
    execution role missing `StartBrowserSession`, which deploys cleanly and passes /ping.
    """
    page = FakePage({})
    result = _run(
        _request({}), page, monkeypatch,
        start_error=RuntimeError("AccessDeniedException: StartBrowserSession"),
    )

    assert result.observed_egress is None
    assert result.trace["step"] == "start_browser_session"
    assert "StartBrowserSession" in (result.error or "")


def test_a_session_that_never_started_is_not_stopped(monkeypatch):
    """Calling `stop` without a session raises a second error on top of the real one,
    which is how a permissions problem gets reported as a teardown problem."""
    _run(_request({}), FakePage({}), monkeypatch, start_error=RuntimeError("denied"))
    assert FakeBrowserClient.instances[-1].stopped is False


def test_every_egress_provider_failing_is_still_reported_as_absent(monkeypatch):
    page = FakePage(
        {
            "#composer": [FakeNode(visible=True, value="")],
            "#streaming": [FakeNode()],
            "#answer": [FakeNode(text="An answer.")],
        },
        egress_ok=False,
    )
    result = _run(_request({}), page, monkeypatch)

    assert result.observed_egress is None
    assert result.trace["egress_source"] is None
    # It got past the check rather than dying on it - unverifiable geography is a
    # mismatch, not a crash.
    assert result.trace["step"] != "egress"


def test_the_browser_session_is_released_even_when_the_drive_fails(monkeypatch):
    """Rule 2. A leaked session is a paid browser running until the 300s backstop."""
    page = FakePage({"#composer": [FakeNode(visible=False)]})
    _run(_request({}), page, monkeypatch)
    assert FakeBrowserClient.instances[-1].stopped is True


# --- finding 2: positional selection over a multi-match selector ------------------


def test_a_hidden_first_match_does_not_hide_the_real_composer(monkeypatch):
    """Exactly the live DOM: ChatGPT's hidden fallback `<textarea
    name="prompt-textarea">` precedes the contenteditable composer in document order, so
    `.first` waited out its whole timeout on a node that can never become visible."""
    page = FakePage(
        {
            "#composer": [FakeNode(visible=False), FakeNode(visible=True, value="")],
            "#send": [FakeNode()],
            "#streaming": [FakeNode()],
            "#answer": [FakeNode(text="Franklin Auto Care is well reviewed.")],
        }
    )
    result = _run(_request({}), page, monkeypatch)

    assert result.answer_text == "Franklin Auto Care is well reviewed."
    assert result.error is None
    assert result.trace["input_matched"] == 2
    assert result.trace["input_visible"] == 1


def test_a_login_wall_whose_first_match_is_hidden_is_still_detected(monkeypatch):
    """The costly direction. Missing the wall reports an empty answer instead, which the
    consumer RETRIES - buying a second paid session against the same wall."""
    page = FakePage(
        {
            "#login": [FakeNode(visible=False), FakeNode(visible=True)],
            "#composer": [FakeNode(visible=True, value="")],
        }
    )
    result = _run(_request({}), page, monkeypatch)

    assert result.login_wall is True
    assert result.trace["login_wall_selector"] == "#login"


def test_a_wall_with_only_hidden_matches_is_not_a_wall(monkeypatch):
    """The inverse must also hold, or every page looks walled and no run ever reaches
    the composer."""
    page = FakePage(
        {
            "#login": [FakeNode(visible=False)],
            "#composer": [FakeNode(visible=True, value="")],
            "#streaming": [FakeNode()],
            "#answer": [FakeNode(text="An answer.")],
        }
    )
    result = _run(_request({}), page, monkeypatch)
    assert result.login_wall is False
    assert result.answer_text == "An answer."


def test_a_hidden_first_match_does_not_cost_us_the_streaming_signal(monkeypatch):
    """The quiet `.first` failure. A hidden first match makes the APPEAR wait time out,
    which does not error - it silently demotes the run to the text-stability fallback,
    and stability is the path that truncates: a surface pausing ~4.5s mid-answer reads as
    finished, and a truncated answer scores as a complete one.

    Written to discriminate. The first version of this test used a hidden-ONLY indicator
    and passed against the unfixed code too, because the appear wait fails identically
    either way - a mutation run caught it. What distinguishes the two is a hidden match
    sitting in FRONT of a real one.
    """
    appeared_then_finished = _TransientNode()
    page = FakePage(
        {
            "#composer": [FakeNode(visible=True, value="")],
            "#streaming": [FakeNode(visible=False), appeared_then_finished],
            "#answer": [FakeNode(text="A complete answer.")],
        }
    )
    result = _run(_request({}), page, monkeypatch)

    assert result.trace["stream_appeared"] is True
    assert result.trace["completion"] == "streaming_selector_hidden", (
        "fell back to text-stability because the streaming indicator was resolved "
        "positionally onto a hidden node; stability can truncate a slow answer"
    )


def test_a_streaming_selector_matching_nothing_visible_falls_back_honestly(monkeypatch):
    """The other half: when there genuinely is no visible indicator, the weaker path is
    correct and `trace` has to say which one ran."""
    page = FakePage(
        {
            "#composer": [FakeNode(visible=True, value="")],
            "#streaming": [FakeNode(visible=False)],
            "#answer": [FakeNode(text="A complete answer.")],
        }
    )
    result = _run(_request({}), page, monkeypatch)

    assert result.trace["stream_appeared"] is False
    assert result.trace["completion"] == "text_stabilized"


# --- the prompt must actually reach the composer ----------------------------------


def test_a_fill_that_does_not_stick_is_retyped(monkeypatch):
    """`fill` reports success against a React-controlled node whose next render reverts
    it. An empty composer does not error - the surface answers whatever was already on
    screen, and that is filed as a measured answer to OUR prompt."""

    reverting = FakeNode(visible=True, value="")

    class RevertingLocator(FakeLocator):
        async def fill(self, value, timeout=None):
            return None  # accepted, then silently reverted

    page = FakePage(
        {
            "#composer": [reverting],
            "#streaming": [FakeNode()],
            "#answer": [FakeNode(text="An answer.")],
        }
    )
    original = page.locator

    def locator(selector):
        if selector == "#composer":
            return RevertingLocator([reverting])
        return original(selector)

    page.locator = locator
    result = _run(_request({}), page, monkeypatch)

    assert result.trace["input_readback"].startswith("lost_after_")
    assert "retyped" in result.trace["input_method"]
    assert page.keyboard.typed, "the prompt was never actually typed"
    assert "ControlOrMeta+a" in page.keyboard.pressed, (
        "typing without clearing first would submit the prompt twice over"
    )


def test_a_fill_that_sticks_is_not_retyped(monkeypatch):
    page = FakePage(
        {
            "#composer": [FakeNode(visible=True, value="")],
            "#streaming": [FakeNode()],
            "#answer": [FakeNode(text="An answer.")],
        }
    )
    result = _run(_request({}), page, monkeypatch)
    assert result.trace["input_readback"] == "ok"
    assert result.trace["input_method"] == "fill"
    assert not page.keyboard.typed


# --- finding 3: discovery has to answer even when the drive does not --------------


def test_discovery_is_returned_even_though_the_drive_failed(monkeypatch):
    """The whole point. The first live `--discover` run died on step one and reported
    nothing about the page; the only fact we learned came from an element quoted inside a
    Playwright error string."""
    page = FakePage({"#composer": [FakeNode(visible=False), FakeNode(visible=False)]})
    result = _run(_request({}, discover=True), page, monkeypatch)

    assert result.answer_text == ""
    assert result.discovery is not None, "a failed discovery run reported nothing"
    on_load = result.discovery["on_load"]
    assert on_load["phase"] == "on_load"
    assert on_load["input"]["#composer"]["matched"] == 2
    assert on_load["input"]["#composer"]["visible"] == 0


def test_discovery_dumps_the_answer_classes_only_after_an_answer_exists(monkeypatch):
    page = FakePage(
        {
            "#composer": [FakeNode(visible=True, value="")],
            "#streaming": [FakeNode()],
            "#answer": [FakeNode(text="An answer.")],
        }
    )
    result = _run(_request({}, discover=True), page, monkeypatch)

    assert set(result.discovery) == {"on_load", "after_answer"}
    assert result.discovery["after_answer"]["answer"]["#answer"]["matched"] == 1


def test_discovery_never_clicks_anything(monkeypatch):
    """Discovery runs broad selectors - `["button"]` for consent - and the old code
    clicked the first visible match of each. On a real page that is as likely to be
    "Log in" as "Accept", so the run whose job is to observe the default state was
    mutating it and could manufacture the very wall it then reported."""
    accept = FakeNode(visible=True, text="Accept")
    page = FakePage(
        {
            "#accept": [accept],
            "#composer": [FakeNode(visible=True, value="")],
            "#streaming": [FakeNode()],
            "#answer": [FakeNode(text="An answer.")],
        }
    )
    result = _run(_request({}, discover=True), page, monkeypatch)

    assert accept.clicked is False
    assert result.trace["consent_skipped_for_discovery"] == ["#accept"]
    assert "consent_dismissed" not in result.trace


def test_a_normal_run_still_dismisses_consent_and_dumps_nothing(monkeypatch):
    """On a residential exit these walls are the norm, so the skip must be scoped to
    discovery only."""
    accept = FakeNode(visible=True, text="Accept")
    page = FakePage(
        {
            "#accept": [accept],
            "#composer": [FakeNode(visible=True, value="")],
            "#streaming": [FakeNode()],
            "#answer": [FakeNode(text="An answer.")],
        }
    )
    result = _run(_request({}), page, monkeypatch)

    assert accept.clicked is True
    assert result.trace["consent_dismissed"] == ["#accept"]
    assert result.discovery is None
    assert page.evaluated == 0


def test_consent_skips_a_hidden_candidate_rather_than_clicking_it(monkeypatch):
    hidden = FakeNode(visible=False, text="Accept")
    page = FakePage(
        {
            "#accept": [hidden],
            "#composer": [FakeNode(visible=True, value="")],
            "#streaming": [FakeNode()],
            "#answer": [FakeNode(text="An answer.")],
        }
    )
    result = _run(_request({}), page, monkeypatch)
    assert hidden.clicked is False
    assert result.trace["consent_dismissed"] == []


# --- the operator flag must never be on in production ----------------------------


def test_discover_defaults_to_off(monkeypatch):
    """aeo-agent-service never sends it. A production run with it on would report a page
    inventory instead of a measurement, and would skip the consent click real runs need."""
    assert _request({}).discover is False


@pytest.mark.parametrize("field", ["discovery", "trace"])
def test_the_diagnostic_fields_are_optional_for_the_consumer(field):
    """`_normalize` reads neither. They must never become required, or a consumer that
    ignores them starts failing to parse the envelope it depends on."""
    from app.models import InvocationResponse

    assert field in InvocationResponse().model_dump()
