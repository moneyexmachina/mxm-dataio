# Makefile for mxm-dataio (Poetry-based)

.PHONY: help test test-all lint type check format

# Default target
help:
	@echo "Available targets:"
	@echo "  make test      - run fast unit tests only (excludes integration/slow)"
	@echo "  make test-all  - run full pytest suite (unit + integration + slow)"
	@echo "  make lint      - run ruff lint + format check"
	@echo "  make format    - auto-format code with ruff"
	@echo "  make type      - run pyright type checker"
	@echo "  make check     - run lint + type + unit tests"

# Fast feedback: unit tests only
test:
	poetry run pytest -m "not integration and not slow" -ra -q

# Full suite: includes integration and slow
test-all:
	poetry run pytest -ra -q

# Lint with ruff (style + errors) and check formatting
lint:
	poetry run ruff check .
	poetry run ruff format --check .

# Auto-format code with ruff
format:
	poetry run ruff format .

# Static type checking with pyright
type:
	poetry run pyright

# Pre-commit check: lint + type + unit tests
check: lint type test
