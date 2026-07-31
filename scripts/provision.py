"""Idempotent provisioning for the ground-truth browser runtime.

Re-runnable: every step checks for an existing resource first and reports `exists`
rather than failing. Run it as many times as you like; run it again after changing the
image and it updates the runtime to the new digest.

    python scripts/provision.py --check      # read-only: what exists, what is missing
    python scripts/provision.py              # create/update everything
    python scripts/provision.py --skip-push  # provision only, do not rebuild/push

**On permissions.** This account's `leo.lindo` user is denied `iam:GetRole`, so
`iam:CreateRole` may well be denied too. That is handled deliberately: the IAM step
catches AccessDenied, prints the exact trust policy and permission policy JSON for
whoever does have the access, and the script continues so everything else is still
provisioned. A partial run that names its one blocker beats a run that dies on step
three.

**Why not CDK.** The org's existing AgentCore agents come from a CDK app
(`AgentCore-aeoskills-production`) that is not available on this machine. These
scripts mirror that stack's shape - ECR repo, execution role, runtime with PUBLIC
network mode - so devops can fold them into it later without re-deciding anything.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time

import boto3
from botocore.exceptions import ClientError

REGION = "us-east-1"  # Where this account's existing AgentCore runtimes live.
ACCOUNT = "082585646836"
ECR_REPO = "aeo-groundtruth/browser"
IMAGE_TAG = "latest"
ROLE_NAME = "AEOGroundTruthBrowserRuntimeRole"
RUNTIME_NAME = "aeo_groundtruth_browser"

#: Secrets this runtime may read. Scoped to the naming convention aeo-agent-service
#: derives (`brightdata-<town>-<region>`) rather than `*`: the runtime's whole job is
#: to use ONE geo's credentials per session, so read access to every secret in the
#: account is authority it can never need. The trailing `*` covers the six random
#: characters AWS appends to every secret ARN.
SECRET_PREFIX = "brightdata-"

REPO_URI = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/{ECR_REPO}"

TRUST_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
            "Action": "sts:AssumeRole",
            # Confused-deputy guards: without these, any AgentCore resource in any
            # account could assume this role.
            "Condition": {
                "StringEquals": {"aws:SourceAccount": ACCOUNT},
                "ArnLike": {
                    "aws:SourceArn": f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:*"
                },
            },
        }
    ],
}

PERMISSION_POLICY = {
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
                "bedrock-agentcore:ConnectBrowserAutomationStream",
            ],
            "Resource": "*",
        },
        {
            "Sid": "ReadOneGeosProxyCredentials",
            "Effect": "Allow",
            "Action": "secretsmanager:GetSecretValue",
            "Resource": (
                f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:{SECRET_PREFIX}*"
            ),
        },
        {
            "Sid": "PullTheRuntimeImage",
            "Effect": "Allow",
            "Action": [
                "ecr:GetAuthorizationToken",
                "ecr:BatchGetImage",
                "ecr:GetDownloadUrlForLayer",
            ],
            "Resource": "*",
        },
        {
            "Sid": "Logs",
            "Effect": "Allow",
            "Action": [
                "logs:CreateLogGroup",
                "logs:CreateLogStream",
                "logs:PutLogEvents",
                "logs:DescribeLogStreams",
            ],
            "Resource": f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:/aws/bedrock-agentcore/*",
        },
    ],
}


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    print(f"  $ {' '.join(cmd)}")
    return subprocess.run(cmd, check=True, **kw)


def ensure_ecr(check: bool) -> str | None:
    ecr = boto3.client("ecr", region_name=REGION)
    try:
        ecr.describe_repositories(repositoryNames=[ECR_REPO])
        print(f"[ecr] exists: {REPO_URI}")
        return REPO_URI
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "RepositoryNotFoundException":
            raise
    if check:
        print(f"[ecr] MISSING: {ECR_REPO}")
        return REPO_URI
    try:
        ecr.create_repository(
            repositoryName=ECR_REPO,
            imageScanningConfiguration={"scanOnPush": True},
            # Immutable so a runtime pinned to :latest cannot silently change underneath
            # a quarterly job. We deploy by digest anyway; this makes that a rule.
            imageTagMutability="IMMUTABLE",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in ("AccessDeniedException", "AccessDenied"):
            raise
        # Same treatment as the IAM step: name the one blocker and keep going, so a
        # single run reports everything that is missing rather than the first thing.
        print("[ecr] BLOCKED: this identity cannot create ECR repositories.")
        print("      See docs/PERMISSIONS-REQUEST.md - grant the OwnEcrNamespaceOnly")
        print("      statement, or have an admin run this script once.")
        return None
    print(f"[ecr] created: {REPO_URI}")
    return REPO_URI


def build_and_push(repo_uri: str) -> str:
    """Build for linux/arm64 and push. Returns the image digest reference.

    Deploys by DIGEST, not by tag. The existing aeoskills runtimes do the same, and the
    reason matters here: a mutable tag means the image a quarterly job runs is whatever
    was pushed last, which could be three months of unreviewed change.
    """
    token = boto3.client("ecr", region_name=REGION).get_authorization_token()
    import base64

    auth = base64.b64decode(token["authorizationData"][0]["authorizationToken"]).decode()
    _, password = auth.split(":", 1)
    registry = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com"
    _run(
        ["docker", "login", "--username", "AWS", "--password-stdin", registry],
        input=password.encode(),
    )
    tag = f"{repo_uri}:{IMAGE_TAG}"
    # --provenance=false: the OCI attestation manifest buildx adds by default makes the
    # pushed artifact a manifest LIST, which AgentCore's image resolution rejects.
    _run(
        [
            "docker", "buildx", "build",
            "--platform", "linux/arm64",
            "--provenance=false",
            "-t", tag,
            "--push", ".",
        ]
    )
    images = boto3.client("ecr", region_name=REGION).describe_images(
        repositoryName=ECR_REPO, imageIds=[{"imageTag": IMAGE_TAG}]
    )
    digest = images["imageDetails"][0]["imageDigest"]
    print(f"[image] pushed {ECR_REPO}@{digest}")
    return f"{repo_uri}@{digest}"


def ensure_role(check: bool) -> str | None:
    """Create the runtime execution role, or explain what to ask for."""
    iam = boto3.client("iam")
    arn = f"arn:aws:iam::{ACCOUNT}:role/{ROLE_NAME}"
    try:
        iam.get_role(RoleName=ROLE_NAME)
        print(f"[iam] exists: {arn}")
        return arn
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code == "AccessDenied":
            # Expected in this account: leo.lindo cannot read IAM roles.
            print("[iam] cannot READ roles (AccessDenied) - assuming it must be created")
        elif code not in ("NoSuchEntity", "ValidationError"):
            raise

    if check:
        print(f"[iam] MISSING (or unreadable): {ROLE_NAME}")
        return None

    try:
        iam.create_role(
            RoleName=ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(TRUST_POLICY),
            Description="Execution role for the AEO Tier-3 ground-truth browser runtime",
        )
        iam.put_role_policy(
            RoleName=ROLE_NAME,
            PolicyName=f"{ROLE_NAME}Policy",
            PolicyDocument=json.dumps(PERMISSION_POLICY),
        )
        print(f"[iam] created: {arn}")
        # IAM is eventually consistent; CreateAgentRuntime fails validation if it
        # cannot yet assume the brand-new role.
        print("[iam] waiting 12s for propagation")
        time.sleep(12)
        return arn
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in ("AccessDenied", "AccessDeniedException"):
            raise
        print()
        print("=" * 78)
        print("BLOCKED: this identity cannot create IAM roles.")
        print("Send the two documents below to whoever administers IAM, and ask for a")
        print(f"role named {ROLE_NAME} in account {ACCOUNT}.")
        print("=" * 78)
        print("\n--- TRUST POLICY ---")
        print(json.dumps(TRUST_POLICY, indent=2))
        print("\n--- PERMISSION POLICY ---")
        print(json.dumps(PERMISSION_POLICY, indent=2))
        print("\nThen re-run this script; it will pick the role up and continue.")
        print("=" * 78)
        return None


def ensure_runtime(container_uri: str, role_arn: str, check: bool) -> str | None:
    control = boto3.client("bedrock-agentcore-control", region_name=REGION)
    existing = None
    for runtime in control.list_agent_runtimes().get("agentRuntimes", []):
        if runtime["agentRuntimeName"] == RUNTIME_NAME:
            existing = runtime
            break

    if check:
        print(f"[runtime] {'exists' if existing else 'MISSING'}: {RUNTIME_NAME}")
        return existing["agentRuntimeArn"] if existing else None

    artifact = {"containerConfiguration": {"containerUri": container_uri}}
    lifecycle = {
        # Tighter than the aeoskills runtimes (900/28800). Ground-truth invocations are
        # minutes long and quarterly, so a long idle window keeps a runtime warm for
        # three months for nothing; and maxLifetime is a backstop against a wedged
        # session, which here means a paid browser.
        "idleRuntimeSessionTimeout": 300,
        "maxLifetime": 3600,
    }
    if existing:
        response = control.update_agent_runtime(
            agentRuntimeId=existing["agentRuntimeId"],
            agentRuntimeArtifact=artifact,
            networkConfiguration={"networkMode": "PUBLIC"},
            roleArn=role_arn,
            lifecycleConfiguration=lifecycle,
        )
        print(f"[runtime] updated to version {response.get('agentRuntimeVersion')}")
        return existing["agentRuntimeArn"]

    response = control.create_agent_runtime(
        agentRuntimeName=RUNTIME_NAME,
        description="AEO Tier-3 ground-truth browser runtime (geo-local SoV)",
        agentRuntimeArtifact=artifact,
        networkConfiguration={"networkMode": "PUBLIC"},
        roleArn=role_arn,
        lifecycleConfiguration=lifecycle,
        environmentVariables={"AGENTCORE_REGION": REGION, "LOG_LEVEL": "INFO"},
    )
    print(f"[runtime] created: {response['agentRuntimeArn']} ({response['status']})")
    return response["agentRuntimeArn"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="read-only inventory")
    parser.add_argument("--skip-push", action="store_true", help="do not rebuild/push")
    args = parser.parse_args()

    print(f"account={ACCOUNT} region={REGION}\n")
    repo_uri = ensure_ecr(args.check)
    role_arn = ensure_role(args.check)

    container_uri = f"{repo_uri}:{IMAGE_TAG}" if repo_uri else ""
    if not args.check and not args.skip_push and repo_uri:
        container_uri = build_and_push(repo_uri)

    if args.check:
        ensure_runtime(container_uri, role_arn or "", check=True)
        return 0

    blockers = [
        name
        for name, ok in (("ECR repository", repo_uri), ("IAM execution role", role_arn))
        if not ok
    ]
    if blockers:
        print()
        print(f"Stopping before the runtime. Blocked on: {', '.join(blockers)}.")
        print("Nothing above was left half-done, and this script is idempotent, so")
        print("re-running it once access is granted finishes the job.")
        print("Hand docs/PERMISSIONS-REQUEST.md to whoever administers this account.")
        return 2

    arn = ensure_runtime(container_uri, role_arn, check=False)
    print()
    print("Set this in aeo-agent-service's .env:")
    print(f"  AGENTCORE_BROWSER_RUNTIME_ARN={arn}")
    print(f"  AGENTCORE_REGION={REGION}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
