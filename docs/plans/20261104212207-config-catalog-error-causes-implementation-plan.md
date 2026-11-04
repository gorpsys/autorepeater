# Issue #14 / PR B: сохранение исходных причин ошибок каталога конфигов (C32)

Дата плана: 04.11.2026. Обновлён: 05.11.2026.
Статус: **B1–B4: локальная реализация и проверки завершены; CI точного коммита ожидается**.
Проверенная база: master `8cd5bfd061e898cec0e95f83a38b50505ba324a8`, версия 7.0.0.
Выполненный PR A: [#53](https://github.com/gorpsys/autorepeater/pull/53).
Тикет: [#14](https://github.com/gorpsys/autorepeater/issues/14).
Контекст: [context-issue-14-c32.md](/home/kazakovaleksey/PythonProjects/context-issue-14-c32.md).

## Цель и действующие решения

Сохранить оригинальное исключение чтения/валидации кандидата и его цепочку,
чтобы однозначный фатальный выбор позволял установить точную причину отказа.
Внешние типы и тексты ошибок, краткие предупреждения о чужих файлах,
правила выбора и подготовка до credentials/Runner/Client сохраняются.
Пользователь результата — владелец приложения, разбирающий ошибки конфигов.
Новых пользовательских параметров, внешних интеграций и миграции конфигов нет.

Объём — **только C32**, отдельный PR B от master. C29/C30 и закрытые схемы
ACCOUNT/INDEX/COMPOSITE уже реализованы в #53. C31 (унификация filesystem
resolver/enumerator), финансовая математика, SDK, исполнения, таймауты,
архивный builder и workflows в этот PR не входят.

Приоритет спецификации: текущая постановка владельца и точные контракты PR B
из плана, вошедшего в #53. Старые рекомендации общего аудита и локальный
подробный план до уточнения ACCOUNT не расширяют объём. ACCOUNT остаётся строгим.

## Источники и проверенная область

| Источник | Основание для плана |
| --- | --- |
| [Контракты PR B в merged #53:148](https://github.com/gorpsys/autorepeater/blob/8cd5bfd061e898cec0e95f83a38b50505ba324a8/docs/plans/20261102233950-strict-config-json-and-error-causes-implementation-plan.md#L148) и [B1–B3:417](https://github.com/gorpsys/autorepeater/blob/8cd5bfd061e898cec0e95f83a38b50505ba324a8/docs/plans/20261102233950-strict-config-json-and-error-causes-implementation-plan.md#L417) | Оригинальный cause у Candidate, точный и implicit INDEX выбор, совместимость, PATCH и проверки |
| [Issue #14](https://github.com/gorpsys/autorepeater/issues/14) | Исходная находка C32; при свежей проверке OPEN, комментариев 0, последняя правка тела 06.10.2026 |
| [Task 9 локального общего плана](20261006160000-architecture-hardening-implementation-plan.md#task-9-однозначные-json-схемы-и-общий-независимый-каталог-конфигураций) | Общая группа и порядок приёмки; более поздние решения сужают её до C32 для PR B |
| [Candidate/discover_candidates:13](https://github.com/gorpsys/autorepeater/blob/8cd5bfd061e898cec0e95f83a38b50505ba324a8/autorepeater/config_catalog.py#L13) и [select_candidate:46](https://github.com/gorpsys/autorepeater/blob/8cd5bfd061e898cec0e95f83a38b50505ba324a8/autorepeater/config_catalog.py#L46) | Сейчас сохраняется только новый ValueError из текста; причина теряется до последующего raise |
| [select_index_config:253](https://github.com/gorpsys/autorepeater/blob/8cd5bfd061e898cec0e95f83a38b50505ba324a8/autorepeater/index_config.py#L253) | Отдельная диагностическая ветка INDEX без имени также теряет причину |
| [test_config_catalog:24](https://github.com/gorpsys/autorepeater/blob/8cd5bfd061e898cec0e95f83a38b50505ba324a8/test/test_config_catalog.py#L24) | Уже закреплены одно чтение, name, тексты и приоритет дублей; проверки идентичности cause ещё нужны |
| [contracts](../agents/contracts.md#подготовка-и-конфиги), [development](../agents/development.md#проверки-и-запуск), [SemVer](../../scripts/README.md#версии-и-выпуск) | Владелец проверки, порядок подготовки и штатная приёмка |

На 04.11.2026 GitHub подтвердил тот же master и merge #53. Пробы из контекста:
8 наблюдательных тестов прошли на git blobs этого SHA; они подтверждают дефект,
а не готовность исправления. В этом этапе планирования тесты заново не запускались.

На этапе планирования рабочая папка находилась на старой docs-ветке `docs/readme-versioning` (`5bfeeeb…`)
с чужими изменениями и untracked-планами. CodeGraph использован для навигации,
но точные исходники/контракты master проверены через git objects. При реализации
нужна чистая продуктовая ветка от свежего master; текущую грязную папку не переносить
на него принудительно и не включать её посторонние файлы в PR. Создание ветки
и способ изоляции относились к последующему разрешённому этапу реализации.
По текущей команде реализация выполняется в отдельном worktree `autorepeater-c32`,
ветка `release/issue-14-c32`, от указанной базы master. Реализация и публикация
ветки/PR #54 разрешены; live E2E, merge и production этой командой не разрешены.

## Архитектура и владение ответственностью

```text
файл -> общий JSON reader -> отдельный validator алгоритма
     -> config_catalog: Candidate(error, cause), отложенный выбор
     -> prepare_strategy -> сохранённый PreparedStrategy -> Runner/Client
```

- JSON reader отвечает за чтение/синтаксис/повторные ключи; validators — за свои
  схемы и decimal-ошибки. Их продуктовый код для C32 не менять.
- `config_catalog` владеет сохранением исключения кандидата и связыванием причины
  при точном выборе. Добавление поля в его внутреннюю модель обосновано тем,
  что диагностическая строка не сохраняет тип, идентичность и цепочку исключения.
- `index_config.select_index_config(None)` владеет только своей legacy-политикой
  выбора без имени и типом UnsupportedSourceError. Он использует сохранённую
  причину единственного кандидата, не повторяет чтение и не определяет новую схему.
- Причина не попадает в DTO конфигов/стратегий, финансовые структуры, порты или
  PreparedStrategy. Её не сериализовать и не выводить дополнительными логами.
- Reporting получает прежнюю строку warning. Переносить chaining в entrypoints,
  Runner, reporting или SDK-адаптеры не требуется.
- Существующий образец явного chaining — [ACCOUNT _fraction:30](https://github.com/gorpsys/autorepeater/blob/8cd5bfd061e898cec0e95f83a38b50505ba324a8/autorepeater/account_config.py#L30);
  сходное поведение уже вошло через [PR #4](https://github.com/gorpsys/autorepeater/pull/4).

## Конкретное решение

### Candidate и discovery

Последним полем существующей dataclass добавить:

```python
cause: OSError | ValueError | None = None
```

Существующие path/name/config/error, их порядок и mutability не менять.
Четырёхпозиционные конструкторы сохраняются. Причина — **тот же объект**,
пойманный reader/validator, включая его прежние `__cause__`/`__context__`.
Создавать новый exception того же типа или восстанавливать его из строки нельзя.

В каждой итерации `discover_candidates` отдельно инициализировать
`original_cause: OSError | ValueError | None = None`. В прежнем
`except (OSError, ValueError) as cause` присвоить `original_cause = cause`,
создать прежний `error` с тем же label/текстом и передать исходную причину
в Candidate. Не использовать очищаемую Python переменную except после блока.
Успешный кандидат получает error=None/cause=None, в том числе после ошибочного
предыдущего кандидата. Discovery продолжает обход и не поднимает сохранённый error.

Набор catches не менять. TypeError/RuntimeError, KeyboardInterrupt/SystemExit
и другие ранее неперехватываемые исключения продолжают распространяться.
Имя извлекается только из разобранного dict перед проверкой тела; parse error
не даёт права восстановить его из filename/сырого JSON.

### Точный выбор и INDEX без имени

В `select_candidate` сохранить всю последовательность warnings/поиска/дублей.
Только для единственного совпавшего кандидата с error:

- при cause не None поднимать **тот же** `candidate.error` через
  `raise candidate.error from candidate.cause`;
- при cause=None выполнять прежний `raise candidate.error`, сохраняя legacy
  поведение и не добавляя выдуманное исключение или новую обёртку.

Для INDEX без имени разделить нынешний объединённый guard:

1. При `len(candidates) != 1` поднимать прежний UnsupportedSourceError.
2. При одном кандидате с error поднимать UnsupportedSourceError с прежним
   сообщением **непосредственно from candidate.cause**. Диагностический
   `candidate.error` не подставлять вместо отсутствующей причины.
   Для legacy cause=None это `raise ... from None` без выдуманной цепочки.
3. При одном корректном кандидате возвращать прежний config.

Точное сообщение implicit INDEX во всех отказах:
`--src must name a configured index strategy`. Предупреждений в эту ветку не добавлять.
Явное имя INDEX по-прежнему делегируется общему select_candidate.

| Сценарий | Внешний результат | Связывание причины |
| --- | --- | --- |
| Одно точное name, ошибка схемы | Прежний ValueError с path/name | Тот же оригинальный exception; его вложенная цепочка сохраняется |
| Корректный выбранный + чужой плохой | Корректный config и прежние warnings | Cause хранится, но не поднимается |
| I/O / JSONDecodeError / duplicate JSON key | Candidate name=None, прежняя диагностика | Оригинальная причина сохранена у Candidate |
| Нет точного name, даже при одном неразобранном файле | Прежний UnsupportedSourceError с problematic files | Не приписывать причину неизвестному источнику |
| Несколько совпавших name, включая плохое тело | Прежний duplicate-name ValueError | Не выбирать причину одного из файлов |
| Implicit INDEX, один плохой кандидат | Прежний UnsupportedSourceError/сообщение | Непосредственно оригинальная причина кандидата, даже при name=None |
| Implicit INDEX, ноль/несколько кандидатов | Прежний UnsupportedSourceError/сообщение | Без произвольного cause |
| Ручной legacy Candidate без cause | Прежний отказ и совместимость конструктора | Причина не выдумывается |

Для отсутствующего/неоднозначного выбора не создавать ExceptionGroup или
эвристику «единственный плохой файл». Не менять прямые loaders и строгий load-all.

## Подход к разработке и тестированию

TDD RED -> GREEN: сначала воспроизвести конкретное новое требование, затем
минимально исправить принадлежащий ему слой. Каждый шаг с изменением кода
завершить GREEN затронутых тестов до следующего шага; не оставлять будущие RED
в готовом промежуточном результате. Старые наблюдательные тесты из `/tmp`
использовать как свидетельства, не переносить их ожидание потерянного cause
в качестве нового контракта.

Новые/изменяемые моки портов и границ эффектов — `autospec=True, spec_set=True`
или `create_autospec(..., spec_set=True)`. Для синтаксиса/decimal использовать
реальные синтетические временные файлы и validators; не создавать SDK-клиенты.
Для проверки идентичности пойманного exception допустим autospec reader/validator
с известным side_effect. Общее переустройство старых моков вне затронутых тестов
не является задачей C32.

Проверять `is`, внешние типы/сообщения и вложенную цепочку, а не только имя типа
или строку. В тестах успешного выбора контролировать прежнее число чтений,
валидаций, warnings и их порядок. Не дублировать все финансовые сценарии;
действующая полная свита подтверждает их неизменность.

## Шаги реализации

### B1. Хранение оригинальной причины и фатальный точный выбор

**Файлы:**
- Изменить: `autorepeater/config_catalog.py`.
- Изменить тесты: `test/test_config_catalog.py`.

- [x] Сверить свежий master/базу и повторить репро C32 на продуктовой ветке.
  Написать RED на Candidate.cause и потерю цепочки при единственном точном выборе.
  Использовать известный экземпляр исходной ошибки с вложенным cause, чтобы
  проверить идентичность `candidate.cause` и `caught.value.__cause__`.
- [x] Добавить последнее optional-поле cause и сохранить оригинальный exception
  в discovery по конкретному решению выше. В named selection связать прежний
  error с cause; при cause=None сохранить прежний raise.
- [x] Добавить/обновить отдельные тесты ошибок чтения PermissionError/OSError,
  настоящего JSONDecodeError и duplicate-key parser; name неизвестно, причина
  сохранена, validator после неуспешного чтения не вызывается. Существующий
  `(OSError, ValueError)` также уже включает UnicodeDecodeError: проверить
  невалидный UTF-8 временного файла без расширения catches.
- [x] Добавить контроли: успешный кандидат и переход bad -> good не наследуют
  cause; чужие ошибки только предупреждаются; неизвестный источник и выбранные
  дубли не получают случайную причину. Проверить duplicate-before-body,
  порядок предупреждений и одно чтение/валидацию на разобранный кандидат.
- [x] Добавить тесты legacy четырёхпозиционного конструктора/default None,
  идентичности внешнего error; неперехвата TypeError/RuntimeError и сигналов
  остановки, без broad catch и без немедленного отказа на чужом файле.
- [x] Получить GREEN: `PYTEST_ADDOPTS='-k "test_config_catalog"' make test`.
  Зафиксировать RED/GREEN и фактическое покрытое поведение в этом плане.

**Evidence B1 (04.11.2026):** worktree `autorepeater-c32`, ветка
`release/issue-14-c32`; `origin/master` —
`8cd5bfd061e898cec0e95f83a38b50505ba324a8`. Штатный
`make setup VENV_PYTHON=python3.12` выполнен. Для
`PYTEST_ADDOPTS='-k "test_config_catalog"' make test`:
RED — **9 failed, 17 passed, 5873 deselected** (4.91 s,
`build/c32-b1-red.log`); GREEN — **26 passed, 5873 deselected** (1.98 s,
`build/c32-b1-green.log`). Проверены идентичность исходной причины и вложенной
цепочки, прежний внешний error, I/O/JSON/duplicate-key/UTF-8, отсутствие утечки
cause в успешный кандидат, legacy default None, порядок warnings и приоритет
дублей, отсутствие произвольной причины у неизвестного/неоднозначного выбора,
одно чтение/валидация и распространение TypeError/RuntimeError/сигналов остановки.
Runtime-изменения B1 ограничены общим каталогом; на момент этой проверки B2
ещё не выполнялся.

### B2. Однозначная причина legacy-выбора INDEX без имени

**Файлы:**
- Изменить: `autorepeater/index_config.py` — только `select_index_config`.
- Изменить тесты: `test/test_config_catalog.py`, `test/test_strategy_selection.py`
  (существующая проверка калибровочного auto-selection).

- [x] Написать RED для одного ошибочного INDEX без имени: known schema error
  и непарсибельный документ дают прежний UnsupportedSourceError, но должны
  сохранять идентичность оригинальной причины и её вложенную цепочку.
- [x] Разделить guard на ноль/несколько кандидатов и один erroneous candidate;
  связать последний непосредственно с Candidate.cause. Возврат корректного config,
  ветку явного имени и текст `--src must name a configured index strategy` сохранить.
- [x] Добавить отдельные тесты одного кандидата с I/O/JSON/duplicate-key ошибкой
  и name=None; одного валидного; legacy cause=None. Проверить отсутствие warnings
  и отсутствие повторного чтения в implicit selection.
- [x] Дополнить случаи нуля/нескольких кандидатов, включая один корректный среди
  плохих и дубли: по-прежнему отказ без произвольной причины; good не выбирается
  автоматически из неоднозначного каталога. Строгий `load_index_configs` прежний.
- [x] Получить GREEN:
  `PYTEST_ADDOPTS='-k "test_config_catalog or test_strategy_selection"' make test`.
  Записать результат; не менять `_index_paths` или финансовые/валютные validators.

**Evidence B2 (04.11.2026):** команда
`make test PYTEST_ADDOPTS='-k "test_config_catalog or test_strategy_selection"'`:
RED — **5 failed, 187 passed, 5717 deselected** (2.61 s); все падения проверяли
потерянный `__cause__` для schema/nested, OSError, PermissionError, настоящего
JSONDecodeError и duplicate-key ValueError. После минимального изменения только
`select_index_config` GREEN — **192 passed, 5717 deselected** (2.50 s), exit 0.
Единственный ошибочный кандидат непосредственно связывает прежний
UnsupportedSourceError с оригинальной причиной; тип, точный текст и вложенная
цепочка сохранены. Проверены legacy cause=None, отсутствие warnings, однократные
чтение/валидация, корректный single, ноль/несколько кандидатов, good среди bad,
дубли и строгий load-all. Явный выбор, `_index_paths`, validators и зависимости
не менялись; брокерские вызовы не выполнялись. Вывод RED/GREEN сохранён в истории
инструментальных вызовов, дополнительные log-файлы не создавались. На момент
этой проверки B3 ещё не был начат.

### B3. Сквозная цепочка в трёх алгоритмах и подготовка до эффектов

**Файлы:**
- Изменить тесты: `test/test_account_config.py`, `test/test_composite_config.py`,
  `test/test_policy_config_schema.py`, `test/test_strategy_selection.py`.
- Изменить тесты: `test/test_strategy_preparation.py` — один сценарий ошибочного
  реального дочернего конфига в COMPOSITE и отсутствия конструирования дерева.
- Продуктовый код этих слоёв не менять: они используют исправленный каталог.

- [x] Дополнить тесты реальных loaders/selectors: ACCOUNT/INDEX с reserve=bad,
  COMPOSITE с component_drift_limit=bad. Прямой loader по-прежнему даёт
  ValueError -> InvalidOperation; exact selector — wrapper(path/name) -> тот же
  пойманный ValueError -> тот же InvalidOperation. Reader/validator наблюдать
  autospec spy с side_effect настоящей функции; не подменять ошибку строкой.
- [x] Добавить отдельные контроли одного чтения, валидного выбранного и чужого
  ошибочного/непарсибельного документа. Из raw JSON/filename не восстанавливается
  name, readable name плохого тела по-прежнему участвует в дублях.
- [x] В CLI и cloud для всех трёх алгоритмов проверить выбранную схемную ошибку
  с полной цепочкой до чтения credentials/get_param и до Runner/Client. Новые
  warnings отсутствуют. Вложенный плохой конфиг COMPOSITE останавливает подготовку
  всего дерева до фабрик, сохраняя исходную причину.
- [x] Сохранить существующие регрессии строгого JSON/unknown fields, exact names,
  независимости DIR/PATH, cycles-before-API, калибровки и корректных target.
  Не добавлять разрешённые extras или новые правила выбора ради GREEN.
- [x] Получить GREEN:
  `PYTEST_ADDOPTS='-k "test_config_catalog or test_account_config or test_composite_config or test_policy_config_schema or test_strategy_selection or test_strategy_preparation"' make test`.
  Зафиксировать результаты и отсутствие I/O к брокеру.

**Evidence B3 (05.11.2026):** команда
`make test PYTEST_ADDOPTS='-k "test_config_catalog or test_account_config or test_composite_config or test_policy_config_schema or test_strategy_selection or test_strategy_preparation"'`:
GREEN — **620 passed, 5317 deselected** (4.65 s), exit 0. Добавлены 28
интеграционных проверок поверх уже исправленного B1–B2; искусственный RED
и дополнительные runtime-изменения не вводились. Все три реальных direct loader
сохраняют исходный ValueError -> InvalidOperation; exact selectors и CLI/cloud
сохраняют wrapper(path/name) -> тот же пойманный ValueError -> тот же
InvalidOperation, с проверками идентичности объектов. Autospec/spec_set spies
наблюдают настоящие reader/validator и однократное чтение/валидацию.
Для каждого алгоритма проверены корректный выбор среди чужих decimal/parse
ошибок, число/порядок warnings, неизвестное name при syntax/duplicate-key parse
error вопреки filename/raw JSON и приоритет дубля с readable name плохого тела
без произвольного cause. CLI/cloud останавливаются до credentials/get_param,
Runner/Client без новых warnings. Реальное дерево COMPOSITE/ROOT -> ACCOUNT/GOOD,
COMPOSITE/BRANCH -> INDEX/BAD готовит валидного предшествующего sibling, затем
прерывается с исходной цепочкой до всех трёх фабрик и Client. Существующие
регрессии выбранных модулей зелёные; I/O к брокеру, live E2E, новые зависимости,
полные coverage/lint и B4 на момент этой проверки не выполнялись. Вывод сохранён в инструментальной
истории без новых log-файлов.

### B4. Документирование диагностического контракта и локальная приёмка

**Файлы:**
- Изменить: `docs/agents/contracts.md`, `CHANGELOG.md`, `autorepeater/__init__.py`,
  этот план.
- Изменить: `docs/plans/20261102233950-strict-config-json-and-error-causes-implementation-plan.md`
  **в продуктовой ветке** — только пометка статуса/ссылка для B1–B3; не переносить
  поверх master старую локальную untracked-версию и не переписывать историю PR A.
- Существующие тесты: `test/test_config_catalog.py`,
  `test/test_strategy_selection.py`, `test/test_yandex_archive.py` и тесты версии;
  новых тестов наличия строк документации не добавлять.
- ➕ По результатам первого полного lint: изменить только затронутые B1–B3 тесты
  и `test/conftest.py`, вынести общий capture/assert helper в тестовую фикстуру,
  убрать дублирование и замечания Pylint без suppressions и product helpers.
- ➕ Первый полный coverage выявил нарушение SDK-blocked импорта новым тестом:
  тест должен вызывать `prepare_strategy` напрямую, без импорта Runner.
  Четыре Make-теста получили внешнее
  command-line значение DIFF_BASE через MAKEFLAGS; их собственные env-сценарии
  корректны. Финальную базу передавать `env DIFF_BASE=<SHA> make coverage`, чтобы
  она не переопределяла независимые дочерние make. Make-тесты/helper не менять.

- [x] В contracts описать сохранение cause для точного ошибочного выбора
  и одного implicit INDEX; неизвестный/неоднозначный источник — без случайного cause.
  Прямые loaders и краткие foreign warnings сохраняют контракт. Инструкция миграции
  7.0.0 в `scripts/README.md` остаётся применимой и не требует новой редакции для C32.
- [x] Поднять PATCH от свежей базы, синхронно версию и первый раздел CHANGELOG.
  Для проверенной базы 7.0.0 цель — **7.0.1**; при продвижении master использовать
  следующий PATCH его фактической версии и учесть существующие release-теги.
  Дату указать фактическую; новую несовместимую миграцию не вводить.
- [x] Выполнить `make check-runtime-lock`, затем полный `make coverage`
  без фильтрующего PYTEST_ADDOPTS, `make lint`, `git diff --check`.
  Diff coverage считать от фактического SHA базы PR через штатный DIFF_BASE.
  Сохранить overall/diff >=90%, не менять exclusions/пороги/workflows ради процентов.
- [x] Убедиться, что существующие `test_nested_application_archive` и остальные
  archive smoke прошли в полном наборе: изолированные импорты нового каталога,
  прежние шесть встроенных конфигов и подготовка без сети. Финальный коммит
  дополнительно проверяется обычным CI package smoke после разрешённой публикации.
- [x] Проверить итоговый diff: runtime-изменения только в config_catalog и
  legacy-диагностике INDEX; нет C31, повторной реализации C29/C30, SDK/финансовых
  изменений, конфигов с изменёнными числами, новых зависимостей или чужих файлов.
  Записать команды, результаты и текущий SHA базы; SHA реализации добавляется,
  когда существует разрешённый коммит.
- [x] Отметить фактически завершённые B1–B3 в исходном объединённом плане и
  критерии этого плана после GREEN. Не объявлять закрытым всё #14 и не переносить
  план в completed до фактического завершения согласованного объёма.

**Evidence B4 (05.11.2026):** контракт cause описан, версия и CHANGELOG
согласованы на **7.0.1**, дата выпуска — **2026-11-05**. `make check-runtime-lock`
завершился успешно. Первый полный coverage дал **5 failed, 5932 passed**
(937.78 s), overall **99.46%**: одна регрессия нового top-level импорта Runner
нарушила SDK-blocked проверку; ещё четыре отказа Make-тестов вызвало внешнее
command-line значение DIFF_BASE через MAKEFLAGS/MAKEOVERRIDES. Новый nested-tree
тест исправлен на прямой `prepare_strategy('COMPOSITE', 'ROOT')` без импорта
Runner, сохранив identity cause, все factory spies и отсутствие вызова Client.
База финальной проверки передана через окружение; `test/test_make_commands.py`
и его helpers не менялись и в diff не входят. Общий capture/assert helper
затронутых B1–B3 тестов вынесен в `test/conftest.py` без suppressions.

Финальная адресная команда
`env DIFF_BASE=8cd5bfd061e898cec0e95f83a38b50505ba324a8 make test PYTEST_ADDOPTS='-k "test_strategy_contract or test_make_commands or test_real_bad_composite_descendant"'`
дала **114 passed, 5823 deselected** (10.48 s), exit 0. Последующий полный
`env DIFF_BASE=8cd5bfd061e898cec0e95f83a38b50505ba324a8 make coverage`
без фильтрующего PYTEST_ADDOPTS завершился с **exit 0**, **5937 passed**:
**9954 statements,
54 missing, overall 99.46%**, все три изменённых покрываемых runtime-файла —
**100%**, diff — **10 lines, 0 missing, 100%**. Полный набор включает
`test_nested_application_archive` и остальные archive smoke. Финальный
`make lint` — exit 0: Pylint **10/10**, Flake8 **0**, Mypy **29 файлов**.
`git diff --check` и отдельная whitespace-проверка нового плана прошли.
Счётчик полного набора дополнительно подтверждён
`make test PYTEST_ADDOPTS='--collect-only -o addopts= -v'`:
**5937 tests collected** (1.44 s), exit 0.
Пороги, exclusions, зависимости, lock-файлы и workflows не менялись.

Пять независимых локальных ревью (quality, implementation, testing,
simplification, smells) завершились с **No findings** за одну итерацию.

Итоговый diff проверен относительно master
`8cd5bfd061e898cec0e95f83a38b50505ba324a8`: изменение runtime-поведения
ограничено `config_catalog.py` и legacy-диагностикой `index_config.py`;
`__init__.py` меняет только версию. C31, финансовые/SDK-контракты и числовые
значения конфигов не затронуты. SHA реализации будет записан после разрешённого
коммита; bootstrap SHA не является подтверждением реализации. Локальная
приёмка завершена, CI точного коммита и package smoke после публикации ожидаются.
Live E2E, merge и production не выполнялись; план остаётся в `docs/plans/`.

## Критерии готовности

- [x] Candidate сохраняет тот же оригинальный exception, его вложенную цепочку
  и default None; причины не перетекают между кандидатами.
- [x] Named selected invalid сохраняет прежний внешний error и связывает причину;
  implicit INDEX с одним плохим кандидатом сохраняет прежний UnsupportedSourceError
  и связывает оригинальную причину напрямую.
- [x] Foreign errors не становятся фатальными; предупреждения, их порядок,
  тексты ошибок, обработка отсутствующего/повторного name и legacy совместимы.
- [x] Подготовка остаётся до credentials/Runner/Client без повторных чтений,
  выдумывания name или дополнительных API-вызовов.
- [x] Адресные и полные offline-проверки зелёные, прежние пороги покрытия сохранены;
  версия/CHANGELOG согласованы, текущий контракт описан.

## Учёт прогресса и рисков

Отмечать пункты сразу после фактического GREEN; новые необходимые пункты помечать
«➕», блокеры — «⚠️», с наблюдаемым основанием. При изменении объёма обновлять план
до расширения diff. Повторно согласовывать уже определённые контракты не требуется.

Главные риски и проверки: ранний отказ на чужом файле; привязка случайной причины
к дублю/неизвестному источнику; смена внешнего типа INDEX; потеря идентичности
вложенной цепочки; повторное чтение; перенос старых ACCOUNT extras из локального
плана. Каждый покрыт адресными сценариями B1–B3. Тесты выполняются на ветке
от master, а не на старом docs-checkout. Хранение исключения в Candidate не требует
его публикации в предупреждениях или расширения logging-контракта.

## После локальной реализации

Исходный этап 04.11.2026 разрешал только создание плана. Текущая команда разрешает
реализацию, коммит/push и публикацию PR #54. Полный Sandbox live E2E, merge и
production требуют отдельных команд владельца и в рамках локальной приёмки не
выполняются.

Самостоятельный PR B подготовлен от проверенной базы master. После разрешённой
публикации проверить штатные Lint/Offline checks,
включая release-version и package smoke, на точном head. Полный Sandbox E2E,
merge и дальнейший выпуск — по действующей защите репозитория и выданным на тот
этап разрешениям. Не подменять offline-тестами live-приёмку и не запускать
диагностические реальные сделки для дефекта конфигурации.

При выпуске использовать существующий CI/архив; результат приёмки PR A и
разрешение перехода сверить по сохранённым материалам, без самовольного обращения
к production. Результаты merge/deploy записываются в историю версий после
санкционированного этапа. В описании будущего PR использовать Refs #14,
без автоматического закрытия: C31 остаётся отдельным решением владельца.

## Вопросы

**Блокирующих вопросов для реализации C32 нет.** Цель, потребитель, данные,
поток, слои, способ хранения, все варианты выбора, совместимость и приёмка
определены постановкой и контрактами B из #53. Номер PATCH сверяется по свежей
базе механически и не является отложенным продуктовым решением.

Судьба C31 и момент окончательного закрытия #14 находятся вне объёма C32.
Если существует дополнительный Chatzone-тред с изменяющими требования решениями,
нужна его ссылка: в изученных #14/#53 и контексте такого URL нет. План не
предполагает несуществующих требований из отсутствующей переписки.
