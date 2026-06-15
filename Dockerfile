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
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--forwarded-allow-ips", "*"]
