"""Invoke the deployed runtime once and print the envelope. Costs one browser session.

This is the script that answers the questions no amount of local testing can:

1. Does the response envelope actually look like what aeo-agent-service expects? Its
   normalizer accepts several spellings for the answer key precisely because nobody had
   ever seen a real payload. **The output of this script is what lets those guesses be
   deleted**, and until they are, an unrecognised shape yields an empty answer that
   scores as "the AI never mentioned this business".
2. What are the real DOM selectors? Every selector in aeo-agent-service's
   `_browser_surfaces.py` is a `PLACEHOLDER` and cannot be written from documentation -
   chatgpt.com and perplexity.ai ship DOM changes without notice. `--discover` dumps
   what is actually on the page.

Usage:
    python scripts/smoke_invoke.py --arn <runtime-arn>
    python scripts/smoke_invoke.py --arn <arn> --surface perplexity.ai
    python scripts/smoke_invoke.py --arn <arn> --discover   # dump candidate selectors

**Runs WITHOUT a proxy by default.** That is deliberate and it is the whole reason this
is useful before Bright Data exists: the browser automation, the envelope and the
selectors can all be verified from an AWS egress. `observed_egress` will report an AWS
region, so aeo-agent-service would reject the result as a geography mismatch - that is
the check working, not a failure of this script. Pass --proxy-server/--proxy-secret once
credentials exist to test the real path.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid

import boto3
from botocore.config import Config as BotoConfig

REGION = "us-east-1"

# The placeholder selectors from aeo-agent-service, with the PLACEHOLDER prefix
# stripped. They are a starting guess only - confirming or replacing them is the point.
SURFACES = {
    "chatgpt.com": {
        "url": "https://chatgpt.com/",
        "selectors": {
            "input": "#prompt-textarea",
            "submit": "[data-testid='send-button']",
            "answer": "[data-message-author-role='assistant']",
            "streaming": "[data-testid='stop-button']",
            "consent": ["[data-testid='cookie-accept']"],
            "login_wall": ["[data-testid='login-button']"],
            "challenge": ["#challenge-form"],
            "citation": ["a[data-citation]"],
        },
    },
    "perplexity.ai": {
        "url": "https://www.perplexity.ai/",
        "selectors": {
            "input": "textarea[placeholder]",
            "submit": "button[aria-label='Submit']",
            "answer": "[class*='prose']",
            "streaming": "[class*='animate-pulse']",
            "consent": ["button:has-text('Accept')"],
            "login_wall": ["[data-testid='signin-modal']"],
            "challenge": ["#cf-challenge"],
            "citation": ["a[class*='citation']"],
        },
    },
}

#: For --discover. Deliberately broad and surface-agnostic: the job is to find what
#: exists, not to confirm what we guessed.
#:
#: These must be plain CSS. The runtime's dump uses `querySelectorAll` so that it reports
#: raw DOM truth rather than Playwright's filtered view, which means Playwright-only
#: engines (`:has-text()`, `visible=`) come back as `unsupported` rather than as matches.
#:
#: `login_wall` and `challenge` are narrow on purpose even here. They are the two classes
#: the runtime ACTS on before typing anything, so a broad match (the old
#: `[href*='login'], [href*='auth'], button`) makes every page look walled and the run
#: returns "login wall" having never reached the composer.
DISCOVERY_SELECTORS = {
    "input": "textarea, [contenteditable='true'], input[type='text']",
    "submit": "button[data-testid], button[aria-label], button[type='submit']",
    "answer": "[data-message-author-role], [class*='prose'], [class*='markdown'], main article",
    "streaming": "[data-testid*='stop'], [class*='animate'], [aria-busy='true']",
    "consent": ["button"],
    "login_wall": ["[data-testid*='login'], [data-testid*='signin']"],
    "challenge": ["#cf-challenge, [class*='captcha'], iframe[title*='challenge']"],
    "citation": ["a[href^='http']"],
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--arn", required=True, help="agentRuntimeArn")
    parser.add_argument("--surface", default="chatgpt.com", choices=sorted(SURFACES))
    parser.add_argument(
        "--prompt", default="Who are the best auto repair shops in Franklin, TN?"
    )
    parser.add_argument(
        "--discover",
        action="store_true",
        help="use broad selectors to find what is on the page, rather than our guesses",
    )
    parser.add_argument("--proxy-server")
    parser.add_argument("--proxy-port", type=int, default=22225)
    parser.add_argument("--proxy-secret", help="Secrets Manager ARN")
    parser.add_argument("--proxy-target", help='e.g. "Franklin, TN US (35.9251,-86.8689)"')
    args = parser.parse_args()

    surface = SURFACES[args.surface]
    payload = {
        "prompt": args.prompt,
        "surface": args.surface,
        "url": surface["url"],
        "selectors": DISCOVERY_SELECTORS if args.discover else surface["selectors"],
        # Broad selectors alone were never enough: they made the drive fail differently
        # rather than making the page legible. This flag is what asks the runtime for an
        # inventory of what matched, independently of whether the drive succeeds.
        "discover": args.discover,
    }
    if args.proxy_server:
        payload["proxy"] = {
            "server": args.proxy_server,
            "port": args.proxy_port,
            "secret_arn": args.proxy_secret,
        }
        payload["proxy_target"] = args.proxy_target
    else:
        print(
            "NOTE: no proxy - this will egress from AWS, so observed_egress will report "
            "an AWS region and aeo-agent-service would reject the result as a geography "
            "mismatch. That is correct behaviour, not a failure.\n"
        )

    # Explicit timeout, and retries OFF. botocore defaults to a 60s read timeout with
    # retries enabled, while the runtime's own deadline is 165s — so the default config
    # gives up mid-invocation and retries, and **each retry opens another paid browser
    # session while the previous one is still driving the page.** The first perplexity.ai
    # run produced FIVE sessions at exactly 60s intervals before the script died, two of
    # them still billing afterwards. The traceback blames a read timeout and says nothing
    # about the four extra browsers.
    client = boto3.client(
        "bedrock-agentcore",
        region_name=REGION,
        config=BotoConfig(
            read_timeout=240,
            connect_timeout=15,
            retries={"total_max_attempts": 1},
        ),
    )
    response = client.invoke_agent_runtime(
        agentRuntimeArn=args.arn,
        # Must be 33+ characters, which is why two hex UUIDs are concatenated. A short
        # id is rejected by the API, not silently padded.
        runtimeSessionId=f"{uuid.uuid4().hex}{uuid.uuid4().hex}",
        payload=json.dumps(payload).encode("utf-8"),
        qualifier="DEFAULT",
    )

    body = response["response"].read()
    try:
        envelope = json.loads(body)
    except json.JSONDecodeError:
        print("The runtime did not return JSON. Raw body:")
        print(body[:4000])
        return 1

    # Discovery is printed on its own below; inlining it here buries the envelope.
    discovery = envelope.pop("discovery", None)
    print("=== ENVELOPE ===")
    print(json.dumps(envelope, indent=2)[:8000])
    print()

    if discovery:
        _print_discovery(discovery)

    print("=== WHAT TO DO WITH THIS ===")
    trace = envelope.get("trace") or {}
    step = trace.get("step")
    if step and step != "done":
        print(f"failed at step  : {step}")
    egress = envelope.get("observed_egress")
    print(f"observed_egress : {egress}")
    if not egress:
        # The old wording asserted the session had no internet access. On the first live
        # run that was flatly wrong - the page had loaded ChatGPT - because the envelope
        # had DISCARDED a successful reading when a later step raised. Now that the
        # reading survives, absence means absence, and `step` says whether we even
        # reached the check.
        if step in (None, "starting", "start_browser_session", "attach_cdp"):
            print("  ^ absent because the run never reached the egress check; see the")
            print("    step above, which is a session/attach problem, not a proxy one.")
        else:
            print("  ^ MISSING, and the check DID run: every IP-geolocation provider")
            print("    failed. aeo-agent-service treats that as a geography mismatch and")
            print("    fails the job, by design.")
    answer = envelope.get("answer_text") or ""
    print(f"answer_text     : {len(answer)} chars")
    if not answer:
        print("  ^ EMPTY. Read `trace` above: `<class>_matched` vs `<class>_visible`")
        print("    separates 'the selector matched nothing' from 'it matched only")
        print("    hidden nodes', and `input_readback` says whether the prompt ever")
        print("    landed in the composer.")
    print(f"citations       : {len(envelope.get('citations') or [])}")
    print(f"login_wall      : {envelope.get('login_wall')}")
    print(f"challenge       : {envelope.get('challenge')}")
    print(f"error           : {envelope.get('error')}")
    print()
    print("Top-level keys, which is what pins the envelope in aeo-agent-service and")
    print("lets its alternate key spellings (_ANSWER_KEYS, _CITATION_KEYS) be deleted:")
    print(f"  {sorted([*envelope, 'discovery'])}")
    return 0


def _print_discovery(discovery: dict) -> None:
    """Summarise the inventory as selector candidates, ranked by usefulness.

    Printed rather than dumped: the raw JSON for `a[href^='http']` alone is hundreds of
    lines, and the decision being made is "which selector goes into
    `_browser_surfaces.py`", which needs the visible candidates and their distinguishing
    attributes, not every node.
    """
    for phase, dump in discovery.items():
        print(f"=== DISCOVERY [{phase}] {dump.get('title')!r} {dump.get('url')} ===")
        for cls in ("input", "submit", "answer", "streaming", "consent", "login_wall",
                    "challenge", "citation"):
            results = dump.get(cls) or {}
            for selector, res in results.items():
                if "unsupported" in res:
                    print(f"  {cls:<11} {selector}\n    !! not valid CSS: {res['unsupported']}")
                    continue
                if "error" in res:
                    print(f"  {cls:<11} {selector}\n    !! {res['error']}")
                    continue
                matched, visible = res.get("matched", 0), res.get("visible", 0)
                print(f"  {cls:<11} {selector}")
                print(f"    matched={matched} visible={visible}")
                for node in res.get("sample") or []:
                    if not node.get("visible") and visible:
                        # Hidden nodes are only worth printing when NOTHING is visible -
                        # that is the case where the hidden one is the whole finding.
                        continue
                    mark = "*" if node.get("visible") else "x"
                    bits = [f"<{node['tag']}>"]
                    if node.get("id"):
                        bits.append(f"#{node['id']}")
                    if node.get("box"):
                        bits.append(node["box"])
                    for k, v in (node.get("attrs") or {}).items():
                        bits.append(f"{k}={v!r}")
                    if node.get("cls"):
                        bits.append(f"class={node['cls'][:60]!r}")
                    if node.get("text"):
                        bits.append(f"text={node['text'][:60]!r}")
                    print(f"    {mark} " + " ".join(bits))
        print()


if __name__ == "__main__":
    sys.exit(main())
