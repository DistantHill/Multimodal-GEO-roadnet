COMPOSE_DEV = docker compose -f infra/docker/compose.dev.yml
COMPOSE_PROD = docker compose -f infra/docker/compose.prod.yml

.PHONY: dev-up dev-down dev-logs dev-config build test pipeline-check pipeline-init-db pipeline-help prod-config

dev-up:
	$(COMPOSE_DEV) up --build

dev-down:
	$(COMPOSE_DEV) down

dev-logs:
	$(COMPOSE_DEV) logs -f

dev-config:
	$(COMPOSE_DEV) config

build:
	$(COMPOSE_DEV) build

test:
	$(COMPOSE_DEV) run --rm backend pytest
	$(COMPOSE_DEV) run --rm pipeline pytest

pipeline-check:
	$(COMPOSE_DEV) run --rm pipeline python -m pipeline check-config

pipeline-init-db:
	$(COMPOSE_DEV) run --rm pipeline python -m pipeline init-db

pipeline-help:
	$(COMPOSE_DEV) run --rm pipeline python -m pipeline run --help

prod-config:
	$(COMPOSE_PROD) config
