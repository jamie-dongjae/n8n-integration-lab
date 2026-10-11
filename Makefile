# n8n-integration-lab — common tasks.
# Docker route is primary; if `docker` is missing we fall back to local Python + npx n8n.
#
# Pinned n8n version: 2.42.6 (compose) / npx uses latest stable.

SHELL := /bin/bash
.ONESHELL:

PY       := .venv/bin/python
PIP      := .venv/bin/pip
UVICORN  := .venv/bin/uvicorn
PYTEST   := .venv/bin/pytest
HAS_DOCKER := $(shell command -v docker >/dev/null 2>&1 && echo yes || echo no)
HAS_NPX    := $(shell command -v npx >/dev/null 2>&1 && echo yes || echo no)

.PHONY: help venv up down logs seed test import-workflows export-workflows bootstrap migrate eval site clean

help:
	@echo "Targets:"
	@echo "  venv              - create .venv and install deps (dev)"
	@echo "  up                - start n8n + mock-apis (docker if present, else npx + uvicorn)"
	@echo "  down              - stop services"
	@echo "  logs              - tail service logs (docker route)"
	@echo "  seed              - deterministic ERP + turnover seed"
	@echo "  test              - pytest"
	@echo "  import-workflows  - import workflows/*.json into n8n"
	@echo "  export-workflows  - export current n8n workflows into workflows/"
	@echo "  bootstrap         - Python port of WF1: ERP -> platform (lets migrate run without n8n)"
	@echo "  migrate           - run turnover migration (expects mock-apis up + platform populated)"
	@echo "  eval              - run triage eval (local rule-based by default)"
	@echo "  site              - regenerate the GitHub Pages report"
	@echo "  clean             - remove venv, caches, generated reports/data"

venv:
	python3 -m venv .venv
	$(PIP) install -q --upgrade pip
	$(PIP) install -q -e ".[dev]"

up:
ifeq ($(HAS_DOCKER),yes)
	docker compose up -d
	@echo "n8n:        http://localhost:5678  (first run: n8n asks you to create an owner account)"
	@echo "mock-apis:  http://localhost:8000/health"
else
ifeq ($(HAS_NPX),yes)
	@echo "Docker not found. Falling back to npx n8n + local uvicorn."
	$(MAKE) up-local
else
	@echo "Neither 'docker' nor 'npx' are on PATH. Install Docker Desktop or Node.js."; exit 1
endif
endif

# Local fallback: run mock-apis with uvicorn in the background, run n8n via npx.
up-local: venv seed
	nohup $(UVICORN) mock_apis.main:app --host 0.0.0.0 --port 8000 > .mock-apis.log 2>&1 &
	@sleep 1
	@curl -fsS http://localhost:8000/health > /dev/null && echo "mock-apis: http://localhost:8000"
	@echo ""
	@echo "Starting n8n via npx — this downloads n8n the first time."
	@echo "Open http://localhost:5678 and create an owner account."
	MOCK_APIS_BASE_URL=http://localhost:8000 npx -y n8n start

down:
ifeq ($(HAS_DOCKER),yes)
	-docker compose down
endif
	-@pkill -f "uvicorn mock_apis.main:app" 2>/dev/null || true

logs:
ifeq ($(HAS_DOCKER),yes)
	docker compose logs -f --tail=200
else
	tail -f .mock-apis.log
endif

seed: venv
	$(PY) scripts/seed.py

test: venv
	$(PYTEST) -q

import-workflows:
ifeq ($(HAS_DOCKER),yes)
	docker compose exec n8n n8n import:workflow --separate --input=/workflows
else
	@echo "No docker. Import JSON files manually in n8n UI (http://localhost:5678 > Workflows > Import)."
endif

export-workflows:
ifeq ($(HAS_DOCKER),yes)
	docker compose exec n8n n8n export:workflow --all --separate --output=/workflows
else
	@echo "No docker. Export from n8n UI into workflows/ instead."
endif

bootstrap: venv
	$(PY) scripts/bootstrap_platform.py --base-url http://localhost:8000 --api-key $${API_KEY_RW:-rw-dev-local-change-me}

migrate: venv
	$(PY) scripts/migrate_turnover.py --base-url http://localhost:8000 --api-key $${API_KEY_RW:-rw-dev-local-change-me}

eval: venv
	$(PY) -m eval.run_eval --mode local

site: venv
	$(PY) scripts/build_site.py

clean:
	rm -rf .venv __pycache__ .pytest_cache *.egg-info **/__pycache__
	rm -f data/lab.db .mock-apis.log site/index.html
	rm -f reports/*.md reports/*.csv
