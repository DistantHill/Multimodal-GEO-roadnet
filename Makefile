COMPOSE_DEV = docker compose -f infra/docker/compose.dev.yml
COMPOSE_PROD = docker compose -f infra/docker/compose.prod.yml
VISION_IMAGE = roadnet-vision-yolov8:8.3.0-cu117

.PHONY: dev-up dev-down dev-logs dev-config build test pipeline-check pipeline-init-db pipeline-help vision-build vision-check vision-run prod-config

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

vision-build:
	$(COMPOSE_DEV) --profile vision build vision

vision-check:
	docker run --rm -v "$(CURDIR)/models/yolo:/models/yolo:ro" $(VISION_IMAGE) python -c "import numpy as np; from ultralytics import YOLO; model = YOLO('/models/yolo/yolov8n.pt'); results = model(np.zeros((64, 64, 3), dtype=np.uint8), device='cpu', verbose=False); print('YOLOv8 CPU smoke test:', len(results), 'result')"

vision-run:
	$(COMPOSE_DEV) --profile vision run --rm vision

prod-config:
	$(COMPOSE_PROD) config
