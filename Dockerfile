FROM python:3.12-slim

WORKDIR /app

RUN pip install uv

# Install pinned dependencies from the lockfile for reproducible builds.
# --frozen fails the build if uv.lock is stale vs pyproject.toml.
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv export --frozen --no-dev --no-emit-project --format requirements-txt -o /tmp/requirements.txt \
    && uv pip install --system -r /tmp/requirements.txt

COPY backend/ .

EXPOSE 8000
# NOTE: forwarded headers are resolved by the app (TRUSTED_PROXY_COUNT /
# TRUST_CLOUDFLARE), not uvicorn — so uvicorn must NOT rewrite client.host from
# X-Forwarded-* (default forwarded-allow-ips=127.0.0.1 keeps client.host = real peer).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
