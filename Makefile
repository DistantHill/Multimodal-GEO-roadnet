COMPOSE_DEV = docker compose --env-file .env -f infra/docker/compose.dev.yml
COMPOSE_PROD = docker compose --env-file .env -f infra/docker/compose.prod.yml
COMPOSE_SERVICES = docker compose --env-file /data/services/.env -f /data/services/compose.yml
VISION_IMAGE = roadnet-vision-yolov8:8.3.0-cu117

.PHONY: services-up services-down services-logs services-config dev-up dev-down dev-logs dev-config build test pipeline-debug pipeline-smoke-test pipeline-full-test frontend-test qgis-plugin-test pipeline-check pipeline-init-db pipeline-help vision-build vision-check vision-run prod-config

services-up:
	$(COMPOSE_SERVICES) up -d --wait

services-down:
	$(COMPOSE_SERVICES) down

services-logs:
	$(COMPOSE_SERVICES) logs -f

services-config:
	$(COMPOSE_SERVICES) config

dev-up: services-up
	$(COMPOSE_DEV) up --build

dev-down:
	$(COMPOSE_DEV) down

dev-logs:
	$(COMPOSE_DEV) logs -f

dev-config:
	$(COMPOSE_DEV) config

build:
	$(COMPOSE_DEV) build

test: compose-test services-up
	$(COMPOSE_DEV) run --rm backend pytest
	$(COMPOSE_DEV) run --rm pipeline python -m pytest tests scripts/cv_human_check/tests

pipeline-debug: services-up
	$(COMPOSE_DEV) run --rm --entrypoint /bin/sh pipeline

pipeline-smoke-test: services-up
	$(COMPOSE_DEV) run --rm -e PIPELINE_CONFIG_PATH=/app/config/pipeline-roadnet-test.toml pipeline python -m pytest tests scripts/cv_human_check/tests

pipeline-full-test: services-up
	$(COMPOSE_DEV) run --rm -e PIPELINE_CONFIG_PATH=/app/config/pipeline.toml pipeline python -m pytest tests scripts/cv_human_check/tests

.PHONY: compose-test
compose-test:
	python3 -m unittest discover -s infra/docker/tests -v

frontend-test:
	cd apps/frontend && npm test && npm run build

qgis-plugin-test:
	python3 -m unittest discover -s apps/qgis-plugin/tests -v
	python3 -m compileall -q apps/qgis-plugin/roadnet_tools

pipeline-check:
	$(COMPOSE_DEV) run --rm pipeline python -m pipeline check-config

pipeline-init-db:
	$(COMPOSE_DEV) run --rm pipeline python -m pipeline init-db

pipeline-help:
	$(COMPOSE_DEV) run --rm pipeline python -m pipeline run --help

vision-build:
	$(COMPOSE_DEV) --profile vision build vision

vision-check:
	docker run --rm -v "$(CURDIR)/models/yolo:/models/yolo:ro" $(VISION_IMAGE) python -c "import numpy as np; from ultralytics import YOLO; model = YOLO('/models/yolo/yolov8n.pt'); results = model(np.zeros((64, 64, 3), dtype=np.uint8), device='cpu', verbose=False); print('YOLOv8 CPU smoke test:', len(results), 'result')"

vision-run:
	$(COMPOSE_DEV) --profile vision run --rm vision

prod-config:
	$(COMPOSE_PROD) config
