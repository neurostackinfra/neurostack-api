# ============================================
# NeuroStack API - Makefile
# ============================================

.PHONY: help install dev test lint format clean docker-up docker-down run typecheck-poc-runner

# Default target
help:
	@echo "NeuroStack API - Available Commands"
	@echo "===================================="
	@echo ""
	@echo "Development:"
	@echo "  make install     - Install dependencies with Poetry"
	@echo "  make dev         - Install dev dependencies"
	@echo "  make run         - Run the API server"
	@echo "  make run-reload  - Run with auto-reload"
	@echo ""
	@echo "Testing:"
	@echo "  make test        - Run all tests"
	@echo "  make test-cov    - Run tests with coverage"
	@echo "  make test-unit   - Run unit tests only"
	@echo ""
	@echo "Load Testing:"
	@echo "  make load-test-smoke     - Quick validation (10 users, 1 min)"
	@echo "  make load-test           - Standard load test (100 users, 5 min)"
	@echo "  make load-test-stress    - Stress test (500 users, 10 min)"
	@echo "  make load-test-benchmark - Target 1000 req/s"
	@echo "  make load-test-ui        - Run Locust with web UI"
	@echo "  make load-analyze        - Analyze load test results"
	@echo ""
	@echo "Code Quality:"
	@echo "  make lint        - Run linters"
	@echo "  make format      - Format code"
	@echo "  make typecheck   - Run type checking"
	@echo "  make quality     - Run all quality checks"
	@echo ""
	@echo "Docker:"
	@echo "  make docker-up   - Start all services"
	@echo "  make docker-down - Stop all services"
	@echo "  make docker-logs - View logs"
	@echo "  make docker-build- Build Docker image"
	@echo ""
	@echo "Database:"
	@echo "  make db-migrate  - Run database migrations"
	@echo "  make db-reset    - Reset database"
	@echo ""
	@echo "Cleanup:"
	@echo "  make clean       - Clean build artifacts"

# ============================================
# Installation
# ============================================

install:
	poetry install --no-root

dev:
	poetry install

# ============================================
# Running
# ============================================

run:
	poetry run uvicorn src.api.main:app --host 0.0.0.0 --port 8000

run-reload:
	poetry run uvicorn src.api.main:app --host 0.0.0.0 --port 8000 --reload

# ============================================
# Testing
# ============================================

test:
	poetry run pytest tests/ -v

test-cov:
	poetry run pytest tests/ --cov=src --cov-report=html --cov-report=term-missing -v

test-unit:
	poetry run pytest tests/ -m unit -v

test-integration:
	poetry run pytest tests/ -m integration -v

test-e2e:
	poetry run pytest tests/ -m e2e -v

# ============================================
# Load Testing & Performance
# ============================================

load-test-smoke:
	@echo "Running smoke load test (10 users, 1 min)..."
	poetry run python tests/load/run_load_tests.py --scenario smoke

load-test:
	@echo "Running standard load test (100 users, 5 min)..."
	poetry run python tests/load/run_load_tests.py --scenario load

load-test-stress:
	@echo "Running stress test (500 users, 10 min)..."
	poetry run python tests/load/run_load_tests.py --scenario stress

load-test-benchmark:
	@echo "Running benchmark test (target: 1000 req/s)..."
	poetry run python tests/load/run_load_tests.py --scenario benchmark

load-test-ui:
	@echo "Starting Locust web UI at http://localhost:8089..."
	poetry run locust -f tests/load/locustfile.py --host=http://localhost:8000

load-analyze:
	@echo "Analyzing load test results..."
	poetry run python tests/load/bottleneck_analyzer.py --csv-dir load_test_results

# ============================================
# Code Quality
# ============================================

lint:
	poetry run flake8 src packages/neurostack-core/neurostack_core tests
	poetry run mypy src packages/neurostack-core/neurostack_core --ignore-missing-imports

format:
	poetry run black src packages/neurostack-core/neurostack_core tests
	poetry run isort src packages/neurostack-core/neurostack_core tests

format-check:
	poetry run black --check src packages/neurostack-core/neurostack_core tests
	poetry run isort --check-only src packages/neurostack-core/neurostack_core tests

typecheck:
	poetry run mypy src packages/neurostack-core/neurostack_core --ignore-missing-imports

typecheck-poc-runner:
	poetry run mypy --ignore-missing-imports scripts/poc_functional_regression.py tests/scripts/test_poc_functional_regression.py

quality: format-check lint typecheck typecheck-poc-runner test

# ============================================
# Pre-commit
# ============================================

pre-commit-install:
	poetry run pre-commit install

pre-commit:
	poetry run pre-commit run --all-files

# ============================================
# Docker
# ============================================

docker-up:
	docker compose up -d

docker-down:
	docker compose down

docker-logs:
	docker compose logs -f

docker-build:
	docker compose build

docker-rebuild:
	docker compose up -d --build

docker-clean:
	docker compose down -v --rmi local

# Start only infrastructure (for local development)
docker-infra:
	docker compose up -d postgres redis qdrant

# ============================================
# Database
# ============================================

db-migrate:
	poetry run alembic upgrade head

db-rollback:
	poetry run alembic downgrade -1

db-reset:
	docker compose exec postgres psql -U neurostack -d neurostack -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
	poetry run alembic upgrade head

# ============================================
# Cleanup
# ============================================

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".mypy_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "htmlcov" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name ".coverage" -delete 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "dist" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "build" -exec rm -rf {} + 2>/dev/null || true

# ============================================
# Utilities
# ============================================

shell:
	poetry shell

deps-update:
	poetry update

deps-show:
	poetry show --tree
