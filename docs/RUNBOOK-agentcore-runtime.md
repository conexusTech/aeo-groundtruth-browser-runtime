# Runbook — creating the AgentCore browser runtime by hand

Every command here was run for real against account `082585646836` on 2026-08-03, and the
runtime it produces is the one currently serving Tier-3 ground truth. `scripts/provision.py`
automates all of it; this document is what the script does, in AWS CLI form, so it can be
done manually, handed to an administrator, or folded into the `AgentCore-aeoskills-production`
CDK app later.

**Read § "The eight things that will bite you" before running anything.** Most of it is not
inferable from AWS's documentation, and three of the items cost a round trip each with the
account administrator.

---

## 0. What you are actually building

AgentCore gives you **two** resources, and only one of them is yours to build:

| | What | Who builds it |
|---|---|---|
| **Browser** | `aws.browser.v1` — the managed Chromium AgentCore drives | **AWS.** Nothing to build, nothing to deploy. |
| **Runtime** | An ARM64 container serving `POST /invocations` + `GET /ping` on `:8080` | **You.** This repo *is* that container. |

The plan's original premise ("AgentCore is already provisioned, build straight through")
conflated the two. `aws bedrock-agentcore-control list-agent-runtimes` confirmed nothing
existed. If you take one thing from this document: *"AgentCore is provisioned" means the
platform, not your agent.*

### Fixed values used throughout

```
ACCOUNT   082585646836
REGION    us-east-1          # NOT us-east-2, which is only the CLI default here
ECR REPO  aeo-groundtruth/browser
ROLE      AmazonBedrockAgentCoreAEOGroundTruthBrowserRole
RUNTIME   aeo_groundtruth_browser
```

The region matters: every existing AgentCore resource in this account lives in `us-east-1`.
A runtime in another region cannot see them and the browser session would be cross-region.

---

## 1. Prerequisites

```powershell
aws sts get-caller-identity          # confirm the account, and WHICH identity
docker version                       # Docker Desktop must be RUNNING (it builds)
docker buildx version                # buildx is required for --platform
```

The caller needs the permissions in `docs/policy-caller.json` — one self-contained policy,
7 statements. Attach that and every step below works; it was applied on 2026-08-03 and the
whole run succeeded first try. `docs/PERMISSIONS-REQUEST.md` explains each statement for
whoever approves it.

### What is self-serve today, and what still needs an administrator

`leo.lindo` holds `policy-caller.json`, so **AgentCore work is ours to do directly** — no
hand-off, no ticket. Verified by attempting each call, not inferred:

| Capability | Status |
|---|---|
| `bedrock-agentcore:*` — create/update/delete/invoke runtimes, browser sessions | ✅ **in `us-east-1` only** (the policy carries an `aws:RequestedRegion` condition) |
| ECR create/push/pull | ✅ but scoped to `repository/aeo-groundtruth/*` |
| Secrets Manager create/read | ✅ but scoped to `secret:brightdata-*` |
| IAM **read** (`GetRole`, `ListRoles`, `ListRolePolicies`, `GetRolePolicy`) | ✅ |
| `iam:PassRole` | ✅ but only for `AmazonBedrockAgentCoreAEOGroundTruthBrowserRole` |
| **`iam:CreateRole` / `PutRolePolicy` / `DeleteRole`** | 🛑 **denied** |

So the three things that make a *new* AgentCore feature need an administrator:

1. **A new execution role.** Reuse `AmazonBedrockAgentCoreAEOGroundTruthBrowserRole` where
   its permissions fit, and the whole feature stays self-serve. A genuinely different one
   (different secrets, a model invocation, a new AWS service) needs the role created for us
   — send the two policy documents, as in step 4.
2. **A different region.** The `aws:RequestedRegion: us-east-1` condition denies everything
   else. Put new AgentCore work in `us-east-1` unless there is a reason not to.
3. **A different ECR namespace or secret prefix.** Both grants are scoped. Keeping new
   images under `aeo-groundtruth/*` and new secrets under `brightdata-*` avoids a grant;
   anything else needs the resource ARN widened.

> The *runtime's* execution role (step 3) is a different thing from the *caller's*
> permissions. Confusing the two is the most common way this gets stuck: the caller needs
> to create and pass the role; the role needs to drive browser sessions and read secrets.

---

## 2. Create the ECR repository

```powershell
aws ecr create-repository `
  --repository-name aeo-groundtruth/browser `
  --region us-east-1 `
  --image-scanning-configuration scanOnPush=true `
  --image-tag-mutability IMMUTABLE
```

Check instead of creating:

```powershell
aws ecr describe-repositories --repository-names aeo-groundtruth/browser --region us-east-1
```

> ⚠️ **The live repo is `MUTABLE` with `scanOnPush=false`,** not what the command above
> asks for. It was created before those flags were added to `provision.py`, and the script
> returns early when the repo already exists rather than reconciling its settings. So
> re-pushing an existing tag *succeeds* today. Deploy by digest (step 4) and it does not
> matter; if you want immutability, set it explicitly on the existing repo — the script
> will not do it for you.

---

## 3. Build the ARM64 image and push it

```powershell
# Log Docker in to ECR (token is valid 12h)
aws ecr get-login-password --region us-east-1 | `
  docker login --username AWS --password-stdin 082585646836.dkr.ecr.us-east-1.amazonaws.com

# Tag by git commit, never `latest` — see gotcha 4
$TAG = git rev-parse --short HEAD

docker buildx build `
  --platform linux/arm64 `
  --provenance=false `
  -t "082585646836.dkr.ecr.us-east-1.amazonaws.com/aeo-groundtruth/browser:$TAG" `
  --push .
```

Then read back the **digest**, which is what you deploy:

```powershell
aws ecr describe-images `
  --repository-name aeo-groundtruth/browser `
  --region us-east-1 `
  --image-ids imageTag=$TAG `
  --query 'imageDetails[0].imageDigest' --output text
```

Both flags are load-bearing:

- **`--platform linux/arm64`** — AgentCore Runtime is ARM64 only. An amd64 image builds
  and pushes fine and fails at **deploy**, so the platform is also pinned in the
  `Dockerfile`'s `FROM` line rather than left to the builder's host.
- **`--provenance=false`** — buildx attaches an OCI provenance attestation by default,
  which makes the pushed artifact a **manifest list**. AgentCore's image resolution rejects
  that. The error does not mention attestations.

The image is ~119 MB, and it should stay that way: **there is no Chromium in it.** The
browser is remote — this container attaches over CDP with `connect_over_cdp`, which needs
only the Playwright pip package and its bundled Node driver. `PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1`
enforces that. Adding `playwright install` would add hundreds of MB for a binary never
launched.

---

## 4. Create the execution role

Two documents. Save them as files — `file://` avoids every shell-quoting problem, which on
Windows is not a small thing.

`trust-policy.json`:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "Service": "bedrock-agentcore.amazonaws.com" },
    "Action": "sts:AssumeRole",
    "Condition": {
      "StringEquals": { "aws:SourceAccount": "082585646836" },
      "ArnLike": { "aws:SourceArn": "arn:aws:bedrock-agentcore:us-east-1:082585646836:*" }
    }
  }]
}
```

Those two conditions are confused-deputy guards. Without them **any** AgentCore resource in
**any** account could assume this role.

`permission-policy.json` — generate the exact current version rather than copying it here,
so it cannot drift from the code:

```powershell
.\.venv\Scripts\python.exe -c "import json;from scripts.provision import PERMISSION_POLICY;print(json.dumps(PERMISSION_POLICY,indent=2))" > permission-policy.json
```

It grants: `Start/Stop/Get/ListBrowserSession` + `ConnectBrowserAutomationStream`;
`secretsmanager:GetSecretValue` scoped to `secret:brightdata-*`; ECR pull; **four** separate
CloudWatch Logs statements; X-Ray; `cloudwatch:PutMetricData` namespaced to
`bedrock-agentcore`; and the runtime's own workload-identity tokens.

Deliberately **not** granted: `bedrock:InvokeModel` (this runtime calls no model — the
extraction LLM is in `aeo-agent-service` on the tenant's credentials) and
`GetWorkloadAccessTokenForUserId` (AWS advises against it; issues tokens from a
caller-supplied user id with no IdP verification).

```powershell
aws iam create-role `
  --role-name AmazonBedrockAgentCoreAEOGroundTruthBrowserRole `
  --assume-role-policy-document file://trust-policy.json `
  --description "Execution role for the AEO Tier-3 ground-truth browser runtime"

aws iam put-role-policy `
  --role-name AmazonBedrockAgentCoreAEOGroundTruthBrowserRole `
  --policy-name AmazonBedrockAgentCoreAEOGroundTruthBrowserRolePolicy `
  --policy-document file://permission-policy.json
```

**Then wait ~12 seconds.** IAM is eventually consistent and `CreateAgentRuntime` fails
validation if it cannot yet assume a brand-new role.

> **The `AmazonBedrockAgentCore` prefix is not cosmetic — do not "tidy" it.** AWS's own
> documented AgentCore policy scopes `iam:PassRole` to `role/AmazonBedrockAgentCore*` and
> role management to `*BedrockAgentCore*`. A differently-named role needs a bespoke policy
> written and reviewed; this name falls under statements an administrator has likely already
> approved. It is also why `BedrockAgentCoreFullAccess` would work for us out of the box.

---

## 5. Create the runtime

`artifact.json` — **by digest**, not by tag:

```json
{
  "containerConfiguration": {
    "containerUri": "082585646836.dkr.ecr.us-east-1.amazonaws.com/aeo-groundtruth/browser@sha256:PUT_THE_DIGEST_HERE"
  }
}
```

```powershell
aws bedrock-agentcore-control create-agent-runtime `
  --region us-east-1 `
  --agent-runtime-name aeo_groundtruth_browser `
  --description "AEO Tier-3 ground-truth browser runtime (geo-local SoV)" `
  --agent-runtime-artifact file://artifact.json `
  --role-arn arn:aws:iam::082585646836:role/AmazonBedrockAgentCoreAEOGroundTruthBrowserRole `
  --network-configuration '{"networkMode":"PUBLIC"}' `
  --lifecycle-configuration '{"idleRuntimeSessionTimeout":300,"maxLifetime":3600}' `
  --environment-variables AGENTCORE_REGION=us-east-1,LOG_LEVEL=INFO
```

Note the CLI service name is **`bedrock-agentcore-control`** for the control plane and
**`bedrock-agentcore`** for invocation. They are different services.

The lifecycle values are tighter than the account's existing `aeoskills` runtimes
(900/28800) on purpose: ground-truth invocations are minutes long and **quarterly**, so a
long idle window keeps a runtime warm for three months for nothing, and `maxLifetime` is a
backstop against a wedged session — which here means a *paid* browser.

Then wait for `READY`:

```powershell
aws bedrock-agentcore-control get-agent-runtime `
  --agent-runtime-id aeo_groundtruth_browser-fGhiSo82t0 `
  --region us-east-1 --query 'status' --output text
```

It went `CREATING` → `READY` in under 6 seconds. The response also gives you the
`workloadIdentityArn`, which AWS created implicitly (see gotcha 1).

---

## 6. Invoke it

```powershell
aws bedrock-agentcore invoke-agent-runtime `
  --region us-east-1 `
  --agent-runtime-arn arn:aws:bedrock-agentcore:us-east-1:082585646836:runtime/aeo_groundtruth_browser-fGhiSo82t0 `
  --runtime-session-id (([guid]::NewGuid().ToString('N')) + ([guid]::NewGuid().ToString('N'))) `
  --qualifier DEFAULT `
  --payload (Get-Content payload.json -Raw) `
  response.json
```

In practice use `scripts/smoke_invoke.py`, which builds the payload, concatenates the
session id correctly, and interprets the envelope:

```powershell
.\.venv\Scripts\python.exe scripts/smoke_invoke.py --arn <arn> --discover
```

---

## 7. Redeploying after a code change

`update-agent-runtime`, **not** create. It keeps the same ARN and bumps the version — we are
on v4.

```powershell
aws bedrock-agentcore-control update-agent-runtime `
  --region us-east-1 `
  --agent-runtime-id aeo_groundtruth_browser-fGhiSo82t0 `
  --agent-runtime-artifact file://artifact.json `
  --role-arn arn:aws:iam::082585646836:role/AmazonBedrockAgentCoreAEOGroundTruthBrowserRole `
  --network-configuration '{"networkMode":"PUBLIC"}' `
  --lifecycle-configuration '{"idleRuntimeSessionTimeout":300,"maxLifetime":3600}' `
  --environment-variables AGENTCORE_REGION=us-east-1,LOG_LEVEL=INFO
```

Or just `python scripts/provision.py --role-arn <role-arn>`, which is idempotent and does
build → push → update in one pass.

> ⚠️ **`update-agent-runtime` is a full REPLACE, not a merge.** Omit
> `--environment-variables` and the variables the create set are **wiped** —
> `get-agent-runtime` then reports `environmentVariables: null`. That happened here on
> v2/v3/v4 and went unnoticed for three redeploys, because both values happen to equal the
> defaults baked into `app/main.py`. Nothing broke; it would have broken the first time a
> non-default value mattered. Fixed in `provision.py`; pass every field on every update.

---

## The eight things that will bite you

**1. `CreateAgentRuntime` authorizes THREE actions, and their names are not inferable.**
This cost three round trips with the account administrator:

| Action | Resource AWS evaluates |
|---|---|
| `bedrock-agentcore:CreateAgentRuntime` | `runtime/*` |
| `bedrock-agentcore:CreateAgentRuntimeEndpoint` | `runtime/*` — it implicitly creates a `DEFAULT` endpoint |
| `bedrock-agentcore:CreateWorkloadIdentity` | **`workload-identity-directory/default/workload-identity/*`** |
| `iam:PassRole` | the execution role, conditioned on `iam:PassedToService` |

None of the middle two appear in AWS's own example policy. **The resource is as
unpredictable as the action** — the third one is not a `runtime/*` ARN at all. Our own
error handler hardcoded `runtime/*` and printed the wrong statement to request, and *a
statement granted on the wrong resource denies byte-for-byte identically*, so the next run
reads as "the grant never landed" and the blame lands on the administrator. Fixed in
`f3c1cbf`: the resource is parsed out of AWS's message, and the script says out loud when
it is guessing.

**The general lesson: the set of actions and resources one API call authorizes cannot be
derived from its name.** Ask for `BedrockAgentCoreFullAccess` (whose first statement is
`bedrock-agentcore:*`) rather than enumerating, or have an admin run `provision.py` once.

**2. There is no way to pre-test `CreateAgentRuntime`.** `scripts/check_permissions.py`
reports it **UNTESTABLE, not MISSING** — a probe that passed authorization would create a
real runtime. **The deploy is the only test.** Do not read "untestable" as "denied", and do
not run the checker hoping it will tell you whether a grant landed.

**3. A probe must never create a resource it is not willing to keep.** An earlier
permission probe created ECR repo `aeo-groundtruth/probe-ab0af7c4` for real the moment
`CreateRepository` was granted, then could not delete it because `DeleteRepository` was not.
Whether cleanup is possible depends on a permission the probe is not testing.

**4. Deploy by digest, not by tag.** A mutable tag means the image a **quarterly** job runs
is whatever was pushed last — potentially three months of unreviewed change. Tag by git
commit so a running image traces back to a commit. (An earlier note claimed `:latest` would
fail on an immutable repo; that was wrong — the repo is MUTABLE, so re-pushing a tag
silently succeeds, which is the worse failure.)

**5. `runtimeSessionId` must be 33+ characters.** A short id is **rejected by the API**, not
silently padded. `smoke_invoke.py` concatenates two hex UUIDs for 64.

**6. IAM is where the remaining wall is, and it moved — check before assuming.** As of the
`policy-caller.json` grant we **can** read IAM (`iam:GetRole`, `ListRoles`,
`ListRolePolicies`, `GetRolePolicy`) but **cannot** create it (`iam:CreateRole` and
`iam:PutRolePolicy` are denied — verified by attempting `CreateRole`, which failed and left
nothing behind).

So the execution role's policy contents **are** now verifiable, and were verified: the live
inline policy matches `provision.py`'s `PERMISSION_POLICY` Sid for Sid. That retires a
long-standing caveat — a role missing `StartBrowserSession` used to be an untestable risk
that would deploy cleanly, pass `/ping`, and fail only at invocation, presenting as your
bug. Read it instead:

```powershell
aws iam list-role-policies --role-name AmazonBedrockAgentCoreAEOGroundTruthBrowserRole
aws iam get-role-policy --role-name AmazonBedrockAgentCoreAEOGroundTruthBrowserRole `
  --policy-name AEOGroundTruthBrowserRuntimePolicy `
  --query 'PolicyDocument.Statement[].Sid'
```

Older notes in this repo and in the project memory say `iam:GetRole` is denied. **That is
stale.** What is still true: a *new* AgentCore feature needing a *new* execution role needs
an administrator, or must reuse this one.

⚠️ **The admin named the inline policy `AEOGroundTruthBrowserRuntimePolicy`**, while
`provision.py`'s create path would name it
`AmazonBedrockAgentCoreAEOGroundTruthBrowserRolePolicy`. Harmless today (that path never
ran) but it means the role would end up with *two* inline policies if it were ever
recreated by the script — and the teardown command below has to use the real name.

**7. The `{"input": {...}}` payload wrapper is AWS's example convention, not the
platform's.** AgentCore passes the payload bytes through verbatim. A flat payload is
correct; `app/main.py` accepts both, which costs one line and removes a whole class of
"deployed fine, returns nothing" confusion.

**8. Always answer HTTP 200 with an envelope, even on failure.** A 5xx reaches the consumer
as an opaque boto3 error with no page state and no `observed_egress`, and its retry
predicate cannot tell a login wall from a crash. This was violated in practice —
`client.start` sat outside `run_invocation`'s try — on the likeliest failure of all, a role
missing `StartBrowserSession`. There is now a last-resort guard in `main.py` and a test that
pins it.

---

## Teardown

```powershell
aws bedrock-agentcore-control delete-agent-runtime `
  --agent-runtime-id aeo_groundtruth_browser-fGhiSo82t0 --region us-east-1

# The live policy name is AEOGroundTruthBrowserRuntimePolicy - what the admin chose, NOT
# what provision.py would have named it. List them first rather than guessing.
aws iam list-role-policies --role-name AmazonBedrockAgentCoreAEOGroundTruthBrowserRole
aws iam delete-role-policy --role-name AmazonBedrockAgentCoreAEOGroundTruthBrowserRole `
  --policy-name AEOGroundTruthBrowserRuntimePolicy
aws iam delete-role --role-name AmazonBedrockAgentCoreAEOGroundTruthBrowserRole
# ^ both IAM deletes need an administrator: iam:CreateRole/PutRolePolicy/DeleteRole are
#   NOT granted to leo.lindo. Everything else here is self-serve.

aws ecr delete-repository --repository-name aeo-groundtruth/browser `
  --region us-east-1 --force
```

Deleting the runtime does **not** stop a browser session already running — those are
released by `driver.py`'s `finally`, with `_SESSION_TIMEOUT_SECONDS = 300` as the backstop.
Check for strays before assuming the meter has stopped:

```powershell
aws bedrock-agentcore list-browser-sessions --region us-east-1
```

---

## Current live state (2026-08-03)

```
Runtime ARN  arn:aws:bedrock-agentcore:us-east-1:082585646836:runtime/aeo_groundtruth_browser-fGhiSo82t0
Version      4          Status  READY        Network  PUBLIC
Image        aeo-groundtruth/browser@sha256:3b9f5f10f8ebbdbae44b988ca048934727de81396bea08ba8ca876e918d01dc8
Role         arn:aws:iam::082585646836:role/AmazonBedrockAgentCoreAEOGroundTruthBrowserRole
Workload id  .../workload-identity-directory/default/workload-identity/aeo_groundtruth_browser-fGhiSo82t0
```

Set in `aeo-agent-service/.env` (already done):

```
AGENTCORE_BROWSER_RUNTIME_ARN=arn:aws:bedrock-agentcore:us-east-1:082585646836:runtime/aeo_groundtruth_browser-fGhiSo82t0
AGENTCORE_REGION=us-east-1
```
