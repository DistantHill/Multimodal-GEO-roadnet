FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src:/opt/pipeline/src

COPY pyproject.toml ./
COPY src ./src
COPY --from=pipeline_source pyproject.toml /opt/pipeline/pyproject.toml
COPY --from=pipeline_source src /opt/pipeline/src
RUN pip install --no-cache-dir /opt/pipeline
RUN pip install --no-cache-dir --progress-bar off --timeout 600 --retries 10 ".[dev]"

EXPOSE 8000
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
