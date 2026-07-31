# aeo-groundtruth-browser-runtime

The AWS Bedrock **AgentCore Runtime** container behind the AEO platform's Tier-3
"ground truth" measurement. It opens an AgentCore Browser session, optionally routed
through a geo-targeted residential proxy, drives a consumer AI surface
(chatgpt.com / perplexity.ai), and returns the answer plus evidence of where it
actually egressed from.

**Consumer:** `aeo-agent-service` → `app/adapters/sov/ground_truth_adapter.py`.
That repo builds the request, holds the DOM selectors, and normalizes the response.

## Why this exists as its own deployable

Tier 3 answers "what does a real customer in Franklin, TN see when they ask an AI for a
local mechanic?" No API call can answer that, because the answer depends on where the
asker is. So it has to be a real browser in a real place.

The browser is **remote** — it runs in the managed AgentCore Browser service, not here.
This container is the agent that drives it. That is why the image ships **no Chromium**:
Playwright attaches over CDP with `connect_over_cdp`, which needs only the pip package.
Installing browsers here would add hundreds of megabytes for a binary never launched.

## The four rules

Each one exists because breaking it produces a plausible number rather than an error,
and this feeds the tier the product presents to customers as trustworthy.

1. **The egress self-check runs from inside the browser page.** Measuring it with
   `httpx` from this container would report the container's own AWS egress and so always
   agree with itself — a check that can never fail, which is worse than no check. The
   proxy is a Chromium `--proxy-server` flag; only the page sees it.
2. **`stop_browser_session` runs in a `finally`, always.** The consumer's abort can only
   cancel the *runtime invocation*. The browser session is created here, so this is the
   only code that can release it, and a leak is a paid browser left running.
3. **Completion is the streaming indicator disappearing, not a sleep.** A fixed wait
   either truncates a long answer — and a partial answer scores as a complete one — or
   burns paid session time on every short one.
4. **An empty answer is never returned as an unexplained success.** The consumer retries
   an unexplained empty answer, buying a second paid session against the same wall.

## Why `observed_egress` is the most important field

AWS documents the browser proxy as *"a browser-level setting … not a network-level
control"* that *"does not guarantee that all traffic will transit the proxy"*, and does
**not** validate proxy connectivity at session creation. It is fail-open.

So the failure mode produces no error: the browser quietly egresses from AWS, the
surface returns a perfectly good answer about the wrong metro, and the consumer files it
as ground truth for a town it never visited. Nothing downstream can detect that. This
runtime therefore reports the city/region/IP it observed, and the consumer fails the job
when they disagree.

## Wire contract

Request (`POST /invocations`). The `{"input": {...}}` wrapper used by AWS's examples is
also accepted — AgentCore passes payload bytes through verbatim, so the wrapper is a
convention rather than a platform requirement.

```json
{
  "prompt": "Who are the best auto repair shops in Franklin, TN?",
  "surface": "chatgpt.com",
  "url": "https://chatgpt.com/",
  "selectors": { "input": "...", "submit": "...", "answer": "...", "streaming": "...",
                 "consent": [], "login_wall": [], "challenge": [], "citation": [] },
  "proxy": { "server": "brd.superproxy.io", "port": 22225,
             "secret_arn": "arn:aws:secretsmanager:...:secret:brightdata-franklin-tn" },
  "proxy_target": "Franklin, TN US (35.9251,-86.8689)"
}
```

**No credential appears anywhere in that payload, and none can.** AgentCore Browser
accepts `externalProxy.credentials.basicAuth.secretArn` and reads Secrets Manager
itself; an inline credential is not expressible in the API. The geo targeting that makes
Tier 3 meaningful is encoded in the Bright Data *username*, which lives inside the
secret and never transits this service. One town = one username = one secret.

Response:

```json
{
  "answer_text": "...",
  "citations": [{"url": "https://...", "title": "..."}],
  "login_wall": false,
  "challenge": false,
  "observed_egress": {"city": "Franklin", "region": "Tennessee", "ip": "68.51.x.x",
                      "source": "ipinfo.io"},
  "error": null,
  "trace": {"input_method": "fill", "submit_method": "enter", "completion": "..."}
}
```

Always HTTP 200, including on failure: a 5xx reaches the consumer as an opaque boto3
error with no page state and no observed egress, and its retry predicate cannot tell a
login wall from a crash. `error` and `trace` are the diagnosis channel — `trace` records
how the prompt was typed, how it was submitted, and how completion was detected, so an
empty answer points at the selector that silently no-opped.

## Development

```bash
python -m venv .venv && ./.venv/Scripts/python.exe -m pip install -r requirements.txt
./.venv/Scripts/python.exe -m pip install pytest httpx
./.venv/Scripts/python.exe -m pytest tests/ -q      # no AWS needed
./.venv/Scripts/python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8080
```

Build and run the real ARM64 image locally (Docker Desktop emulates arm64):

```bash
docker buildx build --platform linux/arm64 --provenance=false -t aeo-groundtruth-browser:arm64 --load .
docker run --rm --platform linux/arm64 -p 8081:8080 aeo-groundtruth-browser:arm64
curl http://localhost:8081/ping
```

## Deploy

```bash
python scripts/provision.py --check   # read-only inventory
python scripts/provision.py           # ECR repo, IAM role, ARM64 build+push, runtime
python scripts/smoke_invoke.py --arn <arn> --discover
```

`provision.py` is idempotent and safe to re-run; re-running after a code change pushes a
new image and updates the runtime to that digest. Deploys go by **digest, not tag**, so
the image a quarterly job runs cannot silently be whatever was pushed last.

**Deploying currently requires access this project does not have** — see
[`docs/PERMISSIONS-REQUEST.md`](docs/PERMISSIONS-REQUEST.md). `ecr:CreateRepository`,
`ecr:InitiateLayerUpload` and `iam:CreateRole` are all denied for the developer account,
so either an administrator runs `provision.py` once, or the scoped policy in that
document is granted. The script degrades on purpose: it reports every blocker in one run
and leaves nothing half-created.

Target account/region is `082585646836` / `us-east-1`, matching the existing
`AgentCore-aeoskills-production` stack. Those two runtimes are the proven pattern this
mirrors, so the provisioning here can be folded into that CDK app later without
re-deciding anything.

## Cost

Browser sessions are the cost — roughly $1.50 each, versus a fraction of a cent for an
API call. The feature runs quarterly at 10 prompts x 2 surfaces. Spend limits live in
the **consumer**, not here: a hard cap of 60 sessions per run, 5 concurrent per pod, and
staggered starts. This container deliberately runs a single uvicorn worker, because
extra workers would multiply that cap on the side of the system that cannot see it.
