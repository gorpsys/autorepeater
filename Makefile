.PHONY: claude-yandex-archive

PYTHON ?= python3

claude-yandex-archive:
	$(PYTHON) scripts/build_yandex_archive.py
