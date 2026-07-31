# AgentCore Runtime requires linux/arm64. An amd64 image fails at DEPLOY, not at
# build, so the platform is pinned here rather than left to the builder's host.
FROM --platform=linux/arm64 python:3.12-slim-bookworm

# NO `playwright install`, and no Chromium, fonts, or system browser libraries.
#
# This is the single biggest thing to understand about this image, and the thing most
# likely to be "fixed" by someone in future: the browser is REMOTE. It runs in the
# AgentCore Browser service and this container attaches to it over CDP with
# `connect_over_cdp`, which needs only the Playwright pip package and its bundled
# Node driver. Installing browsers here would add hundreds of megabytes to every pull
# for a binary that is never launched.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Contract: 0.0.0.0:8080, POST /invocations, GET /ping.
EXPOSE 8080

# Single worker on purpose. Each invocation opens a paid browser session and the
# concurrency cap that governs spend lives in aeo-agent-service
# (SOV_GROUND_TRUTH_MAX_CONCURRENCY, a per-pod cap). Adding workers here would
# multiply that cap by the worker count, silently, on the side of the system that has
# no visibility into the budget.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
