"""Drive one consumer web UI through an AgentCore browser session.

Read the four rules before changing anything here. Each one exists because breaking
it produces a plausible number rather than an error, and this runtime feeds the tier
the product presents to customers as the trustworthy one.

1. **The egress self-check must run from inside the BROWSER PAGE.** Measuring it with
   `httpx` or `boto3` from this container would report the container's own AWS egress
   and therefore always agree with itself — a check that can never fail, which is
   worse than no check because it looks like assurance. The proxy is a Chromium
   `--proxy-server` flag; only the page sees it.
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
from playwright.async_api import Page, async_playwright

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


class DeadlineExceeded(Exception):
    """Our own budget ran out. Distinct from a Playwright timeout on one step."""


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


async def _dismiss_consent(page: Page, selectors: list[str], trace: dict) -> None:
    """Click away cookie/consent walls.

    On a residential exit in a new geography these are the norm rather than an edge
    case, which is why failing to find one is not an error: absent is the common case.
    """
    dismissed = []
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if await locator.is_visible(timeout=_CONSENT_TIMEOUT_MS):
                await locator.click(timeout=_CONSENT_TIMEOUT_MS)
                dismissed.append(selector)
        except Exception:  # noqa: BLE001 - absence is the normal case
            continue
    trace["consent_dismissed"] = dismissed


async def _first_visible(page: Page, selectors: list[str]) -> str | None:
    for selector in selectors:
        try:
            if await page.locator(selector).first.is_visible(timeout=1_000):
                return selector
        except Exception:  # noqa: BLE001
            continue
    return None


async def _enter_prompt(page: Page, sel: Selectors, prompt: str, trace: dict) -> None:
    """Type the prompt and submit it.

    Two fallbacks, both load-bearing across these surfaces. `fill` works on a
    `textarea`/`input` but silently does nothing useful on the contenteditable div
    ChatGPT uses, so a click-then-type path is required; and typing (rather than
    inserting) is what emits the key events an SPA needs before it will enable its own
    send button. Submitting via Enter is the fallback because a send button is often
    disabled until those events fire.
    """
    field = page.locator(sel.input).first
    await field.wait_for(state="visible", timeout=_NAV_TIMEOUT_MS)
    try:
        await field.fill(prompt, timeout=10_000)
        trace["input_method"] = "fill"
    except Exception:  # noqa: BLE001
        await field.click(timeout=10_000)
        await page.keyboard.type(prompt, delay=5)
        trace["input_method"] = "click+type"

    submitted = False
    if sel.submit:
        try:
            button = page.locator(sel.submit).first
            if await button.is_visible(timeout=5_000) and await button.is_enabled():
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
    """
    if sel.streaming:
        try:
            await page.locator(sel.streaming).first.wait_for(
                state="visible",
                timeout=deadline.remaining_ms(_STREAM_APPEAR_TIMEOUT_MS),
            )
            trace["stream_appeared"] = True
            await page.locator(sel.streaming).first.wait_for(
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


async def _drive(page: Page, req: InvocationRequest, deadline: _Deadline) -> InvocationResponse:
    trace: dict[str, Any] = {"surface": req.surface}
    sel = req.selectors

    # FIRST, before the target surface is ever loaded. If the session is egressing
    # from the wrong place, nothing observed afterwards is worth having, and finding
    # out before we spend the remaining budget is free.
    egress = await _read_egress(page, deadline)
    trace["egress_source"] = egress.source if egress else None

    await page.goto(
        req.url, timeout=deadline.remaining_ms(_NAV_TIMEOUT_MS), wait_until="domcontentloaded"
    )
    await _dismiss_consent(page, sel.consent, trace)

    blocked_by = await _first_visible(page, sel.login_wall)
    if blocked_by:
        trace["login_wall_selector"] = blocked_by
        return InvocationResponse(
            login_wall=True, observed_egress=egress, trace=trace,
            error=f"login wall matched {blocked_by!r}",
        )
    challenged_by = await _first_visible(page, sel.challenge)
    if challenged_by:
        trace["challenge_selector"] = challenged_by
        return InvocationResponse(
            challenge=True, observed_egress=egress, trace=trace,
            error=f"challenge matched {challenged_by!r}",
        )

    await _enter_prompt(page, sel, req.prompt, trace)
    await _await_completion(page, sel, deadline, trace)

    answer = await _read_answer(page, sel)
    citations = await _read_citations(page, sel)

    # A wall can appear AFTER the prompt is submitted — several surfaces allow one
    # anonymous question and then demand an account. Re-checking here is what stops
    # that being reported as "the AI answered with nothing".
    if not answer:
        blocked_by = await _first_visible(page, sel.login_wall)
        if blocked_by:
            trace["login_wall_selector"] = blocked_by
            return InvocationResponse(
                login_wall=True, observed_egress=egress, trace=trace,
                error=f"login wall appeared after submitting; matched {blocked_by!r}",
            )
        challenged_by = await _first_visible(page, sel.challenge)
        if challenged_by:
            trace["challenge_selector"] = challenged_by
            return InvocationResponse(
                challenge=True, observed_egress=egress, trace=trace,
                error=f"challenge appeared after submitting; matched {challenged_by!r}",
            )

    return InvocationResponse(
        answer_text=answer,
        citations=citations,
        observed_egress=egress,
        trace=trace,
        # Rule 4: an empty answer never leaves here unexplained.
        error=None if answer else "no answer text matched the answer selector",
    )


async def run_invocation(req: InvocationRequest, region: str) -> InvocationResponse:
    """Open a session, drive the surface, and always release the session."""
    deadline = _Deadline(req.timeout_seconds)
    client = BrowserClient(region=region)
    proxy_configuration = build_proxy_configuration(req)

    # `start` and `stop` are blocking boto3 calls. They run in a worker thread because
    # this coroutine shares its event loop with the `/ping` handler, and AgentCore
    # health-checks that endpoint: a multi-second blocking call here would make a
    # working runtime look unhealthy while it is doing exactly what it was asked to.
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
    try:
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
                return await _drive(page, req, deadline)
            finally:
                await browser.close()
    except DeadlineExceeded as exc:
        logger.error("invocation exceeded its budget: %s", exc)
        return InvocationResponse(error=f"timeout: {exc}")
    except Exception as exc:  # noqa: BLE001 - the envelope IS the error channel
        # Returned rather than raised, so the consumer parses a real envelope. A 500
        # reaches it as an opaque boto3 error with no page state and no observed
        # egress, which is strictly less than this.
        logger.exception("invocation failed")
        return InvocationResponse(error=f"{type(exc).__name__}: {exc}")
    finally:
        # Rule 2. This is the only place that can release the browser session: the
        # consumer's abort cancels the runtime invocation, which cannot reach in here.
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
