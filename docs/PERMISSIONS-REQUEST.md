# Access request: deploy the AEO ground-truth browser runtime

**Account:** `082585646836` · **Region:** `us-east-1` · **Requested for:** `arn:aws:iam::082585646836:user/leo.lindo`

## What this is

The AEO platform's Geo-Local Share-of-Voice feature has a "ground truth" tier that
measures what a real customer in a specific town sees when they ask an AI assistant for
a local service. It does that by driving chatgpt.com and perplexity.ai in a real
browser that egresses from a residential IP in that town.

The browser is **Amazon Bedrock AgentCore Browser** (managed, nothing to create). What
has to be deployed is an **AgentCore Runtime** — an ARM64 container that opens the
browser session and drives the page. The container is written, builds, and passes its
tests; the only thing missing is permission to put it in AWS.

This mirrors the existing `AgentCore-aeoskills-production` stack in the same account
and region, which already runs two AgentCore runtimes the same way.

## What is currently blocked

`leo.lindo` can read the AgentCore control plane and authenticate to ECR, but cannot
deploy. Verified by attempting each call:

| Action | Result |
| --- | --- |
| `ecr:CreateRepository` | AccessDenied (any namespace) |
| `ecr:InitiateLayerUpload` (image push) | AccessDenied |
| `iam:CreateRole` | AccessDenied |
| `iam:GetRole`, `iam:GetPolicy` | AccessDenied |
| `secretsmanager:ListSecrets` | AccessDenied |

## The recommended split: you keep IAM, we get the rest

**You create the execution role yourself** (§"The runtime's own execution role" below has
both policies, ready to paste). We then need **no IAM management permissions at all** — no
`iam:CreateRole`, no `iam:GetRole`, no `iam:PutRolePolicy`. The script takes the role you
made:

```bash
python scripts/provision.py --role-arn arn:aws:iam::082585646836:role/AmazonBedrockAgentCoreAEOGroundTruthBrowserRole
```

### ⚠️ The one IAM permission that is still required: `iam:PassRole`

This is the part that is easy to miss, so it is worth being explicit. Creating the role
is **not sufficient on its own**. `CreateAgentRuntime` *passes* that role to the
AgentCore service, and AWS evaluates whether the **caller** is allowed to pass it. AWS's
own documented AgentCore policy carries this statement (`IAMPassRoleAccess`) for exactly
this reason.

Without it, every other step succeeds — repository created, image pushed — and only the
final call fails, with an error that reads like missing AgentCore access rather than
missing IAM access.

It is a narrow grant: it permits handing **one named role** to **one service**, and
nothing else. It cannot be used to assume the role, modify it, or pass it anywhere else.

```json
{
  "Sid": "PassTheRuntimeRoleToAgentCoreOnly",
  "Effect": "Allow",
  "Action": "iam:PassRole",
  "Resource": "arn:aws:iam::082585646836:role/AmazonBedrockAgentCoreAEOGroundTruthBrowserRole",
  "Condition": {
    "StringEquals": { "iam:PassedToService": "bedrock-agentcore.amazonaws.com" }
  }
}
```

> The role name begins with `AmazonBedrockAgentCore` on purpose. AWS's documented policy
> scopes `iam:PassRole` to `arn:aws:iam::*:role/AmazonBedrockAgentCore*`, so this name is
> already covered by a pattern you may have approved elsewhere, instead of needing a
> one-off exception.

### The rest of the policy for us

No IAM in here. One ECR namespace, the AgentCore control plane, and this feature's own
secrets. It grants nothing over the existing `aeoskills` runtimes or any other repository.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "EcrAuthTokenCannotBeScoped",
      "Effect": "Allow",
      "Action": "ecr:GetAuthorizationToken",
      "Resource": "*"
    },
    {
      "Sid": "OwnEcrNamespaceOnly",
      "Effect": "Allow",
      "Action": [
        "ecr:CreateRepository",
        "ecr:DescribeRepositories",
        "ecr:DescribeImages",
        "ecr:TagResource",
        "ecr:BatchCheckLayerAvailability",
        "ecr:InitiateLayerUpload",
        "ecr:UploadLayerPart",
        "ecr:CompleteLayerUpload",
        "ecr:PutImage",
        "ecr:BatchGetImage",
        "ecr:GetDownloadUrlForLayer"
      ],
      "Resource": "arn:aws:ecr:us-east-1:082585646836:repository/aeo-groundtruth/*"
    },
    {
      "Sid": "ManageThisRuntimeAndInvokeIt",
      "Effect": "Allow",
      "Action": [
        "bedrock-agentcore:CreateAgentRuntime",
        "bedrock-agentcore:UpdateAgentRuntime",
        "bedrock-agentcore:GetAgentRuntime",
        "bedrock-agentcore:ListAgentRuntimes",
        "bedrock-agentcore:InvokeAgentRuntime",
        "bedrock-agentcore:TagResource"
      ],
      "Resource": "*"
    },
    {
      "Sid": "ProxyCredentialSecretsForThisFeature",
      "Effect": "Allow",
      "Action": [
        "secretsmanager:CreateSecret",
        "secretsmanager:PutSecretValue",
        "secretsmanager:DescribeSecret",
        "secretsmanager:GetSecretValue"
      ],
      "Resource": "arn:aws:secretsmanager:us-east-1:082585646836:secret:brightdata-*"
    }
  ]
}
```

Two entries are not resource-scoped, and they are the ones worth questioning:

- **`ecr:GetAuthorizationToken`** accepts no resource condition in IAM. It returns a
  registry login token and grants no access to any repository by itself. Leo already
  has it.
- **`bedrock-agentcore:*AgentRuntime`** is `"*"` because a runtime's ARN contains a
  server-generated suffix that does not exist until creation, so `CreateAgentRuntime`
  cannot be scoped to it in advance. After the first create, the Get/Update/Invoke
  actions can be tightened to
  `arn:aws:bedrock-agentcore:us-east-1:082585646836:runtime/aeo_groundtruth_browser-*`.

### Alternative: you run the whole thing once, we get nothing

If you would rather not grant anything, run the script with your own credentials and send
back the ARN it prints:

```bash
python scripts/provision.py --check   # read-only: shows what is missing
python scripts/provision.py           # ECR repo, IAM role, image build+push, runtime
```

That works completely, and Leo keeps read-only access. The trade-off is that you are
needed again for every subsequent deploy, since re-running is how a code change ships.

## The runtime's own execution role

This is the role **AgentCore** assumes to run the container — the one that opens browser
sessions and reads the proxy credentials. Create it as
`AmazonBedrockAgentCoreAEOGroundTruthBrowserRole`.

Both documents below are generated from `scripts/provision.py`, so they are exactly what
the script would create; they will not drift from the code.

**Trust policy** — carries both confused-deputy guards (`aws:SourceAccount` and
`aws:SourceArn`) as AWS's documentation specifies, so no AgentCore resource in another
account can assume it:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": { "Service": "bedrock-agentcore.amazonaws.com" },
      "Action": "sts:AssumeRole",
      "Condition": {
        "StringEquals": { "aws:SourceAccount": "082585646836" },
        "ArnLike": {
          "aws:SourceArn": "arn:aws:bedrock-agentcore:us-east-1:082585646836:*"
        }
      }
    }
  ]
}
```

**Permission policy.** This follows AWS's documented execution-role template, with two
deliberate differences called out after the JSON.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "DriveBrowserSessions",
      "Effect": "Allow",
      "Action": [
        "bedrock-agentcore:StartBrowserSession",
        "bedrock-agentcore:StopBrowserSession",
        "bedrock-agentcore:GetBrowserSession",
        "bedrock-agentcore:ListBrowserSessions",
        "bedrock-agentcore:ConnectBrowserAutomationStream"
      ],
      "Resource": "*"
    },
    {
      "Sid": "ReadOneGeosProxyCredentials",
      "Effect": "Allow",
      "Action": "secretsmanager:GetSecretValue",
      "Resource": "arn:aws:secretsmanager:us-east-1:082585646836:secret:brightdata-*"
    },
    {
      "Sid": "EcrTokenCannotBeScoped",
      "Effect": "Allow",
      "Action": "ecr:GetAuthorizationToken",
      "Resource": "*"
    },
    {
      "Sid": "PullTheRuntimeImage",
      "Effect": "Allow",
      "Action": ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"],
      "Resource": "arn:aws:ecr:us-east-1:082585646836:repository/*"
    },
    {
      "Sid": "LogGroups",
      "Effect": "Allow",
      "Action": ["logs:DescribeLogStreams", "logs:CreateLogGroup"],
      "Resource": "arn:aws:logs:us-east-1:082585646836:log-group:/aws/bedrock-agentcore/runtimes/*"
    },
    {
      "Sid": "LogResourcePolicy",
      "Effect": "Allow",
      "Action": ["logs:PutResourcePolicy"],
      "Resource": "arn:aws:logs:us-east-1:082585646836:log-group:/aws/bedrock-agentcore/runtimes/aeo_groundtruth_browser-*"
    },
    {
      "Sid": "DescribeLogGroupsIsAccountWide",
      "Effect": "Allow",
      "Action": ["logs:DescribeLogGroups"],
      "Resource": "arn:aws:logs:us-east-1:082585646836:log-group:*"
    },
    {
      "Sid": "LogStreams",
      "Effect": "Allow",
      "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
      "Resource": "arn:aws:logs:us-east-1:082585646836:log-group:/aws/bedrock-agentcore/runtimes/*:log-stream:*"
    },
    {
      "Sid": "Observability",
      "Effect": "Allow",
      "Action": [
        "xray:PutTraceSegments",
        "xray:PutTelemetryRecords",
        "xray:GetSamplingRules",
        "xray:GetSamplingTargets"
      ],
      "Resource": "*"
    },
    {
      "Sid": "Metrics",
      "Effect": "Allow",
      "Action": "cloudwatch:PutMetricData",
      "Resource": "*",
      "Condition": {
        "StringEquals": { "cloudwatch:namespace": "bedrock-agentcore" }
      }
    },
    {
      "Sid": "OwnWorkloadIdentityOnly",
      "Effect": "Allow",
      "Action": [
        "bedrock-agentcore:GetWorkloadAccessToken",
        "bedrock-agentcore:GetWorkloadAccessTokenForJWT"
      ],
      "Resource": [
        "arn:aws:bedrock-agentcore:us-east-1:082585646836:workload-identity-directory/default",
        "arn:aws:bedrock-agentcore:us-east-1:082585646836:workload-identity-directory/default/workload-identity/aeo_groundtruth_browser-*"
      ]
    }
  ]
}
```

Where this differs from AWS's template, and why:

- **`bedrock:InvokeModel` is deliberately NOT granted.** AWS's template includes it
  because most AgentCore runtimes are LLM agents. This one drives a browser and calls no
  model — the language-model work happens in the calling service on its own credentials —
  so granting it here would be authority with no caller.
- **`secretsmanager:GetSecretValue` is scoped to the `brightdata-` prefix**, not `*`. The
  runtime uses exactly one town's credentials per session, so read access to every secret
  in the account is authority it can never legitimately need. The trailing `*` covers the
  six random characters AWS appends to every secret ARN.
- **`GetWorkloadAccessTokenForUserId` is excluded** while the `ForJWT` variant is kept.
  AWS's own guidance recommends denying the `ForUserId` form outside development, since it
  issues tokens from caller-supplied user identifiers with no IdP verification. This
  runtime uses no inbound or outbound OAuth, so it needs neither, but the pair is left in
  its documented minimal form rather than removed entirely.

## Cost, so it is not a surprise

- The ECR repository and the runtime itself cost approximately nothing when idle.
- **Browser sessions are the cost.** Our own estimate is ~$1.50 per session against the
  product requirement's "10–50x an API call". The feature runs **quarterly**, at 10
  prompts x 2 surfaces, and the calling service enforces a hard cap of 60 sessions per
  run plus a maximum of 5 concurrent — those limits are already in code, not planned.
- Verifying the deployment needs a handful of sessions, so single-digit dollars.

## Still outstanding after this, for completeness

Bright Data residential proxy credentials do not exist yet. Without them the runtime
can be deployed and its browser automation verified, but it egresses from AWS rather
than from the customer's town — which the calling service detects and rejects by
design, rather than recording as a result. Those credentials are a separate,
non-AWS procurement.
