"""Probe every permission this project needs, one call at a time.

Written because `iam:SimulatePrincipalPolicy` is denied for this identity, so the only
honest way to know whether a grant landed is to attempt each action and read the error.

**How a probe decides.** Every AWS call authorizes BEFORE it validates, so:

    AccessDenied / AccessDeniedException      -> the permission is MISSING
    any other error (NotFound, Validation)    -> the permission is PRESENT, and the
                                                 request merely referred to something
                                                 that does not exist
    success                                   -> present

That is what lets most of these be read-only: pointing a call at a deliberately
non-existent resource proves authorization without touching anything.

Three actions cannot be probed that way, and the script says so rather than guessing:
`iam:PassRole` is evaluated only by `CreateAgentRuntime`; and the ECR layer-upload
actions only fire during a real push.

    python scripts/check_permissions.py
    python scripts/check_permissions.py --probe-writes   # also probes CreateSecret
"""

from __future__ import annotations

import argparse
import uuid

import boto3
from botocore.exceptions import ClientError

REGION = "us-east-1"
ACCOUNT = "082585646836"
ECR_REPO = "aeo-groundtruth/browser"
ROLE_NAME = "AmazonBedrockAgentCoreAEOGroundTruthBrowserRole"
ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/{ROLE_NAME}"

_DENIED = {"AccessDenied", "AccessDeniedException", "UnauthorizedException"}

GRANTED, MISSING, UNKNOWN = "GRANTED", "MISSING", "UNTESTABLE"


def probe(action: str, fn) -> tuple[str, str, str]:
    """Run one probe. Returns (action, verdict, detail)."""
    try:
        fn()
        return action, GRANTED, "call succeeded"
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code in _DENIED:
            return action, MISSING, code
        # Authorization passed; the call failed for an unrelated reason, which is
        # exactly what a probe aimed at a non-existent resource expects.
        return action, GRANTED, f"authorized (got {code})"
    except Exception as exc:  # noqa: BLE001
        return action, UNKNOWN, f"{type(exc).__name__}: {exc}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--probe-writes",
        action="store_true",
        help=(
            "also probe secretsmanager:CreateSecret by creating a dummy secret and "
            "immediately force-deleting it. Off by default because it is the only probe "
            "here that creates anything."
        ),
    )
    args = parser.parse_args()

    ecr = boto3.client("ecr", region_name=REGION)
    iam = boto3.client("iam")
    control = boto3.client("bedrock-agentcore-control", region_name=REGION)
    data = boto3.client("bedrock-agentcore", region_name=REGION)
    secrets = boto3.client("secretsmanager", region_name=REGION)

    missing_repo = f"aeo-groundtruth/probe-{uuid.uuid4().hex[:8]}"
    missing_secret = f"brightdata-probe-{uuid.uuid4().hex[:8]}"
    missing_runtime = "aeo_groundtruth_probe-XXXXXXXXXX"

    results: list[tuple[str, str, str]] = []

    # --- group 1: iam ---------------------------------------------------------
    results.append(probe("iam:ListRoles", lambda: iam.list_roles(MaxItems=1)))
    results.append(probe("iam:GetRole", lambda: iam.get_role(RoleName=ROLE_NAME)))
    results.append(
        (
            "iam:PassRole",
            UNKNOWN,
            "only evaluated by CreateAgentRuntime - no standalone probe exists",
        )
    )

    # --- group 2/3: ecr -------------------------------------------------------
    results.append(
        probe("ecr:GetAuthorizationToken", lambda: ecr.get_authorization_token())
    )
    results.append(
        probe("ecr:DescribeRepositories", lambda: ecr.describe_repositories(maxResults=1))
    )
    # Probing a create genuinely requires attempting one, so it targets the REAL
    # repository this project needs rather than a throwaway name.
    #
    # An earlier version used a unique throwaway name and tried to delete it afterwards.
    # That left a repository behind the first time the grant landed, because
    # `ecr:DeleteRepository` is deliberately NOT part of the grant - so the probe could
    # create litter it had no way to remove. Aiming at the real name makes success
    # useful (the repository is wanted) and makes a second run report
    # RepositoryAlreadyExists, which is still proof of authorization.
    results.append(
        probe("ecr:CreateRepository", lambda: ecr.create_repository(repositoryName=ECR_REPO))
    )
    # These three are safe against a name that does not exist: RepositoryNotFound proves
    # authorization. They are pointed at the real repo only so the output is readable.
    results.append(
        probe(
            "ecr:InitiateLayerUpload",
            lambda: ecr.initiate_layer_upload(repositoryName=missing_repo),
        )
    )
    results.append(
        probe(
            "ecr:BatchCheckLayerAvailability",
            lambda: ecr.batch_check_layer_availability(
                repositoryName=missing_repo, layerDigests=["sha256:" + "0" * 64]
            ),
        )
    )
    results.append(
        probe(
            "ecr:DescribeImages",
            lambda: ecr.describe_images(repositoryName=missing_repo),
        )
    )
    # Safe to probe for real: aimed at a repository that does not exist, so a
    # RepositoryNotFound proves authorization and there is nothing to destroy. This is the
    # one action where a probe pointed at the REAL resource would be unforgivable.
    results.append(
        probe(
            "ecr:DeleteRepository",
            lambda: ecr.delete_repository(repositoryName=missing_repo),
        )
    )
    results.append(
        (
            "ecr:UploadLayerPart / CompleteLayerUpload / PutImage",
            GRANTED,
            "proven by the real push of aeo-groundtruth/browser:4545e06",
        )
    )

    # --- group 4: agentcore ---------------------------------------------------
    results.append(
        probe("bedrock-agentcore:ListAgentRuntimes", lambda: control.list_agent_runtimes())
    )
    results.append(
        probe(
            "bedrock-agentcore:GetAgentRuntime",
            lambda: control.get_agent_runtime(agentRuntimeId=missing_runtime),
        )
    )
    results.append(
        probe(
            "bedrock-agentcore:UpdateAgentRuntime",
            lambda: control.update_agent_runtime(
                agentRuntimeId=missing_runtime,
                agentRuntimeArtifact={
                    "containerConfiguration": {
                        "containerUri": f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/nope:1"
                    }
                },
                networkConfiguration={"networkMode": "PUBLIC"},
                roleArn=ROLE_ARN,
            ),
        )
    )
    results.append(
        probe(
            "bedrock-agentcore:InvokeAgentRuntime",
            lambda: data.invoke_agent_runtime(
                agentRuntimeArn=(
                    f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/{missing_runtime}"
                ),
                runtimeSessionId=f"{uuid.uuid4().hex}{uuid.uuid4().hex}",
                payload=b"{}",
            ),
        )
    )
    results.append(
        probe(
            "bedrock-agentcore:ListAgentRuntimeEndpoints",
            lambda: control.list_agent_runtime_endpoints(agentRuntimeId=missing_runtime),
        )
    )
    results.append(
        probe(
            "bedrock-agentcore:GetAgentRuntimeEndpoint",
            lambda: control.get_agent_runtime_endpoint(
                agentRuntimeId=missing_runtime, endpointName="DEFAULT"
            ),
        )
    )
    results.append(
        (
            "bedrock-agentcore:CreateAgentRuntime",
            UNKNOWN,
            "not probed: a probe that got past authorization could create a real runtime",
        )
    )
    results.append(
        (
            "bedrock-agentcore:CreateAgentRuntimeEndpoint",
            UNKNOWN,
            "same - and note CreateAgentRuntime authorizes this one too, implicitly",
        )
    )

    # --- group 5: secrets manager --------------------------------------------
    results.append(
        probe(
            "secretsmanager:DescribeSecret",
            lambda: secrets.describe_secret(SecretId=missing_secret),
        )
    )
    results.append(
        probe(
            "secretsmanager:GetSecretValue",
            lambda: secrets.get_secret_value(SecretId=missing_secret),
        )
    )
    if args.probe_writes:
        results.append(
            probe(
                "secretsmanager:CreateSecret",
                lambda: secrets.create_secret(
                    Name=missing_secret, SecretString='{"username":"probe","password":"probe"}'
                ),
            )
        )
    else:
        results.append(
            (
                "secretsmanager:CreateSecret / PutSecretValue",
                UNKNOWN,
                "pass --probe-writes to test (creates then force-deletes a dummy secret)",
            )
        )

    # --- report ---------------------------------------------------------------
    width = max(len(a) for a, _, _ in results)
    print(f"\naccount={ACCOUNT} region={REGION}")
    print(f"identity={boto3.client('sts').get_caller_identity()['Arn']}\n")
    for action, verdict, detail in results:
        mark = {GRANTED: "OK  ", MISSING: "NO  ", UNKNOWN: "??  "}[verdict]
        print(f"{mark}{action.ljust(width)}  {verdict:<11} {detail}")

    # Does the execution role exist yet? ListRoles works even though GetRole does not,
    # so this is the one way to see whether the administrator has created it.
    print()
    try:
        found = False
        for page in iam.get_paginator("list_roles").paginate():
            for role in page["Roles"]:
                if role["RoleName"] == ROLE_NAME:
                    found = True
                    print(f"execution role: EXISTS  {role['Arn']}")
                    break
            if found:
                break
        if not found:
            print(f"execution role: NOT FOUND  {ROLE_NAME}")
            print("  (an administrator has to create it - docs/PERMISSIONS-REQUEST.md)")
    except ClientError as exc:
        print(f"execution role: cannot list roles ({exc.response['Error']['Code']})")

    # --- clean up anything a probe created -----------------------------------
    #
    # No ECR cleanup: the create probe now targets the repository this project actually
    # wants, so there is nothing to undo. Deleting it would not be possible anyway -
    # `ecr:DeleteRepository` is not in the grant, which is correct for least privilege
    # and is exactly why no probe may create a resource it does not want to keep.
    for action, verdict, _ in results:
        if action == "secretsmanager:CreateSecret" and verdict == GRANTED:
            try:
                secrets.delete_secret(
                    SecretId=missing_secret, ForceDeleteWithoutRecovery=True
                )
                print(f"cleanup: force-deleted probe secret {missing_secret}")
            except ClientError as exc:
                print(f"cleanup: could NOT delete {missing_secret}: {exc}")

    denied = [a for a, v, _ in results if v == MISSING]
    print()
    if denied:
        print(f"{len(denied)} still missing: {', '.join(denied)}")
        return 1
    print("No probe came back denied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
