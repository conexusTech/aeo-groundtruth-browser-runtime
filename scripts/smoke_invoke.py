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
DISCOVERY_SELECTORS = {
    "input": "textarea, [contenteditable='true'], input[type='text']",
    "submit": "button[data-testid], button[aria-label], button[type='submit']",
    "answer": "[data-message-author-role], [class*='prose'], [class*='markdown'], main article",
    "streaming": "[data-testid*='stop'], [class*='animate'], [aria-busy='true']",
    "consent": ["button"],
    "login_wall": ["[href*='login'], [href*='auth'], button"],
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

    client = boto3.client("bedrock-agentcore", region_name=REGION)
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

    print("=== ENVELOPE ===")
    print(json.dumps(envelope, indent=2)[:8000])
    print()
    print("=== WHAT TO DO WITH THIS ===")
    egress = envelope.get("observed_egress")
    print(f"observed_egress : {egress}")
    if not egress:
        print("  ^ MISSING. aeo-agent-service treats this as a geography mismatch and")
        print("    fails the job, by design. Check the runtime's CloudWatch logs: every")
        print("    IP-geolocation provider must have failed, which usually means the")
        print("    session could not reach the internet at all.")
    answer = envelope.get("answer_text") or ""
    print(f"answer_text     : {len(answer)} chars")
    if not answer:
        print("  ^ EMPTY. Read `trace` above: it records how the prompt was typed, how")
        print("    it was submitted, and how completion was detected. The step that")
        print("    silently no-opped is the selector that needs replacing.")
    print(f"citations       : {len(envelope.get('citations') or [])}")
    print(f"login_wall      : {envelope.get('login_wall')}")
    print(f"challenge       : {envelope.get('challenge')}")
    print(f"error           : {envelope.get('error')}")
    print()
    print("Top-level keys, which is what pins the envelope in aeo-agent-service and")
    print("lets its alternate key spellings (_ANSWER_KEYS, _CITATION_KEYS) be deleted:")
    print(f"  {sorted(envelope)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
