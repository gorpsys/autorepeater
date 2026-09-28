.PHONY: claude-yandex-archive

claude-yandex-archive:
	rm -rf build/yandex-function build/yandex-function.zip
	mkdir -p build/yandex-function/autorepeater
	cp autorepeater/*.py build/yandex-function/autorepeater/
	cp handler.py requirements.txt build/yandex-function/
	cd build/yandex-function && zip -r ../yandex-function.zip .
