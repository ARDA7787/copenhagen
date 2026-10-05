# Copenhagen developer commands. Run `make help` for the list.
.DEFAULT_GOAL := help
SHELL := /bin/bash

UV ?= uv
COMPOSE ?= docker compose
Q ?=

.PHONY: help up down dev worker check test test-int e2e demo fmt migrate docker-ok

help: ## List targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-10s %s\n", $$1, $$2}'

docker-ok:
	@docker info >/dev/null 2>&1 || { \
		echo "Docker daemon is not running. Start Docker Desktop, then re-run this command." >&2; exit 1; }

up: docker-ok ## Start Postgres and the Temporal dev server, wait until healthy
	$(COMPOSE) up -d --wait
	@echo "Postgres: localhost:5432   Temporal: localhost:7233   Temporal UI: http://localhost:8233"

down: docker-ok ## Stop the stack (named volumes are kept)
	$(COMPOSE) down

dev: ## Run the API plus the control worker (Phase 1+)
	@echo "Not yet: the API arrives in Phase 1 (P1-10)." >&2; exit 1

worker: ## Run a domain worker: make worker Q=people (Phase 3+)
	@test -n "$(Q)" || { echo "Usage: make worker Q=<queue>" >&2; exit 1; }
	@echo "Not yet: domain workers arrive in Phase 3 (P3-12)." >&2; exit 1

check: ## Lint, format check, types, import rules, unit tests, .pth check (pre-commit runs this)
	$(UV) run ruff check .
	$(UV) run ruff format --check .
	$(UV) run pyright
	$(UV) run lint-imports
	$(UV) run pytest -q
	$(UV) run python scripts/check_pth.py

test: check ## Alias for check

test-int: ## Integration tests against `make up` (Postgres, Temporal)
	$(UV) run pytest -q -m integration

e2e: ## End-to-end demo tests: make e2e PHASE=N (Phase 3+)
	$(UV) run pytest -q -m e2e $(if $(PHASE),tests/e2e/test_phase_$(PHASE)_demo.py,)

demo: ## Seed and run the guided demo (Milestone A)
	@echo "Not yet: the guided demo arrives in Phase 4 (P4-15)." >&2; exit 1

fmt: ## Auto-fix lint and format
	$(UV) run ruff check --fix .
	$(UV) run ruff format .

migrate: ## Apply database migrations (Phase 1+)
	@echo "Not yet: Alembic arrives in Phase 1 (P1-07)." >&2; exit 1
