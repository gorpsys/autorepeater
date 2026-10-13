# Технические команды

Все команды ниже выполняются из корня репозитория. Описание приложения и
торгового запуска находится в [README](../README.md).

## Окружение и проверки

Проверенная установка runtime поддерживает CPython 3.12 на Linux x86_64/amd64,
glibc >=2.17. Musl/Alpine, PyPy и другие профили не покрываются текущим lock.
Нужны Python с venv/pip, Git, Make и доступ к PyPI. Сборка из source и Docker
не используются.

```bash
make setup VENV_PYTHON=python3.12
make check-runtime-lock
make lint
make typecheck
make test
make coverage
git diff --check
```

`VENV_PYTHON` создаёт `.venv`; `PYTHON` выбирает интерпретатор остальных целей.
По умолчанию Make выбирает `.venv/bin/python`, затем `python3` из PATH.
Для уже выбранного внешнего окружения:

```bash
/path/to/venv/bin/python -m scripts.install_dependencies --development
make test PYTHON=/path/to/venv/bin/python
```

Helper до установки требует только stdlib и собственные helpers. Runtime
устанавливается из проверенных wheels с force-reinstall, затем инструменты
разработки под constraints. Неудачная установка не оставляет success receipt.
Make lint/typecheck/test/coverage/e2e проверяет runtime graph и schema2 receipt:
хеши установленных файлов и их состав. Замена пакета другими байтами той же
версии требует повторного verified setup; прежний schema1 receipt недействителен.
missing/stale receipt или drift требуют повторной установки helper.
Конфликт с посторонними пакетами требует чистого окружения: helper их не удаляет.

Для установки только runtime используется
`python -m scripts.install_dependencies`; явный резерв SDK выбирается через
`--sdk-source vendored`. Последний режим сохраняет проверку доступного индекса
и одобрение расхождения hashes. Бюджет setup 300 секунд, SDK этапа 60 секунд.

`requirements.txt` содержит runtime pins/hashes и официальный источник SDK.
`pip install -r requirements.txt` сохраняется как runtime export, но не выполняет
helper probe/approval и не принимает SDK hash вне lock. `requirements-dev.txt`
содержит hashless runtime pins и dev tools: raw pip не гарантирует artifact
identity/approval и не выдаёт receipt для Make. Документированный проверенный
путь установки и резерва: Make/helper. TLS проверяется в обоих случаях.

Lint строгий: Pylint, оба этапа Flake8 и Mypy; typecheck запускает Mypy отдельно
в области `pyproject.toml`. Coverage запускает pytest один раз и требует >=90%
общего и diff-покрытия. `make test` запускает только offline `test/`.
Эти команды не устанавливают пакеты автоматически и не вызывают брокера.
Live `make e2e` требует отдельного согласования и sandbox-токена; детали в
[README](../README.md#песочница-в-github). Package smoke его не заменяет.

## Источники и резерв

Основной SDK источник: официальный T-Bank; для остальных wheels: PyPI.
Резерв выбирается при DNS/сетевой ошибке, timeout, HTTP 401/403/404/429/5xx
или отсутствии SDK версии в корректном индексе. Переключение даёт WARNING
с именем, версией, безопасной причиной и путём; явный vendored режим сообщает
`explicit_vendored_mode`. Сырые ответы, credentials и pip stderr не публикуются.
TLS certificate, URL/index format, yanked/ambiguity, hash/metadata/tag/ZIP,
solver/install ошибки фатальны. Резерв не выбирается после произвольной ошибки pip.

Общий installer и offline builder поддерживают per-package fallback только
для сохранённых разрешённых Python wheels с точной version/artifact identity.
В текущем [inventory](../vendor/inventory.json) единственный такой бинарник:
[SDK 1.51.0](../vendor/t-tech-investments/README.md). Для остальных matching
GitHub source исключает binary из Git, даже если готового wheel там нет.
При недоступности PyPI и отсутствии reserve сборка/установка завершается
ошибкой; автоматической сборки чужих source projects нет. Generated cloud ZIP
может содержать эти PyPI binaries: они не становятся tracked binaries в Git.
yc не хранится и не резервируется; штатная загрузка недоступна => деплой падает.

## Runtime lock и одобрения SDK

[runtime-lock.json](../runtime-lock.json) задаёт полный runtime closure,
versions, target, hashes и source identities. Generated requirements проверяются
на соответствие ему; обычный setup/build lock не обновляет. Dev tools не могут
изменять runtime. Для явной генерации по reviewed inventory:

```bash
make update-runtime-lock
python -m scripts.install_dependencies --write-file-lock
make check-runtime-lock
```

Генератор использует supported pip dry-run report под target CPython 3.12 и
точными constraints inventory. Он не выбирает latest и не добавляет approvals.
Отдельный [runtime-file-lock.json](../runtime-file-lock.json) содержит hashes/size
исходных файлов каждого exact wheel, включая RECORD. Генерация --write-file-lock
скачивает wheels по действующим hashes и SDK source policy, проверяет ZIP/RECORD
и записывает только эту проекцию, без установки или receipt. Оба lock-файла
требуют review; устаревшая файловая проекция блокирует setup/offline проверку.
Подготовленный report можно проверить через
`python -m scripts.update_runtime_lock --report /tmp/pip-report.json`;
результат также записывает lock и generated requirements. Изменение версии
требует согласованного inventory/source/licence review, затем генерации,
review diff, новой verified установки, lint/coverage и package smoke.

При доступном официальном индексе разные reserve/index SHA-256 вызывают
`approval_required` до установки/упаковки, включая forced vendored.
Одобрение точной версии и пары hashes вручную записывается в
[SDK manifest](../vendor/t-tech-investments/manifest.json): `version`,
`reserve_sha256`, `index_sha256`, `approved_by`, `approved_at_utc`, `evidence`.
После этого официальный artifact допускается только явным обновлением lock
с проверкой metadata/closure. Изменённые hash/version требуют нового одобрения.
Общего обхода и автоматической записи approvals нет; corruption остаётся
фатальной. Текущие `approvals=[]`, `official_hash_verified=false`:
независимая сверка официального wheel hash не выполнена.

## Сборка облачного архива

Все режимы сохраняют цель и результат `build/yandex-function.zip`:

```bash
# Обычный: зависимости устанавливаются при подготовке функции
make claude-yandex-archive
# Локальный SDK, остальные пакеты устанавливаются из PyPI
make claude-yandex-archive USE_VENDORED_SDK=1
# Готовый runtime payload, без установки пакетов в облаке
make claude-yandex-archive USE_VENDORED_SDK=1 OFFLINE_ARCHIVE=1
```

Оба флага принимают только 0/1. `OFFLINE_ARCHIVE=1 USE_VENDORED_SDK=0`
также допустим: готовый payload с auto выбором SDK. Default builder остаётся
stdlib-only, без сети и SDK probe. Vendored builder выполняет bounded index-only
probe без SDK download; архив содержит local wheel/LICENSE/manifest и hashed
runtime requirements только PyPI. Исходные requirements не переписываются.

Offline builder требует совместимый Linux/CPython/glibc до сети. В отдельном
wheelhouse получает весь locked runtime graph, затем bounded pip download
проверяет локальный набор и pip install создаёт пустой target. Бюджеты получения
и установки по 300 секунд; source/native compilation нет. Native wheels
ограничены manylinux2014/2_17 и совместимыми universal/abi3 tags.
Чужие site-packages не копируются. Root payload включает .dist-info, data,
лицензии и `dependency-manifest.json` с lock digest, artifact и file hashes, без
корневых installer requirements, wheelhouse и dev-only пакетов. Обязательные
через SDK iprotopy/grpcio-tools/mypy-protobuf/ANTLR/setuptools сохраняются.
Публикуется точная проекция проверенных wheels, без созданных pip entrypoints
и bookkeeping-файлов. Повторная offline проверка сверяет весь состав и bytes
с независимым runtime-file-lock до imports, без сети или installer.

Новые режимы проверяют CRC/состав/точные bytes и заменяют ZIP атомарно;
отказ сохраняет предыдущий результат. Offline builder проверяет isolated imports
до публикации ZIP. Прямой script, runpy и другой cwd поддерживаются:

```bash
python /path/to/autorepeater/scripts/build_yandex_archive.py --project-root /path/to/autorepeater --output /tmp/function.zip
```

Для резервных режимов у script те же `--use-vendored-sdk`/`--offline-archive`.
Лицензионный состав payload и оговорка iprotopy описаны в
[vendor README](../vendor/README.md#лицензии-и-payload).

## Проверка готовых архивов

```bash
make check-cloud-package CLOUD_PACKAGE_SHA="$(git rev-parse HEAD)"
```

Без `CLOUD_PACKAGE_SHA` checker использует текущий HEAD; CI передаёт полный SHA.
Проверка требует CPython 3.12/Linux и сети для получения PyPI wheels, создаёт
собственные временные каталоги для payload. Vendored ZIP устанавливается из
архивных requirements без T-Bank install source; build всё равно делает
index-only probe. Offline ZIP перепроверяется и импортируется без pip/acquisition.
Результат JSON содержит SHA checkout и canonical lock digest. Dirty рабочее
дерево не превращает такой результат в доказательство проверки committed SHA.

Оба режима импортируют main/handler/SDK, native grpc/protoc и protobuf в
изолированном `-I/-S` процессе; namespace origins проверяются по `__path__`.
Handler не вызывается, клиенты и RPC не создаются, токены не нужны.
Python socket guard не доказывает OS-level блокировку native syscalls.
Smoke не подтверждает Yandex cold start, облачные native imports или live E2E.

Два GitHub запуска используют ту же Make-цель:

- Required [Offline checks](../.github/workflows/python-app.yml): после одного
  coverage-прогона отдельный обязательный сетевой шаг, без skip/continue-on-error.
- Actions → [Cloud package check](../.github/workflows/cloud-package-check.yml)
  → Run workflow: ручной `Cloud package smoke`, без cloud/sandbox secrets.
  Workflow появляется в штатном UI после добавления в default branch;
  ручной результат не заменяет required Offline checks или Sandbox E2E.

## Production-деплой

Обычный [deploy-prod.yml](../.github/workflows/deploy-prod.yml) использует
default архив. [deploy-prod-reserve.yml](../.github/workflows/deploy-prod-reserve.yml)
запускается только вручную: Actions → deploy-prod-reserve → Run workflow →
branch master → mode vendored/offline. Автоматического reserve deploy нет.
Vendored выбирает USE_VENDORED_SDK=1, offline добавляет OFFLINE_ARCHIVE=1.

Оба workflow допускают только gorpsys/autorepeater/master, требуют успешные
Lint/Offline checks/Sandbox E2E текущего полного SHA с проверкой producer;
checkout точный, gate повторяется до OIDC. Concurrency `deploy-prod`,
cancel-in-progress=false, queue=max общая. Ресурсы 256MB/60s, python312,
handler.handler, deployment/runtime SA, Lockbox и Object Storage сохраняются.
yc 1.40.0 скачивается стандартно; резервного binary/LFS/Release assets нет.
Недоступная загрузка не даёт успешного деплоя.

Live Sandbox E2E для актуального PR SHA запускается вручную после отдельного
согласования. Merge, deploy и production invoke не являются частью package smoke.
После merge резервный деплой требует отдельного согласования и успешных checks
master. Read-only ACTIVE/ID/параметры подтверждают публикацию версии, но не cold
start её Python-кода; облачная import-only приёмка согласуется отдельно.

## Версии и выпуск

Версии: SemVer X.Y.Z, теги vX.Y.Z; записи выпуска краткие в
[CHANGELOG](../CHANGELOG.md). При выпуске принятые изменения из «Не выпущено»
переносятся в раздел версии; промежуточные коммиты не получают отдельные теги.
