# Makefile for easy development workflows.
# See development.md for docs.
# Note GitHub Actions call uv directly, not this Makefile.

.DEFAULT_GOAL := default

.PHONY: default install lint test test-prove test-hypothesis test-integration test-soundness test-fuzz test-parity verify upgrade build clean docs-clean docs-serve docs-build docs-check bench

default: install verify

install:
	uv sync --locked --all-extras --dev

lint:
	uv run devtools/lint.py

test:
	uv run pytest -m "not integration and not hypothesis and not soundness and not fuzz" --ignore=tests/fuzz --runner-backend=both

test-prove:
	uv run pytest tests/core/analysis/ -k "prove or elision_agreement or packml_diagnosis" -q

test-hypothesis:
	uv run pytest -m hypothesis

test-integration:
	uv run pytest -m integration

test-soundness:
	uv run pytest tests/core/analysis/ --prove-agreement -q

test-fuzz:
	uv run pytest tests/fuzz/

test-parity:
	uv run pytest -m parity

verify: lint test test-hypothesis docs-check

upgrade:
	uv lock --upgrade
	uv sync --locked --all-extras --dev

build:
	uv build

bench:
	uv run pyrung lock examples.packml_bench -o bench/pyrung.lock --profile bench/bench.prof

# Improved Windows detection
ifeq ($(OS),Windows_NT)
    WINDOWS := 1
else
    ifeq ($(shell uname -s),Windows)
        WINDOWS := 1
    else
        WINDOWS := 0
    endif
endif

ifeq ($(WINDOWS),1)
	# Windows commands
	RM = powershell -Command "Remove-Item -Recurse -Force"
	RM_SITE = powershell -Command "if (Test-Path 'site') { Remove-Item -Recurse -Force 'site' }"
	FIND_PYCACHE = powershell -Command "Get-ChildItem -Path . -Filter '__pycache__' -Recurse -Directory | Remove-Item -Recurse -Force"
	DOCS_ENV = set DISABLE_MKDOCS_2_WARNING=true&&
else
    # Unix commands
    RM = rm -rf
    RM_SITE = rm -rf site/
    FIND_PYCACHE = find . -type d -name "__pycache__" -exec rm -rf {} +
    DOCS_ENV = DISABLE_MKDOCS_2_WARNING=true
endif

docs-serve:
	$(DOCS_ENV) uv run --group docs mkdocs serve

docs-clean:
	$(RM_SITE)

docs-build: docs-clean
	$(DOCS_ENV) uv run --group docs mkdocs build --strict

docs-check: docs-build

clean:
	$(RM) dist/
	$(RM) *.egg-info/
	$(RM) .pytest_cache/
	$(RM) .mypy_cache/
	$(RM) .venv/
	$(FIND_PYCACHE)
