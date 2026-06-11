FROM python:3.12-slim

WORKDIR /app

RUN pip install uv

COPY backend/pyproject.toml .
RUN uv pip install --system -e .

COPY backend/ .

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--forwarded-allow-ips", "*"]
