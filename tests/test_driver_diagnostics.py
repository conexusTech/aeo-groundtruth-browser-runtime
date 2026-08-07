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
from types import SimpleNamespace

import pytest

from app import driver
from app.models import InvocationRequest

# --- fakes ----------------------------------------------------------------------


class FakeTimeout(Exception):
    """Stands in for playwright's TimeoutError, which the driver only ever catches."""


class FakeNode:
    def __init__(self, *, visible=True, text="", value=None, enabled=True,
                 href=None, furniture=False, text_without_furniture=None):
        self.visible = visible
        self.text = text
        self.value = value
        self.enabled = enabled
        self.clicked = False
        #: For citation nodes.
        self.href = href
        #: True when this node sits inside a `sel.exclude` subtree - chatgpt's business
        #: map widget renders INSIDE the assistant turn, so the anchor looks like a real
        #: citation and only its ancestry says otherwise.
        self.furniture = furniture
        #: What the answer reads to once the excluded subtrees are stripped.
        self.text_without_furniture = text_without_furniture


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

    async def get_attribute(self, name, timeout=None):
        # Added 2026-08-07. Its absence meant `_read_citations`' locator path raised
        # AttributeError on every call, was swallowed by that function's own
        # never-fail-a-run handler, and returned zero citations — so the ORIGINAL
        # citation reader had never once been exercised by a test.
        return getattr(self._one(), name, None)

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
        self.fingerprint_probes = 0
        self.discovery_dumps = 0
        self.load_states: list[str] = []
        #: Cookie names by the time each read happens. The surface sets its device
        #: identity via an XHR that lands AFTER `domcontentloaded`, so the fake grows a
        #: cookie once the driver has waited - which is the whole behaviour under test.
        self._cookies = [{"name": "__Host-next-auth.csrf-token"}]
        self.context = SimpleNamespace(cookies=self._read_cookies)

    async def _read_cookies(self):
        return list(self._cookies)

    async def wait_for_load_state(self, state, timeout=None):
        self.load_states.append(state)
        if state == "networkidle":
            # The bootstrap completes only once something actually waits for it.
            self._cookies = self._cookies + [{"name": "oai-did"}]

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

    async def evaluate(self, script, args=None):
        # `args` is optional because `_FINGERPRINT_JS` is evaluated WITHOUT any, and a
        # two-argument-only signature made that call raise `TypeError` — which
        # `_probe_fingerprint` catches, so the probe silently recorded an error and every
        # test still passed. A fake that cannot express the call the implementation makes
        # cannot falsify it.
        self.evaluated += 1
        if "cloneNode" in script:                       # _TEXT_WITHOUT_JS
            selector, exclude = args
            nodes = self.dom.get(selector, [])
            if not nodes:
                return ""
            n = nodes[-1]
            if exclude and n.text_without_furniture is not None:
                return n.text_without_furniture
            return n.text
        if "closest" in script:                          # _LINKS_WITHOUT_JS
            selectors, exclude = args
            out = []
            for sel_ in selectors:
                for n in self.dom.get(sel_, []):
                    if exclude and n.furniture:
                        continue
                    if n.href:
                        out.append({"url": n.href, "title": n.text or None})
            return out
        if args is None:
            self.fingerprint_probes += 1
            # Stand in for a headless browser: the markers the stealth script targets.
            return {"webdriver": True, "plugins": 0, "chrome_obj": "undefined"}
        # Counted apart from the fingerprint probe on purpose. "A normal run dumps
        # nothing" is a real invariant about DISCOVERY, and folding both into one
        # counter would have forced that assertion to be loosened to accommodate an
        # unrelated feature — which is how a meaningful test quietly stops meaning it.
        self.discovery_dumps += 1
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


class FakeCdpSession:
    """Records CDP commands. `Emulation.setUserAgentOverride` is the one that matters:
    it replaces the outgoing User-Agent HEADER as well as `navigator.userAgent`, which a
    page-side init script cannot do."""

    def __init__(self, browser):
        self._browser = browser

    async def send(self, method, params=None):
        self._browser.cdp_calls.append((method, params or {}))
        return {}


class FakeBrowser:
    instances: list["FakeBrowser"] = []

    def __init__(self, page):
        self._page = page
        self.closed = False
        self.cdp_calls: list[tuple[str, dict]] = []
        FakeBrowser.instances.append(self)

    @property
    def contexts(self):
        page = self._page
        browser = self

        class Ctx:
            pages = [page]

            async def new_cdp_session(self, page):
                return FakeCdpSession(browser)

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
    FakeBrowser.instances.clear()

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
        # A backstop, not a behaviour under test. The stability fallback is bounded only
        # by this budget, so a fake whose answer never stabilises otherwise burns the
        # production default (165s) inside a unit test. Every step here is instant, so
        # nothing legitimate comes close to it.
        "timeout_seconds": 8.0,
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
    consumer RETRIES - buying a second paid session against the same wall.

    The composer is absent here, because a wall is only consulted once the composer
    cannot be used - see the composer-first tests below.
    """
    page = FakePage({"#login": [FakeNode(visible=False), FakeNode(visible=True)]})
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


# --- rule 5: the composer is tried BEFORE the page is called walled ---------------


def test_a_visible_login_button_next_to_a_working_composer_is_not_a_wall(monkeypatch):
    """Exactly the live page. chatgpt.com's logged-out landing page carries
    `data-testid='login-button'` AND `data-testid='signup-button'` as permanent header
    chrome, beside a working composer. The old pre-emptive gate returned
    `login_wall=True` without typing a word - and login_wall is TERMINAL, so every
    ground-truth job on that surface would report "cannot measure here" forever, which
    from the outside is indistinguishable from a surface that really does wall us."""
    page = FakePage(
        {
            "#login": [FakeNode(visible=True, text="Log in")],
            "#composer": [FakeNode(visible=True, value="")],
            "#streaming": [FakeNode()],
            "#answer": [FakeNode(text="Franklin Auto Care is well reviewed.")],
        }
    )
    result = _run(_request({}), page, monkeypatch)

    assert result.login_wall is False, (
        "page furniture was treated as a wall; the run never asked the question"
    )
    assert result.answer_text == "Franklin Auto Care is well reviewed."
    assert "login_wall_selector" not in result.trace


def test_a_wall_is_reported_when_the_composer_really_cannot_be_used(monkeypatch):
    """The gate still has to fire when it means something - here the wall is up and there
    is no usable composer behind it."""
    page = FakePage(
        {
            "#login": [FakeNode(visible=True, text="Log in to continue")],
            "#composer": [FakeNode(visible=False)],
        }
    )
    result = _run(_request({}), page, monkeypatch)

    assert result.login_wall is True
    assert result.trace["step"] == "enter_prompt"
    # The Playwright cause is kept: "walled" and "walled AND our selector timed out" are
    # different follow-ups, and only one of them is someone else's problem.
    assert "FakeTimeout" in (result.error or "")


def test_an_unusable_composer_with_no_wall_is_an_error_not_a_wall(monkeypatch):
    """The important negative. If nothing is walled, the fault is OURS - a selector or a
    timing - and it must not be laundered into a terminal login_wall the consumer will
    never retry."""
    page = FakePage({"#composer": [FakeNode(visible=False)]})
    result = _run(_request({}), page, monkeypatch)

    assert result.login_wall is False
    assert result.challenge is False
    assert result.error and "FakeTimeout" in result.error


def test_a_challenge_is_still_reported_when_it_blocks_the_composer(monkeypatch):
    page = FakePage(
        {
            "#captcha": [FakeNode(visible=True)],
            "#composer": [FakeNode(visible=False)],
        }
    )
    result = _run(_request({}), page, monkeypatch)

    assert result.challenge is True
    assert result.login_wall is False
    assert result.trace["challenge_selector"] == "#captcha"


def test_a_wall_that_appears_only_after_submitting_is_still_caught(monkeypatch):
    """Several surfaces allow one anonymous question and demand an account afterwards.
    That is the second call site, and it is why the check could not simply be deleted."""
    page = FakePage(
        {
            "#composer": [FakeNode(visible=True, value="")],
            # Transient, so completion is detected on the fast path. With a permanently
            # visible indicator this test fell through to the text-stability loop, which
            # never stabilises on an empty answer and so ran to the full 165s budget - it
            # alone took 165 of the suite's 166 seconds.
            "#streaming": [_TransientNode()],
            "#answer": [],  # submitted fine, then nothing rendered
            "#login": [FakeNode(visible=True, text="Sign in to continue")],
        }
    )
    result = _run(_request({}), page, monkeypatch)

    assert result.login_wall is True
    assert "after submitting" in (result.error or "")


def test_a_prompt_that_never_lands_in_the_composer_is_not_submitted(monkeypatch):
    """Pressing Enter on an empty composer makes the surface answer whatever was already
    on screen, and that gets filed as a measured answer to OUR prompt. Nothing downstream
    can detect it, so it has to fail here."""

    unwritable = FakeNode(visible=True, value="")

    class SwallowingLocator(FakeLocator):
        async def fill(self, value, timeout=None):
            return None

        async def input_value(self, timeout=None):
            return ""  # and typing does not stick either

        async def inner_text(self, timeout=None):
            return ""

    page = FakePage({"#composer": [unwritable], "#answer": [FakeNode(text="Welcome!")]})
    original = page.locator
    page.locator = lambda s: (
        SwallowingLocator([unwritable]) if s == "#composer" else original(s)
    )

    result = _run(_request({}), page, monkeypatch)

    assert result.answer_text == "", "a welcome message was about to be scored as the answer"
    assert result.trace["input_readback_2"] == "still_lost"
    assert "PromptNotEntered" in (result.error or "")


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


# --- a closed page is terminal, not "no answer yet" -------------------------------


class _ClosedLocator(FakeLocator):
    """Playwright's behaviour once the target is gone: every call raises."""

    async def count(self):
        raise FakeTimeout("Locator.count: Target page, context or browser has been closed")


def test_a_closed_browser_stops_polling_instead_of_burning_the_budget(monkeypatch):
    """The live cost bug. `_read_answer` swallowed the closed-page error and returned "",
    so `current > 0` was never true, `stable_rounds` never incremented, and the stability
    loop polled a dead browser every 1.5s for the WHOLE remaining budget - about 110s of
    paid session, logging the same warning 70+ times."""
    page = FakePage(
        {
            "#composer": [FakeNode(visible=True, value="")],
            "#streaming": [_TransientNode()],
        }
    )
    original = page.locator
    page.locator = lambda s: _ClosedLocator([]) if s == "#answer" else original(s)

    result = _run(_request({}), page, monkeypatch)

    assert result.trace.get("page_gone") is True
    assert "PageGone" in (result.error or "")
    # Not misfiled as a page-state verdict about the surface: a closed page is usually
    # someone else closing it, so the consumer must be free to retry.
    assert result.login_wall is False
    assert result.challenge is False


def test_a_session_bounced_off_the_host_stops_polling_immediately(monkeypatch):
    """🔴 ~60s of PAID browser per walled job, spent on a page that can never answer.

    A walled chatgpt.com session is redirected to `auth.openai.com` the instant it
    submits, and the appear loop then polled that page for the whole remaining budget —
    measured at `elapsed_s=92.54`, against ~83s before appearing was un-capped.

    This is also `url_at_completion`'s first consumer. The field was recorded for exactly
    this comparison, and `_await_completion`'s own comment claimed the caller made it,
    but nothing in either repo ever read it.
    """
    monkeypatch.setattr(driver.time, "monotonic", _SteppingClock(5.0))
    page = _bounced_page("https://auth.openai.com/log-in-or-create-account", "Log in")
    # No streaming indicator, which is PRODUCTION: `streaming_selector` is None on both
    # chatgpt.com and perplexity.ai, so every real run takes the text fallback. The shared
    # fixture's `_TransientNode` would exit via the fast path and never reach the loop
    # this test is about.
    page.dom["#streaming"] = [FakeNode(visible=False)]
    result = _run(_request({}, timeout_seconds=400.0), page, monkeypatch)

    assert result.trace["completion"] == "navigated_away", (
        "still polling a page the session was bounced off"
    )
    assert result.trace["url_at_completion"].startswith("https://auth.openai.com/")
    # The point of the fix: it gave up early rather than burning the budget.
    assert len(result.trace["stability_samples"]) <= 3, (
        f"polled {len(result.trace['stability_samples'])}x after leaving the surface"
    )


def test_a_successful_navigation_to_the_conversation_url_keeps_polling(monkeypatch):
    """THE critical negative. chatgpt.com navigates `/` → `/c/<id>` on a SUCCESSFUL
    submit, so comparing full URLs instead of HOSTS would abort every healthy run and
    report a wall on the exact path that worked."""
    monkeypatch.setattr(driver.time, "monotonic", _SteppingClock(5.0))
    page = _bounced_page("https://chatgpt.com/c/abc123", "ChatGPT")
    page.dom["#streaming"] = [FakeNode(visible=False)]

    # ⚠️ The answer must arrive LATE, and that is the whole point of this test. With it
    # present on the first read the appear loop never runs, the host comparison is never
    # reached, and a mutation swapping HOST for full-URL survived — the test could not
    # fail for the reason it exists.
    answer = "Franklin Roof Co is well reviewed."
    reads = {"n": 0}

    class LateLocator(FakeLocator):
        async def inner_text(self, timeout=None):
            reads["n"] += 1
            return answer if reads["n"] > 3 else ""

    original = page.locator
    page.locator = lambda s: (
        LateLocator([FakeNode(text="")]) if s == "#answer" else original(s)
    )
    result = _run(_request({}, timeout_seconds=400.0), page, monkeypatch)

    assert result.answer_text == answer, (
        "a same-host navigation to the conversation URL aborted a healthy run"
    )
    assert result.trace["completion"] != "navigated_away"
    assert result.trace["answer_appeared_after_polls"] > 1, (
        "the answer was present immediately, so the host comparison never ran"
    )


def test_page_furniture_inside_the_answer_is_excluded_from_text_and_citations(monkeypatch):
    """🔴 Measured from live ground-truth data (2026-08-07).

    chatgpt.com renders `<div data-testid='businesses-map-widget'>` INSIDE the assistant
    turn. Both the answer selector and the citation selector are correctly scoped to that
    turn, so both swallowed it, and the consequences reached the customer:

      * answers arrived as a directory dump — star ratings, "Closed", "Give feedback" —
        rather than prose, and that is the text the extractor classifies entities from;
      * `mapbox.com` (20) and `openstreetmap.org` (10) were stored as CITATIONS, 24% of
        every citation captured, i.e. map attribution recorded as "the AI cited this".

    An anchor inside the widget is indistinguishable from a real citation on its own —
    only its ANCESTRY says otherwise — which is why the fix tests `closest()` rather than
    denylisting hostnames.
    """
    monkeypatch.setattr(driver.time, "monotonic", _SteppingClock(5.0))
    page = FakePage(
        {
            "#composer": [FakeNode(visible=True, value="")],
            "#streaming": [FakeNode(visible=False)],
            "#answer": [
                FakeNode(
                    text="Franklin Roof Co is well reviewed. Superior Roofing 3.7 Closed",
                    text_without_furniture="Franklin Roof Co is well reviewed.",
                )
            ],
            "#cite": [
                FakeNode(text="franklinroofco", href="https://franklinroofco.com/"),
                FakeNode(text="Mapbox", href="https://mapbox.com/about/maps",
                         furniture=True),
                FakeNode(text="OpenStreetMap", href="https://openstreetmap.org/",
                         furniture=True),
            ],
        }
    )
    req = _request({}, timeout_seconds=400.0)
    req.selectors.exclude = ["[data-testid='businesses-map-widget']"]
    result = _run(req, page, monkeypatch)

    assert result.answer_text == "Franklin Roof Co is well reviewed.", (
        "the map widget's ratings and hours are still being read as the answer"
    )
    hosts = {c.url for c in result.citations}
    assert hosts == {"https://franklinroofco.com/"}, (
        f"map attribution is still stored as a citation: {hosts}"
    )


def test_without_an_exclude_list_reading_is_unchanged(monkeypatch):
    """The non-regression half. perplexity.ai has no furniture to strip, so it must keep
    taking the plain locator path — a surface that sets no `exclude` must not silently
    change behaviour."""
    monkeypatch.setattr(driver.time, "monotonic", _SteppingClock(5.0))
    page = FakePage(
        {
            "#composer": [FakeNode(visible=True, value="")],
            "#streaming": [FakeNode(visible=False)],
            "#answer": [FakeNode(text="Quality Exteriors is a long-standing contractor.")],
            "#cite": [FakeNode(text="qualityexteriors", href="https://qualityexteriors.com/")],
        }
    )
    result = _run(_request({}, timeout_seconds=400.0), page, monkeypatch)

    assert result.answer_text == "Quality Exteriors is a long-standing contractor."
    assert [c.url for c in result.citations] == ["https://qualityexteriors.com/"]


def test_the_device_identity_is_already_present_when_we_submit(monkeypatch):
    """🪦 The autopsy of the "we submit too early" hypothesis, kept so nobody re-derives it.

    The theory: chatgpt.com walls us because we press Enter before its device identity
    (`oai-did`, echoed as an `oai-device-id` header) exists — we navigate on
    `domcontentloaded`, which fires before that XHR lands, and the composer renders early
    too. It fit everything: intermittency, identical fingerprints on both outcomes, the
    wall landing only after submit, perplexity unaffected, and a residential proxy losing
    a race more often than a datacentre egress.

    It was wrong. Two live sessions reported `oai-did` already present at load, and the
    cookie list byte-identical before and after a `networkidle` wait. That wait was
    REMOVED — chatgpt.com long-polls so it never idles, and it burned its full 8s on every
    job to change nothing.

    What survives is this: the cookie state at submit is recorded, so the claim "the
    surface was ready" stays evidence rather than memory.
    """
    monkeypatch.setattr(driver.time, "monotonic", _SteppingClock(5.0))
    page = FakePage({"#composer": [FakeNode(visible=True, value="")]})
    page._cookies = [{"name": "oai-did"}, {"name": "__cf_bm"}]
    result = _run(_request({}, timeout_seconds=400.0), page, monkeypatch)

    assert result.trace["cookies_at_submit"] == ["__cf_bm", "oai-did"]
    assert page.load_states == [], (
        "the removed networkidle wait is back; chatgpt.com never idles, so it costs its "
        "whole budget on every job and changes nothing"
    )


def test_cookie_names_are_recorded_but_never_their_values(monkeypatch):
    """`trace` is persisted to `sov_results.raw_response` as jsonb by the consumer, and a
    session cookie is a credential. Names diagnose the race; values would be a leak."""
    monkeypatch.setattr(driver.time, "monotonic", _SteppingClock(5.0))
    page = FakePage({"#composer": [FakeNode(visible=True, value="")]})
    page._cookies = [{"name": "oai-did", "value": "SECRET-DEVICE-TOKEN"}]
    # Budget well above the stepping clock: at 5s per READ the 8s default expires
    # while `remaining_ms` is being evaluated, so the settle is never reached.
    result = _run(_request({}, timeout_seconds=400.0), page, monkeypatch)

    assert "SECRET-DEVICE-TOKEN" not in json.dumps(result.trace)
    assert "oai-did" in result.trace["cookies_at_submit"]


def test_the_self_identifying_user_agent_is_replaced_before_anything_navigates(monkeypatch):
    """🔴 What the fingerprint probe actually found, and it killed the previous theory.

    Every marker a stealth script normally patches was ALREADY clean on the AgentCore
    browser — `webdriver: false`, `cdc_keys: 0`, `chrome_obj: 'object'`, `plugins: 5`,
    `ua_headless: false`. The A/B proved it: `stealth=applied` produced a fingerprint
    byte-identical to the control, and both were walled.

    What the UA said instead was
    `... Chrome/148.0.0.0 Amazon-Bedrock-AgentCore-Browser` — AWS appending a product
    token that announces the browser as automated, on every HTTP request.

    It must be overridden BEFORE the first navigation, because the UA travels as a
    request HEADER and a request already sent cannot be un-sent. That is also why this is
    a CDP `Emulation.setUserAgentOverride` and not an init script: an init script can
    only reach `navigator.userAgent`, leaving the header untouched.
    """
    monkeypatch.setattr(driver.time, "monotonic", _SteppingClock(5.0))
    page = FakePage({"#composer": [FakeNode(visible=True, value="")]})
    result = _run(_request({}), page, monkeypatch)

    calls = dict(FakeBrowser.instances[0].cdp_calls)
    assert "Emulation.setUserAgentOverride" in calls, "the UA was never overridden"
    ua = calls["Emulation.setUserAgentOverride"]["userAgent"]
    assert "Amazon" not in ua and "AgentCore" not in ua, (
        "the UA still announces the browser as an automated AgentCore session"
    )
    assert result.trace["ua_masked"] is True


def test_the_replacement_user_agent_stays_internally_consistent(monkeypatch):
    """An INCONSISTENT fingerprint is a stronger bot signal than an unusual coherent one.

    Claiming Windows here would contradict `navigator.platform`, the WebGL renderer and
    the Sec-CH-UA client hints, all of which still say Linux. So the replacement keeps
    the platform and the Chrome major version and only drops the product token.
    """
    monkeypatch.setattr(driver.time, "monotonic", _SteppingClock(5.0))
    page = FakePage({"#composer": [FakeNode(visible=True, value="")]})
    _run(_request({}), page, monkeypatch)

    params = dict(FakeBrowser.instances[0].cdp_calls)["Emulation.setUserAgentOverride"]
    assert "X11; Linux x86_64" in params["userAgent"]
    assert params["platform"] == "Linux x86_64"
    assert "Chrome/148" in params["userAgent"]
    assert params["userAgent"].endswith("Safari/537.36"), (
        "a real Chrome UA ends in Safari/537.36; the AgentCore one replaces it"
    )


def test_ua_masking_can_be_turned_off_for_a_control_run(monkeypatch):
    """The off switch is what makes this measurable rather than a belief. It is the only
    reason the previous hypothesis could be falsified instead of quietly believed."""
    monkeypatch.setattr(driver.time, "monotonic", _SteppingClock(5.0))
    page = FakePage({"#composer": [FakeNode(visible=True, value="")]})
    result = _run(_request({}, stealth=False), page, monkeypatch)

    assert FakeBrowser.instances[0].cdp_calls == []
    assert result.trace["ua_masked"] == "off"


def test_the_fingerprint_is_probed_on_a_failed_run_too(monkeypatch):
    """The comparison that matters is walled-session vs answered-session, so a probe that
    only ran on success would compare nothing. This page never answers."""
    # Advance-per-read clock: these pages never answer, so with the real clock
    # each one spins the appear loop for the whole 8s budget - 24s of suite time
    # for three tests that are not about timing at all.
    monkeypatch.setattr(driver.time, "monotonic", _SteppingClock(5.0))
    page = FakePage({"#composer": [FakeNode(visible=True, value="")], "#answer": []})
    # Budget raised well above the stepping clock: at 5s per READ the 8s default
    # expires during the egress lookup, so `_drive` never reaches the probe and the
    # test fails for a reason that has nothing to do with fingerprinting.
    result = _run(_request({}, timeout_seconds=400.0), page, monkeypatch)

    assert result.answer_text == ""
    assert page.fingerprint_probes >= 1, "the fingerprint was never read"
    assert result.trace["fingerprint"]["webdriver"] is True


class _SteppingClock:
    """A monotonic clock that advances a fixed amount on every READ.

    `asyncio.sleep` is a no-op in this file, so wall-clock never moves — which means
    neither the settle cap nor the deadline can ever be reached, and "the answer took
    longer to render than the cap allows" is not expressible at all. Advancing per read
    is what makes it testable without spending real seconds.
    """

    def __init__(self, step=5.0):
        self.step = step
        self.now = 0.0

    def __call__(self):
        self.now += self.step
        return self.now


def _late_answer_page(text, appears_after):
    """A page whose answer container matches nothing until the Nth read."""
    page = FakePage(
        {
            "#composer": [FakeNode(visible=True, value="")],
            "#streaming": [FakeNode(visible=False)],
            "#answer": [FakeNode(text="")],
        }
    )
    reads = {"n": 0}

    class LateLocator(FakeLocator):
        async def inner_text(self, timeout=None):
            reads["n"] += 1
            return text if reads["n"] > appears_after else ""

    original = page.locator
    page.locator = lambda s: (
        LateLocator([FakeNode(text="")]) if s == "#answer" else original(s)
    )
    return page


def test_a_slowly_rendering_answer_is_not_cancelled_by_the_settle_cap(monkeypatch):
    """🔴 The live perplexity.ai failure of 2026-08-06, and the reason the wait is now
    two phases rather than one clock.

    That job reached `/search/<id>` titled with our own prompt — it HAD answered — and
    still failed, logging `samples=[0,0,0,0,0,0,0,0,0,0]` at `elapsed_s=84.33`: about
    19s of overhead plus the 65s cap, exactly. Perplexity runs a web search before it
    renders ("Searching the web", "10 sources"), so the container simply had not appeared
    yet, and the session was cancelled with ~35s of its budget unspent.

    The cap is the right bound for text that will not SETTLE and the wrong bound for text
    that has not APPEARED. Under the single-clock version this test reports
    `text_never_stabilized` and returns no answer.
    """
    monkeypatch.setattr(driver.time, "monotonic", _SteppingClock(5.0))
    answer = "Franklin Roof Co. is the most trusted roofer in Franklin, TN."
    page = _late_answer_page(answer, appears_after=12)

    result = _run(_request({}, timeout_seconds=100_000.0), page, monkeypatch)

    assert result.answer_text == answer, (
        "a slow render was cancelled by the settle cap, losing an answer the session "
        "had already paid for"
    )
    assert result.trace["completion"] == "text_stabilized"
    # The zeros are still recorded: the appear phase is where the diagnosis lives.
    assert result.trace["stability_samples"], "the samples were dropped on the happy path"
    assert result.trace["answer_appeared_after_polls"] > 1


def test_an_answer_that_never_renders_is_distinguished_from_one_that_never_settles(
    monkeypatch,
):
    """`samples=[0,0,…]` and `samples=[2502,2504,…]` are opposite faults with opposite
    fixes, and the single-clock version reported BOTH as `text_never_stabilized`.

    Never-appeared means the render is slow or the selector is wrong; never-settled means
    something in the container keeps mutating and no cap will ever help. Tonight that
    ambiguity is what made six chatgpt.com failures and one perplexity.ai failure look
    like the same bug when they were nothing alike.
    """
    monkeypatch.setattr(driver.time, "monotonic", _SteppingClock(5.0))
    page = FakePage(
        {
            "#composer": [FakeNode(visible=True, value="")],
            "#streaming": [FakeNode(visible=False)],
            "#answer": [],  # nothing ever matches
        }
    )

    result = _run(_request({}, timeout_seconds=400.0), page, monkeypatch)

    assert result.trace["completion"] == "answer_never_appeared", (
        "a container that never rendered was reported as text that never stabilised"
    )
    assert set(result.trace["stability_samples"]) == {0}
    assert "answer_appeared_after_polls" not in result.trace


def test_the_stability_fallback_is_bounded_independently_of_the_deadline(monkeypatch):
    """A live perplexity.ai run spent 164 seconds - its entire budget - polling for an
    answer that never stabilised, then returned nothing. The deadline is the whole
    invocation's budget, so using it as this loop's only bound spends everything on the
    weakest completion signal we have.

    The cap is shrunk rather than the deadline raised. With a real 90s cap this test would
    either spin for 90 seconds of monotonic time (sleep is a no-op here) or hit the
    fixture's deadline first and prove nothing — which is exactly what the first version
    did, reporting `text_still_growing_at_deadline` and passing for the wrong reason.
    """
    monkeypatch.setattr(driver, "_STABILITY_MAX_MS", 50)
    reads = {"n": 0}

    class GrowingLocator(FakeLocator):
        async def inner_text(self, timeout=None):
            reads["n"] += 1
            return "x" * reads["n"]  # never the same length twice

    page = FakePage(
        {
            "#composer": [FakeNode(visible=True, value="")],
            "#streaming": [FakeNode(visible=False)],
            "#answer": [FakeNode(text="grows")],
        }
    )
    original = page.locator
    page.locator = lambda s: (
        GrowingLocator([FakeNode(text="grows")]) if s == "#answer" else original(s)
    )

    result = _run(_request({}), page, monkeypatch)

    assert result.trace["completion"] == "text_never_stabilized", (
        "an answer that never settles ran to the deadline instead of a bounded cap"
    )
    # Bounded by the stability cap, NOT by the invocation deadline.
    assert result.trace["step"] == "done"


# --- the session can be bounced off the surface entirely --------------------------


def _bounced_page(url, title):
    """A page that redirects itself the moment the prompt is submitted."""
    page = FakePage(
        {
            "#composer": [FakeNode(visible=True, value="")],
            "#streaming": [_TransientNode()],
            "#answer": [],  # nothing to read: this is a different document now
        }
    )
    real_type = page.title

    async def bounced_title():
        return title if page.url != "about:blank" else await real_type()

    original_goto = page.goto

    async def goto(u, timeout=None, wait_until=None):
        result = await original_goto(u, timeout=timeout, wait_until=wait_until)
        return result

    page.goto = goto
    page.title = bounced_title
    # The keyboard press is what triggers the redirect on the real surface.
    original_press = page.keyboard.press

    async def press(key):
        await original_press(key)
        if key == "Enter":
            page.url = url

    page.keyboard.press = press
    return page


def test_being_bounced_to_a_cloudflare_interstitial_is_terminal_not_an_empty_answer(
    monkeypatch,
):
    """The live failure. The run finished on chatgpt.com/api/auth/error titled "Just a
    moment..." with all eight selector classes matching ZERO, and reported "no answer text
    matched the answer selector" - which is extraction_failed, which is RETRYABLE. So the
    consumer would keep buying paid sessions against a wall it can never get through.

    Neither the challenge nor the login selectors match Cloudflare's interstitial. The
    signals that do are the title and the URL, and no CSS selector can carry those.
    """
    page = _bounced_page("https://chatgpt.com/api/auth/error", "Just a moment...")
    result = _run(_request({}), page, monkeypatch)

    assert result.answer_text == ""
    assert result.challenge is True, "an interstitial was reported as a retryable empty answer"
    assert result.trace["blocked_page"] == "challenge"
    assert "Just a moment" in (result.error or "")
    assert result.trace["final_url"] == "https://chatgpt.com/api/auth/error"


def test_an_auth_path_with_no_interstitial_title_is_read_as_a_login_wall(monkeypatch):
    page = _bounced_page("https://chatgpt.com/api/auth/error", "Sign in")
    result = _run(_request({}), page, monkeypatch)

    assert result.login_wall is True
    assert result.challenge is False
    assert result.trace["blocked_page"] == "login"


def test_openais_real_login_wall_is_terminal_not_a_retryable_empty_answer(monkeypatch):
    """The live failure of 2026-08-06, and the reason the marker list stopped being
    substrings.

    Two of six chatgpt.com ground-truth sessions finished on
    `https://auth.openai.com/log-in-or-create-account` titled "Log in or sign up -
    OpenAI". That is a hard wall, but `_classify_blocked_page` returned None for it: the
    old `_AUTH_PATH_MARKERS` were `/auth/error`, `/auth/login`, `/api/auth`, `/login`,
    and `/log-in-or-create-account` contains NONE of them, because `log-in` is not
    `login`.

    So the run reported `extraction_failed`, which the consumer has in `RETRYABLE_CODES`,
    and every walled prompt bought a SECOND paid session against the same wall. The title
    is not a Cloudflare marker either, so nothing else caught it.
    """
    page = _bounced_page(
        "https://auth.openai.com/log-in-or-create-account", "Log in or sign up - OpenAI"
    )
    result = _run(_request({}), page, monkeypatch)

    assert result.login_wall is True, (
        "a hard login wall was reported as a retryable empty answer, which buys another "
        "paid session against the same wall"
    )
    assert result.challenge is False
    assert result.trace["blocked_page"] == "login"
    assert result.trace["final_url"] == "https://auth.openai.com/log-in-or-create-account"


def test_an_auth_host_is_a_wall_whatever_the_path_is_called(monkeypatch):
    """The host check is the half that does not depend on guessing a vendor's spelling.

    `/log-in-or-create-account` was unguessable; `auth.` as the first hostname label was
    not. A surface that renames the path tomorrow is still caught.
    """
    page = _bounced_page("https://auth.openai.com/totally-new-path", "Sign in")
    result = _run(_request({}), page, monkeypatch)

    assert result.login_wall is True
    assert result.trace["blocked_page"] == "login"


def test_a_question_slug_about_logging_in_is_not_read_as_a_login_wall(monkeypatch):
    """The false positive that segment-EQUALITY matching exists to prevent, and the
    reason this is not just a longer substring list.

    Perplexity puts the question itself in the answer URL as a slug, so a customer asking
    anything about signing in yields `/search/login-problems-with-…`.

    ⚠️ The answer is deliberately EMPTY here, and that is the whole test. `_drive` only
    consults `_classify_blocked_page` when nothing was extracted, so a version of this
    with an answer present never reaches the classifier and passes no matter what the
    matching rule is — it was written that way first and survived the mutation.

    Empty-answer-on-a-slug-URL is not hypothetical: it is exactly what perplexity.ai did
    on 2026-08-06, reaching `/search/<id>` titled with our own prompt while `div.prose`
    matched zero. Under substring matching, the same miss on a slug URL would be
    upgraded from a retryable extraction failure to a TERMINAL login wall — the surface
    silently stops being measured and reports a wall that was never there. A missed wall
    costs one more session; this costs the measurement itself.
    """
    # The slug must START with the bare word, so the URL literally contains "/login" —
    # otherwise this passes under the old substring list too and proves nothing.
    page = _bounced_page(
        "https://www.perplexity.ai/search/login-problems-with-my-router-x7f2",
        "login problems with my router",
    )
    result = _run(_request({}), page, monkeypatch)

    assert result.login_wall is False, (
        "a slug containing the word 'login' was read as a wall, which is terminal and "
        "would stop the surface being measured at all"
    )
    assert result.challenge is False
    assert "blocked_page" not in result.trace
    assert result.error == "no answer text matched the answer selector"


def test_an_auth_path_on_an_ordinary_host_is_still_a_wall(monkeypatch):
    """The path list carries its own weight, independently of the host check.

    Without this, `_AUTH_PATH_SEGMENTS` could be emptied entirely and every test would
    still pass — `auth.openai.com` is caught by its hostname. This is the case that
    breaks if OpenAI moves the same page onto the surface's own domain.
    """
    page = _bounced_page("https://chatgpt.com/log-in-or-create-account", "Log in")
    result = _run(_request({}), page, monkeypatch)

    assert result.login_wall is True
    assert result.trace["blocked_page"] == "login"


def test_a_normal_conversation_url_is_not_treated_as_a_bounce(monkeypatch):
    """The critical negative. chatgpt.com navigates from / to /c/<id> on submitting, so a
    URL change is the SUCCESS path - a check that flagged any navigation would fail every
    healthy run."""
    page = _bounced_page("https://chatgpt.com/c/abc123", "Best auto repair in Franklin")
    page.dom["#answer"] = [FakeNode(text="Franklin Auto Care is well reviewed.")]
    result = _run(_request({}), page, monkeypatch)

    assert result.answer_text == "Franklin Auto Care is well reviewed."
    assert result.login_wall is False
    assert result.challenge is False
    assert "blocked_page" not in result.trace


def test_an_empty_answer_on_the_surface_itself_stays_retryable(monkeypatch):
    """A slow render is genuinely worth another attempt, so it must NOT be swept into the
    terminal bucket - that would strand a run the surface would have answered."""
    page = _bounced_page("https://chatgpt.com/c/abc123", "ChatGPT")
    result = _run(_request({}), page, monkeypatch)

    assert result.login_wall is False
    assert result.challenge is False
    assert result.error == "no answer text matched the answer selector"


def test_the_url_is_recorded_at_submit_and_at_completion(monkeypatch):
    """Both, because a navigation also satisfies wait_for("hidden") - the element is gone
    because the whole document is. The live run recorded
    completion=streaming_selector_hidden while being bounced, so the strongest completion
    signal we have was reporting a finished answer for a destroyed page."""
    page = _bounced_page("https://chatgpt.com/api/auth/error", "Just a moment...")
    result = _run(_request({}), page, monkeypatch)

    assert result.trace["url_at_submit"] == "https://chatgpt.com/"
    assert result.trace["url_at_completion"] == "https://chatgpt.com/api/auth/error"
    assert result.trace["url_at_submit"] != result.trace["url_at_completion"]


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


def test_discovery_submits_with_enter_rather_than_clicking_a_broad_match(monkeypatch):
    """Discovery's submit selector is deliberately broad, and its first visible match on
    the live chatgpt.com page is "Open sidebar". Clicking it marks the prompt submitted and
    sends nothing - so the run would wait out its budget for an answer to a question it
    never asked."""
    send = FakeNode(visible=True, text="Open sidebar")
    page = FakePage(
        {
            "#send": [send],
            "#composer": [FakeNode(visible=True, value="")],
            "#streaming": [_TransientNode()],
            "#answer": [FakeNode(text="An answer.")],
        }
    )
    result = _run(_request({}, discover=True), page, monkeypatch)

    assert send.clicked is False
    assert result.trace["submit_method"] == "enter"
    assert result.trace["submit_forced_to_enter_for_discovery"] == "#send"


def test_a_normal_run_still_clicks_its_submit_button(monkeypatch):
    """Scoped to discovery only: a real run has a precise selector and should use it."""
    send = FakeNode(visible=True)
    page = FakePage(
        {
            "#send": [send],
            "#composer": [FakeNode(visible=True, value="")],
            "#streaming": [_TransientNode()],
            "#answer": [FakeNode(text="An answer.")],
        }
    )
    result = _run(_request({}), page, monkeypatch)

    assert send.clicked is True
    assert result.trace["submit_method"] == "button"


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
    assert page.discovery_dumps == 0


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
