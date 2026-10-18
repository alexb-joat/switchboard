# Pinned to a digest: a tag can be re-pointed, a digest cannot (checked 2026-10-14).
# Dependabot (docker ecosystem) proposes new digests; the tag stays for readability.
FROM python:3.14-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # metrics.db, budget.json, prices.json: the app user has no home, so keep them in the logs volume
    HLIDSKJALV_HOME=/app/logs/hlidskjalv

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app
COPY requirements.txt .
# --require-hashes: every package must match the lockfile hash, or the build fails.
RUN pip install --require-hashes --no-deps -r requirements.txt

COPY trioditis ./trioditis
COPY trioditis_metrics ./trioditis_metrics
RUN mkdir -p /app/logs && chown app /app/logs

USER 10001
EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=3s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/health', timeout=2)"

# Binding 0.0.0.0 inside the container requires TRIODITIS_API_KEY (enforced at start).
CMD ["python", "-m", "trioditis", "--config", "/config/config.yaml", "serve", "--host", "0.0.0.0", "--port", "8787"]
