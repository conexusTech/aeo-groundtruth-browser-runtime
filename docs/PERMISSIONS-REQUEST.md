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

## Two ways to unblock, pick either

### Option A — an admin runs the provisioning script once (no new permissions)

The repo contains an idempotent script that creates everything and prints the runtime
ARN. Run it with admin credentials:

```bash
python scripts/provision.py --check   # read-only: shows what is missing
python scripts/provision.py           # creates ECR repo, IAM role, image, runtime
```

Then send back the ARN it prints. **Nothing else is needed from us**, and Leo keeps
read-only access. Re-running it later updates the image, so an admin would be needed
again for each deploy — which is the trade-off against Option B.

### Option B — grant Leo the deploy permissions (self-serve)

Attach the policy below. It is scoped to this feature's own resources: one ECR
namespace, one named IAM role, and Bright Data secrets. It grants nothing over the
existing `aeoskills` runtimes or any other repository.

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
      "Sid": "OneNamedExecutionRole",
      "Effect": "Allow",
      "Action": [
        "iam:CreateRole",
        "iam:GetRole",
        "iam:TagRole",
        "iam:PutRolePolicy",
        "iam:GetRolePolicy"
      ],
      "Resource": "arn:aws:iam::082585646836:role/AEOGroundTruthBrowserRuntimeRole"
    },
    {
      "Sid": "PassThatRoleToAgentCoreOnly",
      "Effect": "Allow",
      "Action": "iam:PassRole",
      "Resource": "arn:aws:iam::082585646836:role/AEOGroundTruthBrowserRuntimeRole",
      "Condition": {
        "StringEquals": {
          "iam:PassedToService": "bedrock-agentcore.amazonaws.com"
        }
      }
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

Notes on the two entries that are not resource-scoped, since those are the ones worth
questioning:

- **`ecr:GetAuthorizationToken`** does not accept a resource condition in IAM. It
  returns a registry login token only; it grants no access to any repository by itself.
  Leo already has it.
- **`bedrock-agentcore:*AgentRuntime`** is `"*"` because a runtime's ARN contains a
  server-generated suffix that does not exist until it is created, so `CreateAgentRuntime`
  cannot be scoped to it. It can be tightened to
  `arn:aws:bedrock-agentcore:us-east-1:082585646836:runtime/aeo_groundtruth_browser-*`
  for the Get/Update/Invoke actions after the first create, if that is preferred.

## The runtime's own execution role

Separately from the above, the runtime needs an execution role that **AgentCore**
assumes. `scripts/provision.py` creates it, but here it is for review. This is the role
that actually opens browser sessions and reads the proxy credentials.

**Trust policy** — includes both confused-deputy guards, so no AgentCore resource in
another account can assume it:

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

**Permission policy** — the secret access is scoped to the `brightdata-` prefix rather
than `*` on purpose: the runtime uses exactly one town's credentials per session, so
read access to every secret in the account is authority it can never legitimately need.

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
      "Sid": "PullTheRuntimeImage",
      "Effect": "Allow",
      "Action": [
        "ecr:GetAuthorizationToken",
        "ecr:BatchGetImage",
        "ecr:GetDownloadUrlForLayer"
      ],
      "Resource": "*"
    },
    {
      "Sid": "Logs",
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents",
        "logs:DescribeLogStreams"
      ],
      "Resource": "arn:aws:logs:us-east-1:082585646836:log-group:/aws/bedrock-agentcore/*"
    }
  ]
}
```

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
