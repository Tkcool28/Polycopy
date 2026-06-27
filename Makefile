.PHONY: help dev-install audit lint lint-fix test test-frontend frontend-install frontend-lint frontend-typecheck frontend-test frontend-build clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

dev-install: ## Install with dev deps
	pip install -e ".[dev]"

audit: ## Run data capability probe
	python scripts/probe_polymarket.py

lint: ## Run ruff
	ruff check src tests

lint-fix: ## Auto-fix ruff issues
	ruff check src tests --fix --unsafe-fixes

test: ## Run pytest
	pytest tests/

test-frontend: ## Run frontend tests
	cd frontend && npm test

frontend-install: ## Install frontend deps
	cd frontend && npm ci

frontend-lint: ## Lint frontend
	cd frontend && npm run lint

frontend-typecheck: ## Type-check frontend
	cd frontend && npm run typecheck

frontend-test: ## Test frontend
	cd frontend && npm test

frontend-build: ## Build frontend
	cd frontend && npm run build

clean: ## Remove build artifacts
	rm -rf .pytest_cache .ruff_cache *.egg-info build dist
	rm -rf frontend/dist frontend/node_modules/.cache
	find . -type d -name __pycache__ -exec rm -rf {} +
