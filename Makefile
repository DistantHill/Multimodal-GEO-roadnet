PYTHON ?= python3
PIPELINE_CONFIG ?= $(CURDIR)/pipeline.toml
BUILD_ROOT ?= /tmp/roadnet-workbench-build

.PHONY: install build test pipeline-check pipeline-run pipeline-tune pipeline-preview pipeline-labels pipeline-inspect pipeline-help

install:
	$(PYTHON) -m pip install --require-hashes -r services/pipeline/build-requirements.lock -r services/pipeline/requirements.lock
	$(PYTHON) services/pipeline/scripts/build_workbench.py --output "$(BUILD_ROOT)" --install

build:
	$(PYTHON) -m pip install --require-hashes -r services/pipeline/build-requirements.lock
	$(PYTHON) services/pipeline/scripts/build_workbench.py --output "$(BUILD_ROOT)"

# Tests generate all data under pytest's temporary directory, never require PG/UI.
test:
	$(PYTHON) -m pytest services/pipeline/tests/workbench -q -p no:cacheprovider

pipeline-check:
	$(PYTHON) -m pipeline check-config --config "$(PIPELINE_CONFIG)"

pipeline-run:
	$(PYTHON) -m pipeline run --config "$(PIPELINE_CONFIG)"

pipeline-tune:
	$(PYTHON) -m pipeline tune --config "$(PIPELINE_CONFIG)"

pipeline-preview:
	$(PYTHON) -m pipeline preview-labels --config "$(PIPELINE_CONFIG)"

pipeline-labels:
	$(PYTHON) -m pipeline list-labels --config "$(PIPELINE_CONFIG)"

pipeline-inspect:
	$(PYTHON) -m pipeline inspect --config "$(PIPELINE_CONFIG)"

pipeline-help:
	$(PYTHON) -m pipeline --help
