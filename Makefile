# managed: mxm-foundry:begin make v1
# Default to showing help
.DEFAULT_GOAL := help

POETRY ?= poetry
RUN     = $(POETRY) run

.PHONY: help install lock fmt lint type test check build clean \
        deps.outdated deps.update-dev deps.bump-dev ci publish

help:
	@echo "Common targets:"
	@echo "  install        - poetry install (sync lock)"
	@echo "  lock           - refresh lockfile (no version bumps)"
	@echo "  fmt            - ruff --fix, black, isort"
	@echo "  lint           - ruff, black --check, isort --check-only"
	@echo "  type           - pyright (strict by default)"
	@echo "  test           - pytest"
	@echo "  check          - lint + type + test"
	@echo "  deps.outdated  - list outdated deps (incl. dev)"
	@echo "  deps.update-dev- update dev deps within constraints"
	@echo "  deps.bump-dev  - bump dev tool constraints to latest"
	@echo "  build          - poetry build"
	@echo "  clean          - remove build/type/test artifacts"
	@echo "  ci             - alias for 'check'"
	@echo "  publish        - placeholder; use 'poetry publish' or GH release"

install:
	$(POETRY) install --sync

lock:
	$(POETRY) lock --no-update

fmt:
	$(RUN) ruff check . --fix
	$(RUN) black .
	$(RUN) isort .

lint:
	$(RUN) ruff check .
	$(RUN) black --check .
	$(RUN) isort --check-only .

type:
	$(RUN) pyright

test:
	$(RUN) pytest -q

check: lint type test

ci: check

deps.outdated:
	$(POETRY) show --outdated

deps.update-dev:
	$(POETRY) update --group dev

# Bump dev tool constraints to latest stable (and update lock)
deps.bump-dev:
	$(POETRY) add --group dev black@latest isort@latest ruff@latest pyright@latest pytest@latest

build:
	$(POETRY) build

clean:
	rm -rf dist build .pytest_cache .coverage htmlcov .pyright .ruff_cache *.egg-info
# end: mxm-foundry:end
