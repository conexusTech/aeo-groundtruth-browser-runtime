"""AgentCore Runtime entrypoint for the Tier-3 ground-truth browser path.

The platform contract is fixed and minimal: `POST /invocations`, `GET /ping`, host
`0.0.0.0`, port 8080, `linux/arm64`. Everything else here is ours.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import FastAPI, Request

from app.driver import run_invocation
from app.models import InvocationRequest, InvocationResponse

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    # ASCII-only format and messages throughout: these lines are read in CloudWatch
    # and grepped from a Windows console, where non-ASCII renders as mojibake and a
    # corrupted-looking diagnostic reads as a broken tool rather than as the finding.
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

AGENTCORE_REGION = os.getenv("AGENTCORE_REGION", "us-east-1")

app = FastAPI(title="AEO Ground-Truth Browser Runtime", version="1.0.0")


@app.get("/ping")
async def ping() -> dict[str, str]:
    """Health check.

    Deliberately does NOT touch AWS. AgentCore polls this to decide whether the
    runtime is alive; making it verify credentials or the browser service would mean a
    transient AWS blip takes the runtime out of service rather than failing the one
    invocation it affects.
    """
    return {"status": "healthy"}


@app.post("/invocations", response_model=InvocationResponse, response_model_exclude_none=False)
async def invocations(request: Request) -> InvocationResponse:
    """Drive one prompt on one surface and return the §4 envelope.

    Accepts both the flat payload aeo-agent-service sends and the `{"input": {...}}`
    wrapper used by AWS's own examples and console test harness. AgentCore passes the
    payload through verbatim — the wrapper is a convention, not a platform
    requirement — so accepting both costs one line and removes a whole class of
    "deployed fine, returns nothing" confusion.

    **Always answers 200 with an envelope, including on failure.** A 5xx reaches the
    consumer as an opaque boto3 error carrying no page state and no observed egress,
    and its retry predicate cannot tell a login wall from a crash. The envelope's
    `error` field is the diagnosis channel instead.
    """
    body: Any = await request.json()
    if isinstance(body, dict) and "input" in body and isinstance(body["input"], dict):
        body = body["input"]

    try:
        parsed = InvocationRequest.model_validate(body)
    except Exception as exc:  # noqa: BLE001
        logger.error("rejected an unparseable payload: %s", exc)
        return InvocationResponse(error=f"invalid payload: {exc}")

    logger.info(
        "invocation surface=%s url=%s proxied=%s target=%s",
        parsed.surface,
        parsed.url,
        parsed.proxy is not None,
        parsed.proxy_target,
    )
    return await run_invocation(parsed, region=AGENTCORE_REGION)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
