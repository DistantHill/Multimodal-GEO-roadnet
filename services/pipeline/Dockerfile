FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir --progress-bar off --timeout 600 --retries 10 ".[dev]"

CMD ["python", "-m", "pipeline", "serve"]
