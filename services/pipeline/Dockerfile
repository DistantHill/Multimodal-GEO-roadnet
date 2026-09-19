FROM python:3.12-slim AS duckdb-extension-builder

ARG TARGETARCH
ARG DUCKDB_POSTGRES_EXTENSION_URL="https://extensions.duckdb.org/v1.5.5/linux_amd64/postgres_scanner.duckdb_extension.gz"
ARG DUCKDB_POSTGRES_EXTENSION_GZIP_SHA256="e0f631a5535f165468bc8a20501f8bc1490adbc877d38fcdff2f8d05531e1e5b"
ARG DUCKDB_POSTGRES_EXTENSION_SHA256="b1ced4cfc6311313e117c2afb3eac76508718778dde0716421503c7dbfb5605c"

RUN detected_arch="$(dpkg --print-architecture)" \
    && test "$detected_arch" = "amd64" \
    && { test -z "$TARGETARCH" || test "$TARGETARCH" = "amd64"; } \
    || { echo "Unsupported Pipeline image architecture: ${TARGETARCH:-$detected_arch} (expected amd64)" >&2; exit 1; } \
    && apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /opt/duckdb/extensions/v1.5.5/linux_amd64 \
    && curl --fail --location --retry 5 --output /tmp/postgres_scanner.duckdb_extension.gz \
        "$DUCKDB_POSTGRES_EXTENSION_URL" \
    && echo "$DUCKDB_POSTGRES_EXTENSION_GZIP_SHA256  /tmp/postgres_scanner.duckdb_extension.gz" \
        | sha256sum --check --strict - \
    && gzip --decompress /tmp/postgres_scanner.duckdb_extension.gz \
    && echo "$DUCKDB_POSTGRES_EXTENSION_SHA256  /tmp/postgres_scanner.duckdb_extension" \
        | sha256sum --check --strict - \
    && mv /tmp/postgres_scanner.duckdb_extension \
        /opt/duckdb/extensions/v1.5.5/linux_amd64/postgres_scanner.duckdb_extension

FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    DUCKDB_POSTGRES_EXTENSION_PATH=/opt/duckdb/extensions/v1.5.5/linux_amd64/postgres_scanner.duckdb_extension

COPY --from=duckdb-extension-builder /opt/duckdb/extensions /opt/duckdb/extensions

COPY requirements.lock build-requirements.lock ./
RUN pip install --no-cache-dir --progress-bar off --timeout 600 --retries 10 \
    --require-hashes -r requirements.lock \
    && pip install --no-cache-dir --progress-bar off --timeout 600 --retries 10 \
    --require-hashes -r build-requirements.lock

COPY pyproject.toml ./
COPY src ./src
COPY scripts ./scripts
RUN pip install --no-cache-dir --progress-bar off --no-deps --no-build-isolation .

CMD ["python", "-m", "pipeline", "serve"]
