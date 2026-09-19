FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

COPY requirements.lock build-requirements.lock ./
RUN pip install --no-cache-dir --progress-bar off --timeout 600 --retries 10 \
    --require-hashes -r requirements.lock \
    && pip install --no-cache-dir --progress-bar off --timeout 600 --retries 10 \
    --require-hashes -r build-requirements.lock

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir --progress-bar off --no-deps --no-build-isolation .

CMD ["python", "-m", "pipeline", "serve"]
