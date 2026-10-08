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

pipeline-smoke-test: validate-pipeline-smoke-selection
	$(MAKE) --no-print-directory services-up
	$(COMPOSE_DEV)  -f infra/docker/compose.smoke.yml \
	 run --rm -e PIPELINE_CONFIG_PATH=/app/config/pipeline-roadnet-test.toml \
	 pipeline python -m pipeline run $(PIPELINE_SELECTION_ARGS)

pipeline-full-test: validate-pipeline-full-selection
	$(MAKE) --no-print-directory services-up
	$(COMPOSE_DEV) run --rm -e PIPELINE_CONFIG_PATH=/app/config/pipeline.toml pipeline python -m pipeline run $(PIPELINE_SELECTION_ARGS) --time-start "$(FULL_TEST_TIME_START)" --time-end "$(FULL_TEST_TIME_END)"

# Full data runs select March 1 in the source dataset's Beijing timezone.
FULL_TEST_TIME_START ?= 2023-03-01T00:00:00+08:00
FULL_TEST_TIME_END ?= 2023-03-02T00:00:00+08:00

PIPELINE_SELECTION_ARGS = $(if $(strip $(SOURCE_TABLE)),--source-table "$(SOURCE_TABLE)",$(if $(strip $(REFINE_ID)),--refine-id "$(REFINE_ID)")) $(if $(filter undefined,$(origin REFINE_RESUME_LAST)),,--refine-resume-last "$(REFINE_RESUME_LAST)")

.PHONY: validate-pipeline-smoke-selection validate-pipeline-full-selection
validate-pipeline-smoke-selection:
	$(if $(and $(strip $(SOURCE_TABLE)),$(strip $(REFINE_ID))),$(error SOURCE_TABLE 与 REFINE_ID 只能指定一个))
	$(if $(and $(filter-out undefined,$(origin REFINE_ID)),$(if $(strip $(REFINE_ID)),,true)),$(error REFINE_ID 不能为空))
	$(if $(and $(filter-out undefined,$(origin SOURCE_TABLE)),$(if $(strip $(SOURCE_TABLE)),,true)),$(error SOURCE_TABLE 不能为空))
	$(if $(and $(filter-out undefined,$(origin REFINE_RESUME_LAST)),$(if $(strip $(REFINE_RESUME_LAST)),,true)),$(error REFINE_RESUME_LAST 不能为空))
	$(if $(and $(filter-out undefined,$(origin REFINE_RESUME_LAST)),$(filter-out 1,$(words $(strip $(REFINE_RESUME_LAST))))),$(error REFINE_RESUME_LAST 必须是单个 true 或 false))
	$(if $(and $(filter-out undefined,$(origin REFINE_RESUME_LAST)),$(filter-out true false,$(REFINE_RESUME_LAST))),$(error REFINE_RESUME_LAST 只能是 true 或 false))
	$(if $(and $(filter-out undefined,$(origin REFINE_RESUME_LAST)),$(strip $(REFINE_ID))),$(error REFINE_RESUME_LAST 只能与 SOURCE_TABLE 配合使用))
	$(if $(or $(strip $(SOURCE_TABLE)),$(strip $(REFINE_ID)),$(filter true,$(REFINE_RESUME_LAST))),,$(error 请指定 SOURCE_TABLE、REFINE_ID，或 REFINE_RESUME_LAST=true))
	@:

validate-pipeline-full-selection:
	$(if $(strip $(FULL_TEST_TIME_START)),,$(error FULL_TEST_TIME_START 不能为空))
	$(if $(strip $(FULL_TEST_TIME_END)),,$(error FULL_TEST_TIME_END 不能为空))
	$(if $(and $(strip $(SOURCE_TABLE)),$(strip $(REFINE_ID))),$(error SOURCE_TABLE 与 REFINE_ID 只能指定一个))
	$(if $(and $(filter-out undefined,$(origin REFINE_ID)),$(if $(strip $(REFINE_ID)),,true)),$(error REFINE_ID 不能为空))
	$(if $(and $(filter-out undefined,$(origin SOURCE_TABLE)),$(if $(strip $(SOURCE_TABLE)),,true)),$(error SOURCE_TABLE 不能为空))
	$(if $(and $(filter-out undefined,$(origin REFINE_RESUME_LAST)),$(if $(strip $(REFINE_RESUME_LAST)),,true)),$(error REFINE_RESUME_LAST 不能为空))
	$(if $(and $(filter-out undefined,$(origin REFINE_RESUME_LAST)),$(filter-out 1,$(words $(strip $(REFINE_RESUME_LAST))))),$(error REFINE_RESUME_LAST 必须是单个 true 或 false))
	$(if $(and $(filter-out undefined,$(origin REFINE_RESUME_LAST)),$(filter-out true false,$(REFINE_RESUME_LAST))),$(error REFINE_RESUME_LAST 只能是 true 或 false))
	$(if $(and $(filter-out undefined,$(origin REFINE_RESUME_LAST)),$(strip $(REFINE_ID))),$(error REFINE_RESUME_LAST 只能与 SOURCE_TABLE 配合使用))
	$(if $(or $(strip $(SOURCE_TABLE)),$(strip $(REFINE_ID)),$(filter true,$(REFINE_RESUME_LAST))),,$(error 请指定 SOURCE_TABLE、REFINE_ID，或 REFINE_RESUME_LAST=true))
	@:

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

.PHONY: pipeline-experiment
EXPERIMENT_CONFIG_PATH ?= /data/experiments/experiment.toml
PIPELINE_GIT_COMMIT = $(shell git rev-parse HEAD)
# Draft configuration must remain editable by the host user.
EXPERIMENT_DRAFT_USER = $(if $(filter init restore,$(firstword $(ARGS))),--user "$(shell id -u):$(shell id -g)")
pipeline-experiment:
	$(COMPOSE_DEV) run --rm $(EXPERIMENT_DRAFT_USER) -e PIPELINE_GIT_COMMIT="$(PIPELINE_GIT_COMMIT)" pipeline python -m pipeline experiment --config "$(EXPERIMENT_CONFIG_PATH)" $(ARGS)

vision-build:
	$(COMPOSE_DEV) --profile vision build vision

vision-check:
	docker run --rm -v "$(CURDIR)/models/yolo:/models/yolo:ro" $(VISION_IMAGE) python -c "import numpy as np; from ultralytics import YOLO; model = YOLO('/models/yolo/yolov8n.pt'); results = model(np.zeros((64, 64, 3), dtype=np.uint8), device='cpu', verbose=False); print('YOLOv8 CPU smoke test:', len(results), 'result')"

vision-run:
	$(COMPOSE_DEV) --profile vision run --rm vision

prod-config:
	$(COMPOSE_PROD) config
