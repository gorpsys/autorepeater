# AGENTS.md

Instructions for coding agents working in this repository.

## Что это

Автоповторитель сделок для Т-Инвестиций (t-tech-investments). ACCOUNT/INDEX/
COMPOSITE строят StrategyPlan по свежим данным; общий исполнитель соблюдает
права продаж, деньги областей, cash_floor, лотность и own немаржинальные лимиты.
Общий threshold удалён. Бизнес-правила и поля подробно описаны в [README.md](README.md).
Исторические calibration docs/JSON/assets не являются актуальным runtime-контрактом.

## Проверки и запуск

```bash
pip install -r requirements.txt
pytest test/test_autorepeater.py
pytest test/test_autorepeater.py -k имя_теста
pytest --cov --cov-report=term-missing --cov-report=xml -q
coverage report --fail-under=90
diff-cover coverage.xml --compare-branch=origin/master --fail-under=90
pylint $(git ls-files --cached --others --exclude-standard '*.py')
flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics
git diff --check
python -m scripts.check_rebalance_policy --output /tmp/rebalance-policy-report.json
make claude-yandex-archive
```

CI: Python 3.12, pytest/pylint/flake8; общее и diff-покрытие >=90%.
.coveragerc включает рабочие модули, main.py, handler.py, scripts; исключает
тесты, сборочные копии и окружения. PR diff сравнивается с SHA базы, push
master с предыдущим SHA; diff без исполняемых строк проходит без процента.
Пороги/область покрытия/workflows не менять ради процентов. Стремиться к
100% осмысленно, без pragma/exclusions. Проверять отслеживаемые и новые Python.
Для незакоммиченной работы diff-cover по git не включает untracked:
проверить их отдельно либо построить --diff-file без staging.
В текущем окружении interpreter /tmp/autorepeater-install-check/bin/python;
полная свита требует PATH=/tmp/autorepeater-install-check/bin:/usr/local/bin:/usr/bin:/bin
для архивных subprocess-тестов.

```bash
# INVEST_TOKEN предоставляется через окружение для явного рабочего запуска
python main.py --algoritm INDEX -s IMOEX -d <dst> --debug
python main.py --algoritm ACCOUNT -s 2193248994 -d <dst> --debug
python main.py --algoritm COMPOSITE -s BALANCED -d <dst> --debug
```

Оба селектора CLI обязательны, algoritm написан именно так.
Src для всех встроенных алгоритмов является точным name JSON; ACCOUNT
source_account_id находится внутри конфига и сохраняет ведущие ASCII-нули.
Регистр/пробелы не нормализуются. -t/--threshold и -r/--reserve отвергаются.
Debug строит и проверяет планы с чтением данных, без submit_order.
Облако по умолчанию COMPOSITE/BALANCED, run_sync без стрима и без debug.
Query algoritm/src при отсутствии ключа использует ALGORITM/SRC_ACCOUNT;
BALANCED default только для COMPOSITE. Пустой ключ не заменять окружением.
Token fallback INVEST_TOKEN/t_token, dst DST_ACCOUNT/DEFAULT_DST_ACCOUNT.

## Подготовка и конфиги

prepare_strategy(algoritm, src) вызывается один раз до чтения токена.
PreparedStrategy сохраняет фабрику/непрозрачные данные/source_display.
create_strategy использует сохранённую фабрику и callable-проверку до Client,
без повторного поиска регистрации или чтения конфигов.
Конструкторы/импорт не выполняют I/O и не сохраняют data/SDK-client.
Активный стек (algoritm, src) обнаруживает цикл с полным путём до фабрик;
соседние повторения разрешены и готовятся заново без глобального кеша.

Каталоги: ACCOUNT_CONFIG_DIR либо ACCOUNT_CONFIG_PATH (configs/account/);
INDEX_CONFIG_DIR либо IMOEX_CONFIG_PATH (configs/); COMPOSITE_CONFIG_DIR
(configs/composite/). Пары DIR/PATH несовместимы. Внешний relative path от cwd,
встроенный от модуля. Только непосредственные нескрытые JSON.
Неверный/пустой путь без fallback. Каждый кандидат читается/валидируется
один раз; чужие ошибки/дубликаты предупреждаются, выбранное неверное/
отсутствующее/повторное имя фатально. Читаемое name плохого тела участвует
в дубликатах, из битого JSON имя не восстанавливать. load_index_configs строгий.
Пути разных алгоритмов независимы; настройки фиксируются при подготовке.

ACCOUNT обязательные name/source_account_id/reserve/allocation_drift_limit;
дополнительные поля загрузчик не использует.
Встроенные reserve="0.01", limit="0.0092"; source_account_id ASCII-цифры,
не произвольный src. Reserve/limit конечные строки [0,1).
COMPOSITE строгие name/component_drift_limit/components; limit [0,1),
непустые компоненты только algoritm/src/weight; weight (0,1], сумма по
порядку при Decimal prec28 <=1, без нормализации. Собственного reserve нет.
INDEX обязательные name/reserve/max_lot_weight_error/instruments/
allocation_drift_limits; min_position_value default "0".
Числа только конечными строками, без NaN/Infinity/bool/JSON-чисел.
Таблица непуста, первый from=0, последняя to=null, остальные конечны,
непрерывные [from,to), upper_inclusive=false, limit [0,1).
IMOEX 95 limits уже содержат запас 1.1, не умножать повторно.
Одноинструментные INDEX используют [0,null) limit=0.

INDEX reference_price/reference_index_capitalization положительны;
effective_quantity >=0, free_float/weight_limit (0,1], reference_weight [0,100]
в процентах. Уникальные непустые ticker. Рабочая капитализация =
reference_index_capitalization * current_price / reference_price;
справочные поля не применять повторно. Минимум полного состава применяется
один раз до перенормализации; непрерывный префикс, лоты к ближайшему целому
с половинами к чётному, бюджетные отмены по минимальной цене ошибки.
Ноль лотов всегда отказ; max_lot_weight_error только при ideal_lots<1,
не при >=1. Первый отказ отсекает весь хвост, без возврата в этом расчёте.
Одноинструментный состав игнорирует минимум и ошибку, но требует исполнимый лот.
Чистый calculate_index_target получает бюджет после резерва, не cash_floor.
Селектор точного ticker share/etf читает полные метаданные всех кандидатов,
выбирает первый api_trade_available bool=True без белого списка режимов;
несколько WARNING, ни одного ошибка; цена/лотность/метаданные проверяются.

## Контракт и слои

Пять методов Strategy без обязательного наследования:

```python
load_snapshot(data) -> object
allocation_profile(snapshot) -> AllocationProfile
build_plan(snapshot, context: StrategyContext) -> StrategyPlan
event_accounts(dst_account_id) -> tuple[str, ...]
should_rebalance(event, dst_account_id) -> bool
```

Callable-проверка не вызывает методы. Снимок непрозрачен родителю/движку,
локален проходу. Profile/build_plan чистые, без SDK/I/O/торговли.
Leaf build_target внутренний помощник main/control, не общий API.
Profile: exposures/prices/reserve_fraction, состав до хвоста и лотов.
Context: path, новый budget>0, previous_budget>=0, отдельный bool
budget_reduction_requires_rebalance, выделенные positions и единые marks.
Plan: path/budget/target/cash_floor/decision/children/unassigned/positions.
Decision: BUY_ONLY/REBALANCE, явный redistribution_allowed, reason/metric/limit.
Cash_floor обязательный, конечный 0..budget; TargetPortfolio его не содержит.
Проверять все main/планы/бюджеты до любой заявки, включая debug/unassigned.

TargetPortfolio quantities/prices в Decimal-штуках и ценах штуки,
empty_reason только у пустых quantities. Общий validate_target допускает
конечные отрицательные/дробные/нулевые количества и неположительные цены;
не сужать его ради нового режима. Финансовый план отдельно запрещает short,
требует положительные цены ненулевых позиций и положительный UID в main.
Пустая/полностью нулевая main ERROR/ValueError, не INFO/ранний возврат.
Пустая control отключает только внутренний сигнал; достигнутая непустая
main с пустой дельтой нормальный SKIP.

| Слой | Файлы |
| --- | --- |
| Схемы/выбор/подготовка | *_config.py, config_catalog.py, strategies.py, strategy_contract.py |
| Чистые стратегии/математика | *_strategy.py, strategy_budget.py, strategy_plan.py, strategy_allocation.py, rebalance_policy.py |
| Порт чтения стратегий | strategy_data.py, tinvest_strategy_data.py |
| Read-only доступность исполнения | execution_data.py, tinvest_execution_data.py |
| Владение/деньги/лоты | order_plan.py, purchase_plan.py |
| Проход и отправка | execution.py (нейтральный), order_execution.py (SDK) |
| Оркестрация/Client | repeater.py (нейтральный), runner.py, grpc_deadline.py |
| Вывод/общие DTO | reporting.py, logging_config.py, money.py, portfolio.py |
| Входы/утилиты | main.py, serverless.py, handler.py, scripts/check_rebalance_policy.py, scripts/check_imoex_strategy.py |

Стратегии не импортируют SDK/grpc, адаптеры, реестр, исполнение, Runner
или друг друга. Общие DTO/политика/движок SDK не импортируют.
COMPOSITE создаёт непрозрачных детей через strategy_contract.create_strategy.
Reporting использует загруженные DTO/UID без дополнительных запросов
ради расчётных логов. Явный просмотр счетов допустим в Runner.run().
AutoRepeater(strategy, data, execution_data, executor), sync_accounts/mainflow
получают только dst. RunnerParams содержит только debug.
StrategyData read-only: get_portfolio/find_instruments/get_instrument/
get_last_prices/position_events; свои DTO/enum, units+nano без округления,
порядок/дубликаты сохраняются, никаких raw_response.
ExecutionData: get_destination(account_id), get_trade_rules(account_id,uids);
ExecutionSnapshot/TradeRules собственные деньги, позиции, caps и flags.
SDK-преобразования подтверждены в [docs/execution-adapter.md](docs/execution-adapter.md).

## Финансовые правила

Бюджет корня включает бумаги/деньги назначения, per-position nano до суммы.
ACCOUNT валюты источника исключает и применяет reserve до ratio.
Профиль листа содержит (1-reserve)*веса, COMPOSITE складывает weight*профили.
C листа=V/(1-reserve), вложенного узла=V/sum(exposures).
Общие UID делятся по weight*exposure в Decimal-штуках, остаток наибольшему
коэффициенту, затем первому; один кандидат получает всё, нулевые кандидаты
не владеют бумагой. Неприписанное не исчезает и не удваивается.
Marks имеющихся UID от dst, отсутствующих максимум из профилей дерева.

COMPOSITE M=B*sum(weights): сначала initial по weights, затем выше порога
по weights, ниже/на пороге при дефиците пропорционально C, иначе сохранять C
и направлять новые деньги в положительные денежные недовесы.
Метрика частей max относительного отклонения доли, строго >limit.
Не считать скорректированные текущие доли новой целью.
Резервное исключение: R=sum(C_i*rho_i), A=sum(C)-R, при A<=M<S
ниже/на пороге уменьшение бюджета не разрешает продажи.
При M<A, выше порога или явном требовании предка уменьшенный ребёнок
получает force=True; это не даёт безусловного SELL неуменьшенным соседям.
Нераспределённая доля вложенности не является резервом.

Лист control от вложенной стоимости, main от полного бюджета, по одному снимку.
Drift половина L1 нормированных денежных весов бумаг; cash вне метрики.
Force имеет приоритет, иначе строго >собственного limit; ниже BUY_ONLY.
IMOEX выбирает limit по main-budget до резерва. Пополнение может перейти
в меньший допуск и вызвать продажи: принятый риск, без безусловных обещаний.
Cash_floor листа=budget*reserve, композиции=sum(child floor)+нераспределённое.
Лотный остаток не floor. Корень force=False; числовое C>B из-за резерва
само по себе не вызывает продаж, остаток восстанавливается поступлениями.

Выручка непроданных бумаг не cash. Unassigned SELL разрешён отдельно,
но BUY требует фактической availability после FILL. Деньги внутренних
продаж закреплены за областью; перенос только по явным правам родителей,
вложенные запреты сохраняются. Не удваивать предполагаемую выручку в B.
Receipt.cash содержит только доказанные денежные факты исполнителя; SDK
оставляет {}. Неизвестная выручка изолированных областей откладывает BUY с INFO;
общая availability только ограничивает пулы, не доказывает принадлежность.
Netting не финансирует покупки, не списывает защищённое владение BUY_ONLY,
не создаёт встречные внешние заявки ради виртуального владельца.

BUY приоритет наибольшее абсолютное уменьшение денежного недовеса за лот,
затем UID/path; не нормировать на цену лота. Бюджет/cash_floor/own caps/цель
ограничивают, BUY floor без перепокупки; пакеты эквивалентны пошаговому правилу.
SELL nearest только после общего netting с физическими/free/owner caps,
без short; Decimal-вклады владельцев точно равны lots*lot.
Атрибуция фиксирована на проход, fresh reads не переделывают владельца.
Decimal prec28, сравнения строго без epsilon; exact_sum/exact_product
и коррекция только построенных пропорций, без исправления плохих входов.

## Исполнение, ошибки и события

Готовность: нет active orders/loading/числовых/depository/exchange blockers.
Доступность API/BESTPRICE по полным данным/статусу и own GetMaxLots,
без NORMAL_TRADING-only. Рублёвое исполнение, не неявный FX=1.
Не использовать недоказанную money-blocked/balance/total_order_amount
как cash/чистую выручку. Money верхняя граница, own money/caps обязательны.
Валюта оценки current_price.currency не native currency инструмента:
нерублёвую оценку назначения отклонять до плана без явного FX, не исключать
позицию и не суммировать разноимённые деньги. RUB nano до суммы сохраняется.
Совместимые дубликаты UID агрегируются без изменения nano B,
несовместимые отклоняются.

Каждая SELL/BUY отправляется один раз, FILL и requested=executed=intent.lots.
Неизвестный/частичный ответ останавливает текущий проход с ERROR,
без retry/ожидания/отмены, уже совершённое не откатывается.
После SELL свежие positions/money/caps, BUY перепланируется при той же main.
Неотражённое поступление/несовпавшие позиции откладывают BUY с INFO.
Перед дальнейшими покупками доступность снова читается.
Unary interceptor deadline10s, меньший existing timeout сохранить,
стрим не ограничивать unary-дедлайном.

ValueError (включая UnsupportedSourceError) данных фатален, без retry;
DataAccessError/ExecutionDataError сохраняют cause транспорта.
OrderExecutionError стоп прохода: mainflow продолжает события,
run_sync/облако пробрасывают, ложного Success нет. Programming errors не маскировать.
Event_accounts непустой tuple уникальных непустых строк без пробелов;
should_rebalance строго bool, это инициатор проверки, не разрешение сделки.
Начальная синхронизация до подписки, одно position_events, повтор после конца/
транспортного сбоя. COMPOSITE вызывает всех детей без short-circuit.
Непустой dst: все money/security blockers проверяются.
Таймера цен CLI нет; внешний облачный таймер остаётся.

## Разработка и приёмка

Для нового алгоритма плоский модуль/тесты и одна регистрация
register_algorithm('NAME', AlgorithmDefinition(prepare_source, create)).
Реестр не понимает схемы/номера/типы. CLI/Runner/движок не менять.
Новый состав требует только JSON с уникальным name.
Архив автоматически включает плоские модули и нескрытые JSON дерева,
не внешние каталоги; проверять реальные конфиги/пять методов вне репозитория
с блокировкой сети. Диагностические scripts/тесты в архив не включаются.

Все деньги/количества Decimal, форматирование без экспоненты, целые с .0,
nano до 9 знаков. Не исправлять публичные опечатки postiton_to_string.
INFO причины/ограничения, ERROR ошибки, WARNING неоднозначные UID.
Токены не выводить, SDK-запросы ради логов не добавлять.
Новых зависимостей без согласования не добавлять.
Ручные правки apply_patch; сохранять чужие dirty changes, без rollback.

Тесты pytest, TDD RED -> GREEN, чистые бизнес-проверки отдельно от SDK.
Порты autospec(spec_set=True), SDK-сервисы autospec(inspect.unwrap(Service),
instance=True,spec_set=True) с явными DTO; конструкторы/сеть не вызывать.
Проверять точное число/порядок/аргументы и отсутствие лишних вызовов.
Стримы конечные, заканчивать контролируемым TestException, рабочий стрим
не обязан завершаться. AST/import-тесты сохраняют границы SDK.

Офлайн-сценарии и seeds/hashes в test/data/rebalance_scenarios.json.
ACCOUNT holdout 24000 seed20261003: .0092, совпадение91.3375%.
Публичный snapshot 44 котировок не является реальным ACCOUNT; состава
такого счёта нет, на нём отличие не измерено. Не выдавать модель за real account.
Старый .004 допускается только компактным чистым oracle офлайн-утилиты
и историческими свидетельствами, не runtime helper.
Отчёт показывает turnover/order count/estimated commission/half-L1 error,
повторения, monthly2000, random prices/tail/off-grid/table boundaries,
резервные дефициты/вложенность/shared UID и оценочный риск владения.
Модельные FILL/комиссия/marks не гарантируют BESTPRICE/settlement/экономию.
1000-сетка и крайние 0/null экстраполяции не гарантируют произвольные бюджеты.
Live sandbox/GitHub CI отдельный санкционированный запуск; локальный pytest
не закрывает их. Внешние проверки здесь не выполняются.
Перекрывающиеся облачные вызовы/активные заявки проверить перед промышленным
частым таймером; истории/кеша/внешнего хранилища этот проект не добавляет.
