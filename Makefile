.PHONY: setup lint typecheck test coverage e2e claude-yandex-archive \
        check-lint check-typecheck check-test check-coverage check-e2e \
        update-runtime-lock check-runtime-lock check-cloud-package

PYTHON ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)
VENV_PYTHON ?= python3
SANDBOX_TOKEN_FILE ?= .sandbox_token
SANDBOX_RUN = $(PYTHON) -m scripts.sandbox_token --token-file "$(SANDBOX_TOKEN_FILE)" --
MAKE_OFFLINE := $(abspath $(dir $(lastword $(MAKEFILE_LIST)))scripts/make_offline.py)
MAKE_ENVIRONMENT := $(abspath $(dir $(lastword $(MAKEFILE_LIST)))scripts/make_environment.py)
E2E_TIMEOUT ?= 1800
E2E_ARGS ?=
USE_VENDORED_SDK ?= 0
OFFLINE_ARCHIVE ?= 0
CLOUD_PACKAGE_SHA ?=

ifeq ($(USE_VENDORED_SDK),0)
ARCHIVE_SDK_ARGUMENT =
else ifeq ($(USE_VENDORED_SDK),1)
ARCHIVE_SDK_ARGUMENT = --use-vendored-sdk
else
ARCHIVE_SDK_ARGUMENT = $(error USE_VENDORED_SDK must be 0 or 1)
endif

ifeq ($(OFFLINE_ARCHIVE),0)
ARCHIVE_OFFLINE_ARGUMENT =
else ifeq ($(OFFLINE_ARCHIVE),1)
ARCHIVE_OFFLINE_ARGUMENT = --offline-archive
else
ARCHIVE_OFFLINE_ARGUMENT = $(error OFFLINE_ARCHIVE must be 0 or 1)
endif

setup:
	"$(VENV_PYTHON)" -m venv .venv
	.venv/bin/python -m scripts.install_dependencies --development

update-runtime-lock:
	"$(PYTHON)" -m scripts.update_runtime_lock

check-runtime-lock:
	"$(PYTHON)" -m scripts.runtime_lock

check-cloud-package:
	"$(PYTHON)" -m scripts.check_cloud_bundle --expected-sha "$(CLOUD_PACKAGE_SHA)"

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
	"$(PYTHON)" scripts/build_yandex_archive.py $(ARCHIVE_SDK_ARGUMENT) $(ARCHIVE_OFFLINE_ARGUMENT)
