# Standalone core runtime; no database, frontend, backend or online extension downloads.
FROM python:3.12-slim-bookworm AS builder
RUN apt-get update && apt-get install --yes --no-install-recommends g++ \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY pyproject.toml setup.py MANIFEST.in ./
COPY build-requirements.lock requirements.lock ./
COPY native ./native
COPY src ./src
RUN pip install --no-cache-dir --require-hashes -r build-requirements.lock \
    && pip download --no-cache-dir --require-hashes --dest /wheels -r requirements.lock \
    && pip wheel --no-cache-dir --no-deps --no-build-isolation --wheel-dir /wheels .

FROM python:3.12-slim-bookworm AS runtime
RUN apt-get update && apt-get install --yes --no-install-recommends libstdc++6 libexpat1 libgoogle-perftools4 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels 'roadnet-pipeline[postgres]' \
    && rm -rf /wheels
WORKDIR /work
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "-m", "pipeline"]
CMD ["--help"]

FROM runtime AS development
COPY --from=builder /wheels /wheels
COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --no-index --find-links=/wheels --require-hashes -r /tmp/requirements.lock \
    && rm -rf /wheels /tmp/requirements.lock

FROM development AS debug
RUN groupadd --gid 1000 roadnet && useradd --uid 1000 --gid 1000 --create-home roadnet
USER roadnet

FROM runtime AS production
