# Разработка и проверки

Действующие инструкции для кода, тестов и сборки.
Подробные команды — в [техническом README](../../scripts/README.md#окружение-и-проверки).
Live-песочница — в [отдельных инструкциях](sandbox.md).
Прежние ошибки проверок — в [истории находок](history.md#разработка-и-приёмка).

## Проверки и запуск

```bash
make setup VENV_PYTHON=python3.12
make check-runtime-lock
pytest test/test_autorepeater.py
pytest test/test_autorepeater.py -k имя_теста
make test
make coverage
make lint
make typecheck
git diff --check
python -m scripts.check_rebalance_policy --output /tmp/rebalance-policy-report.json
make claude-yandex-archive
make claude-yandex-archive USE_VENDORED_SDK=1
make claude-yandex-archive USE_VENDORED_SDK=1 OFFLINE_ARCHIVE=1
```

Verified setup: CPython 3.12/Linux x86_64, glibc >=2.17; PyPI нужен для
установки и сборки offline payload. make setup устанавливает проверенные
runtime wheels через scripts.install_dependencies, затем dev tools под
runtime constraints. Проверки Make требуют актуальный schema2 receipt,
состав и хеши файлов; для внешнего PYTHON сначала запустить helper тем же
интерпретатором. Проверки не устанавливают пакеты автоматически.
CI: Python 3.12, pytest/pylint/flake8; общее и diff-покрытие >=90%.
Обязательный Offline checks до установки зависимостей проверяет выпуск через
scripts.release_version: __version__ совпадает с первым разделом CHANGELOG,
версия строго больше тегов и версии базы PR. На master допускается уже
созданный тег только для того же SHA. Release tag публикует аннотированный
тег после мержа; вручную теги новых выпусков не создавать.
Make использует .venv/bin/python, если он существует, иначе python3;
PYTHON переопределяет выбор для всех целей. make lint строгий: любое замечание
Pylint, Flake8 или Mypy завершает цель с ошибкой, --exit-zero не используется.
make typecheck запускает Mypy отдельно; явная область в pyproject.toml
включает Make/e2e/deploy helpers, установку и сборку зависимостей, runtime lock,
vendor/package smoke и API-wrapper, не всё приложение.
.coveragerc включает рабочие модули, main.py, handler.py, scripts; исключает
тесты, сборочные копии и окружения. PR diff сравнивается с SHA базы, push
master с предыдущим SHA; diff без исполняемых строк проходит без процента.
Пороги/область покрытия/workflows не менять ради процентов. Стремиться к
100% осмысленно, без pragma/exclusions. Проверять отслеживаемые и новые Python.
Для незакоммиченной работы diff-cover по git не включает untracked:
проверить их отдельно либо построить --diff-file без staging.
Для временных окружений выбирайте существующий интерпретатор через PYTHON;
архивные subprocess-тесты используют PATH того же окружения.
Запись об окружении конкретной сессии сохранена в [истории](history.md#разработка-и-приёмка).

```bash
# INVEST_TOKEN предоставляется через окружение для явного рабочего запуска
python main.py --algoritm INDEX -s IMOEX -d <dst> --debug
python main.py --algoritm ACCOUNT -s 2193248994 -d <dst> --debug
python main.py --algoritm COMPOSITE -s BALANCED -d <dst> --debug
```

Оба селектора CLI обязательны, algoritm написан именно так.
Src для всех встроенных алгоритмов является точным name JSON; ACCOUNT
source_account_id находится внутри конфига и передаётся API без преобразований.
Регистр/пробелы не нормализуются. -t/--threshold и -r/--reserve отвергаются.
Debug строит и проверяет планы с чтением данных, без submit_order.
Облако по умолчанию COMPOSITE/BALANCED, run_sync без стрима и без debug.
Query algoritm/src при отсутствии ключа использует ALGORITM/SRC_ACCOUNT;
BALANCED default только для COMPOSITE. Пустой ключ не заменять окружением.
Token fallback INVEST_TOKEN/t_token сохраняется и для пустого token.
Dst при отсутствии query-ключа: DST_ACCOUNT/DEFAULT_DST_ACCOUNT.
Явный dst: непустая строка, не только пробелы; не преобразовывать ID.
Пустой/null/нестроковый dst отвергать до SDK-клиента, без fallback.
Event и queryStringParameters — JSON-объекты; event=None, отсутствие query
и query=None означают отсутствие параметров. Timer messages/details.payload
не являются query-параметрами; таймер сохраняет выбор из окружения/defaults.

## Разработка и приёмка

Для нового алгоритма плоский модуль/тесты и одна регистрация
register_algorithm('NAME', AlgorithmDefinition(prepare_source, create)).
Реестр работает с фабриками. Новый алгоритм подключается своей регистрацией,
сохраняя CLI/Runner/движок.
Новый состав требует только JSON с уникальным name.
Архив автоматически включает плоские модули и нескрытые JSON дерева,
не внешние каталоги; проверять реальные конфиги/пять методов вне репозитория
с блокировкой сети. Диагностические scripts/тесты в архив не включаются.
Default-архив содержит requirements для установки в облаке. Vendored добавляет
локальный SDK; offline включает проверенный runtime payload без корневых
installer requirements, dev-only пакетов и копирования site-packages.
Оба резервных режима проверяют основной индекс; допустимая недоступность
выводится как WARNING, а разные SDK hashes требуют явного одобрения точной
пары в manifest. Политика источников, лицензий и команд — в
[техническом README](../../scripts/README.md#источники-и-резерв).
runtime-lock.json и runtime-file-lock.json обновляются явно с review;
обычные setup/build их не меняют. Required Offline checks после одного
coverage-прогона выполняет обязательный сетевой make check-cloud-package
для точного SHA. Smoke не вызывает handler или RPC, не заменяет live E2E
и не подтверждает cold start в Yandex.

Все деньги/количества Decimal, форматирование без экспоненты, целые с .0,
nano до 9 знаков. Сохранять публичное имя postiton_to_string для совместимости.
INFO причины/ограничения, ERROR ошибки, WARNING неоднозначные UID.
Токены не выводить, SDK-запросы ради логов не добавлять.
Новые зависимости согласовывать до добавления.
Ручные правки apply_patch; сохранять чужие dirty changes, без rollback.

Тесты pytest, TDD RED -> GREEN, чистые бизнес-проверки отдельно от SDK.
Порты autospec(spec_set=True), SDK-сервисы autospec(inspect.unwrap(Service),
instance=True,spec_set=True) с явными DTO; конструкторы/сеть не вызывать.
Проверять точное число/порядок/аргументы и отсутствие лишних вызовов.
Стримы конечные, заканчивать контролируемым TestException, рабочий стрим
не обязан завершаться. AST/import-тесты сохраняют границы SDK.


## Офлайн-модель

Офлайн-сценарии и seeds/hashes в test/data/rebalance_scenarios.json.
Исторические значения holdout и результаты калибровки сохранены в
[отчёте от 2026-10-06](../reports/2026-10-06-calibration.md).
Публичный snapshot 44 котировок не является реальным ACCOUNT; состава
такого счёта нет, на нём отличие не измерено. Не выдавать модель за real account.
Старый .004 допускается только компактным чистым oracle офлайн-утилиты
и историческими свидетельствами, не runtime helper.
Отчёт показывает turnover/order count/estimated commission/half-L1 error,
повторения, monthly2000, random prices/tail/off-grid/table boundaries,
резервные дефициты/вложенность/shared UID и оценочный риск владения.
Модельные FILL/комиссия/marks не гарантируют BESTPRICE/settlement/экономию.
RUB-модель поддерживает сигнатуры ExecutionData/OrderExecutor, включая
необязательный Valuation и чтение прежнего FILL по broker ID без новых эффектов.
Её денежные поля используют известную модельную отдельную комиссию; точная
модельная SELL-выручка не переносится в SDK-контракт с `Receipt.cash={}`.
Сравнение денежных границ модели с SDK и многовалютная регрессия настоящего
Runner проверяются отдельно на строгих SDK-моках, без внешних вызовов.
1000-сетка и крайние 0/null экстраполяции не гарантируют произвольные бюджеты.
