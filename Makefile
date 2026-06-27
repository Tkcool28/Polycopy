.PHONY: help dev-install audit lint test clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-15s\033[0m %s\n", $$1, $$2}'

dev-install: ## Install with dev deps
	pip install -e ".[dev]"

audit: ## Run data capability probe
	python scripts/probe_polymarket.py

lint: ## Run ruff
	ruff check src tests

test: ## Run pytest
	pytest tests/

clean: ## Remove build artifacts
	rm -rf .pytest_cache .ruff_cache *.egg-info build dist
	find . -type d -name __pycache__ -exec rm -rf {} +
