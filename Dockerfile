# Pin to a digest once you build for real: python:3.12-slim@sha256:<digest>
# (Dependabot below keeps the tag/digest current.)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app
COPY requirements.txt .
# --require-hashes: every package must match the lockfile hash, or the build fails.
RUN pip install --require-hashes --no-deps -r requirements.txt

COPY switchboard ./switchboard
RUN mkdir -p /app/logs && chown app /app/logs

USER 10001
EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=3s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/health', timeout=2)"

# Binding 0.0.0.0 inside the container requires SWITCHBOARD_API_KEY (enforced at start).
CMD ["python", "-m", "switchboard", "--config", "/config/config.yaml", "serve", "--host", "0.0.0.0", "--port", "8787"]
