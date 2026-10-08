.PHONY: setup lint typecheck test coverage e2e claude-yandex-archive \
        check-lint check-typecheck check-test check-coverage check-e2e

PYTHON ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)
VENV_PYTHON ?= python3
SANDBOX_TOKEN_FILE ?= .sandbox_token
SANDBOX_RUN = $(PYTHON) -m scripts.sandbox_token --token-file "$(SANDBOX_TOKEN_FILE)" --
MAKE_OFFLINE := $(abspath $(dir $(lastword $(MAKEFILE_LIST)))scripts/make_offline.py)
MAKE_ENVIRONMENT := $(abspath $(dir $(lastword $(MAKEFILE_LIST)))scripts/make_environment.py)
E2E_TIMEOUT ?= 1800
E2E_ARGS ?=

setup:
	"$(VENV_PYTHON)" -m venv .venv
	.venv/bin/python -m pip install -r requirements-dev.txt

check-lint check-typecheck check-test check-coverage check-e2e:
	"$(PYTHON)" "$(MAKE_ENVIRONMENT)" $(patsubst check-%,%,$@)

lint: check-lint
	"$(PYTHON)" "$(MAKE_OFFLINE)" --python "$(PYTHON)" --root "$(CURDIR)" lint

typecheck: check-typecheck
	"$(PYTHON)" "$(MAKE_OFFLINE)" --python "$(PYTHON)" --root "$(CURDIR)" typecheck

test: check-test
	"$(PYTHON)" "$(MAKE_OFFLINE)" --python "$(PYTHON)" --root "$(CURDIR)" test

coverage: check-coverage
	"$(PYTHON)" "$(MAKE_OFFLINE)" --python "$(PYTHON)" --root "$(CURDIR)" coverage

e2e: check-e2e
	$(SANDBOX_RUN) "$(PYTHON)" -m scripts.sandbox_e2e --timeout "$(E2E_TIMEOUT)" -- $(E2E_ARGS)

claude-yandex-archive:
	"$(PYTHON)" scripts/build_yandex_archive.py
