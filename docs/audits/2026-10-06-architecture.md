# Архитектурный аудит autorepeater

Дата: 6 октября 2026 года. Проверенный HEAD: `cc838e876da0c7765b2a8beef4b0b58758720180`, ветка `feature/composite-strategy`.

## Итог

Основа архитектуры подходит этому приложению: один процесс, собственные модели, стратегии через общий протокол, раздельные порты чтения и отправки, нейтральный исполнитель и SDK-адаптеры. Циклов импортов между 33 рабочими модулями не обнаружено. ACCOUNT, INDEX и COMPOSITE не импортируют SDK или друг друга. Добавление алгоритма действительно не требует менять CLI, облачный handler и исполнитель, пока алгоритм укладывается в существующий финансовый контракт.

Однако отсутствие SDK-импортов не равно полной изоляции. Наиболее существенные проблемы находятся в доверительных границах DTO, распределении ответственности между расчётом и исполнением, сохранении фактов заявки и проверках доставки. Необходимая сложность вложенных бюджетов, владения и денежных ограничений смешана с устранимой сложностью обходов, переэкспортов, вывода и повторного построения тех же данных.

Ниже перечислены **46 самостоятельных замечаний** после объединения повторов: **2 H, 31 M, 13 L**. Список включает воспроизведённые дефекты публичных границ, структурную связанность и пробелы приёмочных проверок. Это не 46 установленных производственных инцидентов. Отдельно перечислены принятые ограничения и операционные риски, не включённые в это число.

- **H:** защитная граница допускает неподтверждённый торговый факт или отправку несогласованного торгового плана. Приоритет исправления высокий; производственный ущерб не установлен.
- **M:** существенный пробел контракта, восстановления, согласованности или проверки изменений; либо заметная связанность важных областей.
- **L:** локальная связанность, лишняя работа, хрупкость сопровождения либо дефект на заведомо искусственном числовом входе.

Документация рассматривалась как свидетельство текущих правил и принятых ограничений, а не как запрет пересматривать архитектуру. Для каждого пункта важны сценарий и последствия, а не соответствие учебному паттерну.

## Объём и метод

Аудит разделён между пятью независимыми субагентами: стратегии/контракты; нейтральное исполнение; SDK-граница; входы/конфигурация/оркестрация; тесты/утилиты/доставка. Дополнительно проверены сквозные зависимости, Decimal-контекст и наиболее серьёзные контрпримеры. Использованы текущие исходники, CodeGraph, AST-анализ, существующие тесты и офлайн-порты. Проверены рабочие модули, точки входа, три рабочие утилиты, архитектура тестов и конфигов, Makefile, requirements и workflows. Исторические документы калибровки не считались runtime-контрактом.

Проверки в этом аудите:

- Полная локальная свита: **2190 passed**, 18 предупреждений; около 79 секунд.
- Выбранные проверки контрактов, подготовки, финансового плана и диагностики: **174 passed**.
- Субагент SDK-границы: **177 passed**; субагент исполнения: **54 passed**. Это пересекающиеся наборы, их нельзя складывать с полным числом тестов.
- Повторены шесть офлайн-проб исполнения и проба несовпадающего ответа PostOrder. Никаких реальных заявок не отправлялось.
- Проверены пути и номера строк в 230 ссылках пяти частных отчётов: отсутствующих файлов и выходов за границы файлов не обнаружено.
- Тесты не доказывают безопасность конкурентных облачных запусков, атомарность внешних данных, фактическую комиссию или воспроизводимость облачного окружения. Live API, sandbox и GitHub CI здесь не запускались.

## Карта ответственности

```text
CLI / cloud event
  -> выбор алгоритма и источника, подготовка конфигов
  -> Runner: создание Client и SDK-адаптеров
  -> AutoRepeater: чтение данных, контекст, финансовый план
  -> Strategy: профиль, main/control, решение и дерево бюджетов
  -> OrderPlan: владение, netting, лоты, ограниченные денежные пулы
  -> execute_plan: отправка, подтверждение, ожидание и свежие чтения
  -> SDK adapters -> Т-Инвестиции

CLI после начального прохода: position_events -> инициатор нового прохода.
Cloud: один проход; повторные вызовы инициирует внешний таймер.
```

Фактические границы не везде совпадают с этой картой: стратегии и расчётные helpers вызывают presentation/logging; reporting содержит SDK-подобные чтения; часть чистой денежной политики находится в исполнителе; офлайн-утилита повторяет приложение, а не только моделирует внешние порты.

## Два наиболее серьёзных дефекта

### C01. H: ответ PostOrder теряет собственную идентичность

Источники: [order_execution.py:18](/home/kazakovaleksey/PythonProjects/autorepeater/autorepeater/order_execution.py:18), [order_execution.py:49](/home/kazakovaleksey/PythonProjects/autorepeater/autorepeater/order_execution.py:49), [execution.py:55](/home/kazakovaleksey/PythonProjects/autorepeater/autorepeater/execution.py:55).

`submit_order` формирует квитанцию через `_receipt(response, intent.uid, intent.side)`. UID и направление берутся из намерения, а не из ответа брокера. Поэтому последующая проверка совпадения квитанции с намерением фактически проверяет уже подставленные поля. В отличие от этого, `get_order_state` использует UID и направление фактического ответа.

Повторённый офлайн-контрпример: намерение `SELL expected-uid`, фактический SDK-ответ `FILL BUY other-uid`, одинаковые счётчики лотов. Получена квитанция `SELL expected-uid`; `_receipt_matches` вернул `True`. Был вызван только mock, не API.

Минимальное исправление: преобразовывать и проверять фактические поля ответа, не заменять недостающую идентичность аргументами запроса. Несовпадение или отсутствие необходимых полей останавливает проход. Добавить отрицательные SDK-тесты с несовпадающими UID/направлением.

### C02. H: исполнитель доверяет неподтверждённой согласованности OrderPlan

Источники: [execution.py:262](/home/kazakovaleksey/PythonProjects/autorepeater/autorepeater/execution.py:262), [order_plan.py:24](/home/kazakovaleksey/PythonProjects/autorepeater/autorepeater/order_plan.py:24), [purchase_plan.py:33](/home/kazakovaleksey/PythonProjects/autorepeater/autorepeater/purchase_plan.py:33).

Перед отправкой проверяются StrategyPlan и форма отдельных намерений, но не вся связь очередей с политикой, владением, lot/rules и совокупными ограничениями. Корректная сумма `pieces` сама по себе не доказывает право SELL. Штатный builder формирует безопасный план, но публичная граница исполнения и изменяемые вложенные DTO не обеспечивают сохранение этого свойства.

Повторённый офлайн-контрпример: builder создал ноль продаж для листа BUY_ONLY, владеющего X=100. Замена `plan.sells` на формально корректный SELL X=100 привела к одному вызову фиктивного executor. Это дефект защитной границы, а не утверждение о том, что штатный builder продавал защищённые позиции в облаке.

Минимальное исправление: до первого внешнего эффекта валидировать OrderPlan целиком либо пересчитывать/сверять допустимые SELL по имеющимся правилам. Проверять фазу, пути, UID, lot, владение, права и суммарные ограничения. Проверка должна предшествовать отправке, а не ошибке обновления ownership после неё.

## Остальные замечания

В каждой строке указаны нарушенная граница или сценарий, последствие и минимальный вариант улучшения. Номера строк относятся к проверенному HEAD. Отметка «встроенный путь не затронут» ограничивает доказанное последствие, а не отменяет дефект публичного контракта.

### Финансовые модели и контракт расширения

| ID | Уровень | Замечание, последствия и минимальное изменение | Код |
| --- | --- | --- | --- |
| C03 | M | **Теряется валютная размерность источника ACCOUNT.** Валютные позиции исключаются, но оставшиеся бумаги с оценками 100 RUB и 100 USD могут складываться в числовые 200 и профиль 50/50. Это воспроизведено без SDK; неверная реальная заявка не показана. До построения профиля проверять совместимость валют оценки; будущую поддержку FX вводить явно, а не через неявное равенство валют. | [account_strategy.py:50](/home/kazakovaleksey/PythonProjects/autorepeater/autorepeater/account_strategy.py:50) |
| C04 | M | **Межузловые инварианты финансового дерева не являются частью общего валидатора.** Он принимает удвоенное/потерянное владение, parent target u=99 при детях u=2/u=3, а также parent floor=0 при положительных floors детей и нераспределённом бюджете. Встроенный COMPOSITE строит эти связи правильно; дефект допуска проявляется у нового производителя плана. Проверять сохранение UID-владения, сумму целей и защиту floors/нераспределённого бюджета; агрегирование цен и точность сумм закрепить отдельно. Не требовать target_value==budget и не запрещать дробные цели. | [strategy_plan.py:196](/home/kazakovaleksey/PythonProjects/autorepeater/autorepeater/strategy_plan.py:196) |
| C05 | M | **Обязательные marks ошибочно допускают None.** `validate_context` передаёт marks в helper, где None означает «не проверять карту». Контекст проходит проверку, затем control падает с TypeError. Проверять обязательную карту независимо; оставить необязательность у helper для других задач. Штатный движок такой контекст не создаёт. | [strategy_plan.py:152](/home/kazakovaleksey/PythonProjects/autorepeater/autorepeater/strategy_plan.py:152) |
| C06 | M | **Readiness проверяется через truthiness.** Snapshot с `limits_ready='false'` допускает BUY в офлайн-пробе. SDK-адаптер bool проверяет, но нейтральный порт и его новый адаптер не защищены общей границей. Проверять строгий bool без coercion/fallback до торговли. | [order_plan.py:36](/home/kazakovaleksey/PythonProjects/autorepeater/autorepeater/order_plan.py:36) |
| C07 | M | **Непустой корневой path допускается раньше, чем исполнитель обнаруживает несовместимость.** План с root `(7,)` строится, SELL подтверждается mock, затем `_sale_paths` падает с KeyError(()). Обычный корень () не затронут. Либо запретить поддерево до отправки, либо ограничить подъём фактическим корнем переданного дерева. | [execution.py:148](/home/kazakovaleksey/PythonProjects/autorepeater/autorepeater/execution.py:148) |
| C08 | M | **Распределение подтверждённых кредитов зависит от порядка SELL и теряет допустимое финансирование.** Общая выручка U=100 и изолированная выручка A=100 при одинаковых недовесах дают 150 или 200 доступных денег в зависимости от порядка. Запреты не нарушаются, но часть законного финансирования не используется. Это воспроизведено на порте с доказанной receipt.cash; SDK сейчас возвращает {}. Сначала учитывать наиболее ограниченные кредиты, затем гибкие; проверить перестановки. | [execution.py:166](/home/kazakovaleksey/PythonProjects/autorepeater/autorepeater/execution.py:166) |
| C09 | M | **Проверенные факты остаются изменяемыми.** Frozen StrategyContext/Profile/Plan содержат dict и mutable TargetPortfolio; дети разделяют ссылку marks. IndexConfig/IndexQuote также mutable. Изменение ссылки после валидации меняет соседние расчёты и уже проверенный план. Встроенное вредоносное изменение не обнаружено. Фиксировать карты один раз на границе и использовать read-only Mapping; не копировать весь граф при каждом helper-вызове. | [strategy_plan.py:17](/home/kazakovaleksey/PythonProjects/autorepeater/autorepeater/strategy_plan.py:17) |
| C10 | M | **Детерминизм арифметики зависит от ambient Decimal-контекста.** constants меняет глобальную precision при импорте через logging; не все расчёты задают собственный контекст. Для бюджета 154000.123456789 и reserve=.006 `available_budget` даёт 153076 при prec6 и 153076.122716048266 при prec28. В штатной среде prec28 установлен; риск проявляется при встраивании/изменении контекста. Определить локальный финансовый контекст на расчётной границе и отделить арифметику от настройки логгера. | [strategy_budget.py:5](/home/kazakovaleksey/PythonProjects/autorepeater/autorepeater/strategy_budget.py:5), [constants.py:7](/home/kazakovaleksey/PythonProjects/autorepeater/autorepeater/constants.py:7) |
| C22 | L | **ACCOUNT перезаписывает дубликат UID раньше проверки совместимости.** В snapshot остаётся последняя quantity, а total содержит все позиции. Совместимые количества 2+3 с ценой 10 дают dict quantity=3 и total=50, затем source total mismatch. Не допускает неверную торговлю, но теряет данные и выдаёт вторичную причину отказа. Агрегировать совместимые дубликаты до расчёта либо явно отвергать их на read-границе. | [account_strategy.py:55](/home/kazakovaleksey/PythonProjects/autorepeater/autorepeater/account_strategy.py:55) |
| C45 | L | **SELL nearest теряет информацию при делении на экстремально большой lot.** На разрешённом искусственном lot=10**40+1 и delta=1.5E40 планирует 2 вместо точного ближайшего 1. В реальных инструментах такие лоты не предполагаются. Сравнивать остаток с половиной lot без потери информации либо явно ограничить числовой контракт; не маскировать epsilon. | [order_plan.py:153](/home/kazakovaleksey/PythonProjects/autorepeater/autorepeater/order_plan.py:153) |

### Слои, ответственность и наблюдаемость

| ID | Уровень | Замечание, последствия и минимальное изменение | Код |
| --- | --- | --- | --- |
| C11 | M | **Чистая денежная политика находится в orchestration.** `_sale_paths` и `_fresh_money` вычисляют права и денежные пулы без I/O; repeater также собирает root-экономику и атрибуцию. Менять финансовое правило приходится рядом с отправкой/ожиданиями. Вынести конкретное чистое построение/пересчёт исполнения в общий расчётный слой; оркестратору оставить порядок чтений и эффектов. Не превращать все три строки сборки контекста в отдельные сервисы. | [execution.py:166](/home/kazakovaleksey/PythonProjects/autorepeater/autorepeater/execution.py:166), [repeater.py:73](/home/kazakovaleksey/PythonProjects/autorepeater/autorepeater/repeater.py:73) |
| C12 | L | **Расчёт и подготовка знают способ presentation.** Стратегии вызывают reporting при загрузке, атрибуция пишет WARNING, planners пишут операционные SKIP; loaders импортируют presentation ради предупреждения каталога. Финансовые результаты можно переиспользовать, но диагностика связана с конкретным выводом. Возвращать небольшие диагностические факты/причины и выводить их в shell; существующий callback предупреждений каталога передавать снаружи. | [strategy_allocation.py:160](/home/kazakovaleksey/PythonProjects/autorepeater/autorepeater/strategy_allocation.py:160), [index_config.py:9](/home/kazakovaleksey/PythonProjects/autorepeater/autorepeater/index_config.py:9) |
| C13 | M | **SDK-представления пересекают anti-corruption boundary через diagnostic_text.** В собственные DTO записывается str(position)/str(response), затем reporting выводит его. Нейтральное поле не делает содержимое независимым от SDK; изменение repr меняет логи, а полный DTO трудно контролировать. Утечка реального токена не установлена. Передавать только выбранные собственные диагностические поля и форматировать их снаружи. | [tinvest_strategy_data.py:218](/home/kazakovaleksey/PythonProjects/autorepeater/autorepeater/tinvest_strategy_data.py:218) |
| C14 | M | **StrategyDataError использует SDK str(error) как внешний текст.** Cause сохраняется, но обычный лог теперь зависит от транспортного repr/metadata. Соседние adapters используют другие правила. Нужен единый безопасный перевод ошибок: публичная причина, выбранные code/method/UID/tracking ID, исходный cause отдельно, без raw kwargs/DTO. Реальная утечка секрета не доказана. | [tinvest_strategy_data.py:62](/home/kazakovaleksey/PythonProjects/autorepeater/autorepeater/tinvest_strategy_data.py:62) |
| C15 | M | **При timeout PostOrder теряется собственный request ID.** Он создаётся внутри аргументов вызова; quota retry сохраняет его правильно, но ошибка и лог не позволяют оператору восстановить этот ключ при неизвестном результате. Создавать локально до вызова и сохранять в безопасном типизированном контексте ошибки. Не считать request ID автоматически равным broker order_id; не повторять timeout вслепую. | [order_execution.py:36](/home/kazakovaleksey/PythonProjects/autorepeater/autorepeater/order_execution.py:36) |
| C16 | L | **Legacy SDK-shaped display смешан с нейтральными моделями.** portfolio.py содержит TargetPortfolio и клиент operations.get_portfolio; money.py знает units/nano вложенных SDK-позиций; reporting одновременно форматирует свои DTO и читает SDK-подобный клиент. Разделить legacy просмотр/преобразование и собственные модели/форматирование; не добавлять новый общий фасад поверх каждого helper. | [portfolio.py:8](/home/kazakovaleksey/PythonProjects/autorepeater/autorepeater/portfolio.py:8), [money.py:38](/home/kazakovaleksey/PythonProjects/autorepeater/autorepeater/money.py:38), [reporting.py:106](/home/kazakovaleksey/PythonProjects/autorepeater/autorepeater/reporting.py:106) |
| C17 | L | **Execution-адаптер импортирует приватный нормализатор соседнего адаптера.** `_decimal_value` стал межмодульным API strategy-адаптера, связывая две независимые реализации портов. Вынести только действительно общую SDK-normalization в нейтрально названный SDK-helper с публичным контрактом. Это не прямой SDK leak в движок. | [tinvest_execution_data.py:13](/home/kazakovaleksey/PythonProjects/autorepeater/autorepeater/tinvest_execution_data.py:13) |
| C18 | L | **Общие торговые примитивы принадлежат purchase-модулю и скрыты переэкспортами.** OrderIntent обеих сторон, обходы, difference/floor/take_pieces живут в purchase_plan; execution импортирует их через order_plan. Разместить общие primitives у общего торгового плана и импортировать из владельца определения. Не строить отдельную иерархию BUY/SELL без нужды. | [purchase_plan.py:18](/home/kazakovaleksey/PythonProjects/autorepeater/autorepeater/purchase_plan.py:18), [execution.py:13](/home/kazakovaleksey/PythonProjects/autorepeater/autorepeater/execution.py:13) |
| C46 | M | **Результат прохода не сообщает его состояние.** Tuple receipts одинаково представляет нормальный NOOP, блокировку до торговли, завершённые сделки и SELL с отложенными BUY. Причины/stage существуют только в логах. `plan.buys` является preview: исполнение перепланирует покупки. Вернуть компактный ExecutionOutcome с receipts, состоянием и причиной; отделить preview от результата. Не обещать транзакционный откат. | [execution.py:248](/home/kazakovaleksey/PythonProjects/autorepeater/autorepeater/execution.py:248), [order_plan.py:32](/home/kazakovaleksey/PythonProjects/autorepeater/autorepeater/order_plan.py:32) |

### Вычислительная сложность

| ID | Уровень | Замечание, последствия и минимальное изменение | Код |
| --- | --- | --- | --- |
| C19 | M | **Вложенность многократно пересчитывает профили и повторно валидирует поддеревья.** Каждый уровень COMPOSITE повторяет recursive allocation_profile и проверки, листья тоже повторяют profile; нет явной границы глубины/числа узлов. Для цепочки работа может расти квадратично, большие trusted configs упираются в recursion/время. Фиксировать вычисленный профиль на один проход и валидировать дерево один раз на правильной границе; отдельно ввести понятный предел ресурсов подготовки, если нужны большие композиции. Не делать глобальный кеш. | [composite_strategy.py:66](/home/kazakovaleksey/PythonProjects/autorepeater/autorepeater/composite_strategy.py:66) |
| C20 | L | **BUY рассчитывает очередь, чей хвост немедленно отбрасывается.** purchase_intents повторяет обход leaves для кандидатов; execute_plan исполняет только первый intent, затем строит всю очередь заново по fresh state. Пересчёт после сделки нужен, вычисление отброшенного хвоста нет. Общий selector одного следующего intent и отдельный preview полного списка могут использовать одну математику. | [purchase_plan.py:97](/home/kazakovaleksey/PythonProjects/autorepeater/autorepeater/purchase_plan.py:97), [execution.py:285](/home/kazakovaleksey/PythonProjects/autorepeater/autorepeater/execution.py:285) |
| C21 | L | **Runtime INDEX всегда собирает полную диагностическую историю префиксов.** Main/control сохраняют passes/allocations, хотя builder сразу берёт только target. Отделить сбор трассы от того же единственного калькулятора, не дублировать алгоритм и не удалять полезную диагностику калибровки. Существенная задержка на текущих 44 инструментах не измерена. | [index_strategy.py:265](/home/kazakovaleksey/PythonProjects/autorepeater/autorepeater/index_strategy.py:265) |

### Жизненный цикл, входы и конфигурация

| ID | Уровень | Замечание, последствия и минимальное изменение | Код |
| --- | --- | --- | --- |
| C23 | M | **Readiness проверяется слишком поздно.** После loading=True/limits_ready=False стартовый проход всё ещё читает GetMaxLots. В реальном логе это дважды закончилось INTERNAL70001 и 502. Финансовую валидацию сохранить, но trade rules не читать при заведомой неготовности. Причина серверного 70001 не доказана; не вводить blanket retry INTERNAL. Задача уже заведена как issue #5 с низким операционным приоритетом. | [repeater.py:89](/home/kazakovaleksey/PythonProjects/autorepeater/autorepeater/repeater.py:89), [issue #5](https://github.com/gorpsys/autorepeater/issues/5) |
| C24 | M | **Reconnect стрима не включает сверочный пересчёт.** Начальный проход только один; после разрыва/конца восстановление открывает подписку, но не сверяет authoritative state. Если транспорт не replay-ит пропущенные события, CLI может не заметить изменение до следующего события. Добавить reconciliation после восстановления; гарантии replay брокера отдельно проверить. Таймер котировок для этого не нужен. | [repeater.py:106](/home/kazakovaleksey/PythonProjects/autorepeater/autorepeater/repeater.py:106) |
| C25 | M | **Reconnect не ограничен паузой или backoff.** Немедленный конец генератора или быстрый DataAccessError вызывают tight loop, лишнюю нагрузку и шум. Добавить ограниченный reconnect backoff с тестовым временем и reset после нормального потока; не менять отдельно согласованный quota retry unary-запросов. | [repeater.py:109](/home/kazakovaleksey/PythonProjects/autorepeater/autorepeater/repeater.py:109) |
| C26 | M | **Обзор всех счетов является обязательной зависимостью запуска CLI.** Runner.run до mainflow вызывает print_all_portfolio с дополнительными чтениями/метаданными. Отказ ненужного счёта или display-query мешает торговле выбранным dst. Явно отделить обзор счетов от рабочего запуска либо сделать необязательный вывод по уже загруженному снимку. | [runner.py:40](/home/kazakovaleksey/PythonProjects/autorepeater/autorepeater/runner.py:40) |
| C27 | M | **Runner.__init__ настраивает глобальный local logging, в том числе после cloud logging.** Конструирование объекта меняет процессную среду и затрудняет совместное использование режимов/тесты. Настройку логов выполнять один раз в соответствующей точке входа; Runner получает зависимости, а не выбирает формат. Нарушение JSON при штатном cold start не доказано. | [runner.py:29](/home/kazakovaleksey/PythonProjects/autorepeater/autorepeater/runner.py:29) |
| C28 | M | **Облачная оболочка не валидирует форму event/query и типы параметров.** Произвольное queryStringParameters может вызвать TypeError/AttributeError до понятной ошибки входных данных. Стандартный envelope провайдера снижает риск, но интерфейс handler не фиксирует это. Отделить проверку транспортного конверта от разрешения defaults и подготовки стратегии; не маскировать programming errors общим except. | [serverless.py:14](/home/kazakovaleksey/PythonProjects/autorepeater/autorepeater/serverless.py:14) |
| C29 | M | **JSON duplicate keys теряются до проверки схемы.** json.load принимает два reserve/weight и оставляет последнее значение, в том числе во вложенных объектах. Строгая последующая schema не может обнаружить неоднозначность. Использовать общий parser с отказом на повторном ключе; это не то же самое, что дубликаты name между файлами. | [composite_config.py:88](/home/kazakovaleksey/PythonProjects/autorepeater/autorepeater/composite_config.py:88) |
| C30 | M | **INDEX молча принимает опечатку необязательного экономического поля.** Например, неверное имя min_position_value оставляет default=0 и меняет хвост. COMPOSITE строгий, ACCOUNT намеренно игнорирует extras. Нужна явная единая политика схем/расширений: отвергать или предупреждать неизвестные экономические поля, сохраняя осознанную совместимость. Это пересмотр текущей терпимости, не нарушение документации. | [index_config.py:118](/home/kazakovaleksey/PythonProjects/autorepeater/autorepeater/index_config.py:118) |
| C31 | L | **Filesystem discovery дублируется и уже различается по error translation.** ACCOUNT/INDEX/COMPOSITE отдельно разрешают env/path и перечисляют JSON. Выделить небольшой общий resolver/enumerator, но оставить каталоги и схемы независимыми. Не делать универсальную конфигурационную платформу. | [account_config.py:49](/home/kazakovaleksey/PythonProjects/autorepeater/autorepeater/account_config.py:49), [composite_config.py:99](/home/kazakovaleksey/PythonProjects/autorepeater/autorepeater/composite_config.py:99) |
| C32 | L | **Config catalog сохраняет только текст чужой ошибки.** При выбранном повреждённом документе нет исходной exception chain для точного разбора парсинга/файла. Хранить cause рядом с диагностическим текстом и пробрасывать его при фатальном выборе; предупреждение о чужом конфиге остаётся безопасным и кратким. | [config_catalog.py:34](/home/kazakovaleksey/PythonProjects/autorepeater/autorepeater/config_catalog.py:34) |

### Тесты, модельная приёмка и доставка

| ID | Уровень | Замечание, последствия и минимальное изменение | Код |
| --- | --- | --- | --- |
| C33 | M | **Фильтр symlink не защищает корень упаковываемого дерева.** Проверяются дети configs, но symlink самого configs/пакета или явно добавляемого root-файла позволяет захватить внешний источник. В checkout наличие такого корня не обнаруживалось; проба была синтетической, внешние файлы не читались. Проверять корни и принадлежность resolved paths допустимой базе до упаковки. | [build_yandex_archive.py:20](/home/kazakovaleksey/PythonProjects/autorepeater/scripts/build_yandex_archive.py:20) |
| C34 | M | **ZIP публикуется неатомарно.** Открытие конечного файла в режиме w уничтожает предыдущий успешный архив до чтения всех членов. Поздняя ошибка оставляет частичный ZIP. Сборка падает честно, но файл по обычному пути уже не надёжный артефакт. Собирать во временный файл рядом и атомарно заменять после успеха/проверки. | [build_yandex_archive.py:27](/home/kazakovaleksey/PythonProjects/autorepeater/scripts/build_yandex_archive.py:27) |
| C35 | M | **Archive parity проверяет исходники, но не чистую установку зависимостей.** subprocess с -I использует site-packages текущего interpreter; packaged requirements отдельно не устанавливаются, Client/Runner частично подменены. Эти tests полезны, но не доказывают installability облака. Добавить отдельный санкционированный clean-environment acceptance; обычные unit tests остаются офлайн. | [test_yandex_archive.py:213](/home/kazakovaleksey/PythonProjects/autorepeater/test/test_yandex_archive.py:213) |
| C36 | M | **Окружение зависимостей и транспорт установки не полностью воспроизводимы.** Есть direct pins, но нет транзитивного lock/constraints+hashes; инструменты CI разрешаются независимо и плавают. extra-index расширяет источники, trusted-host ослабляет TLS-проверку. Проверить необходимость trusted-host и закрепить проверяемое разрешение зависимостей. Компрометация пакетов не установлена; согласовать изменения, не добавлять зависимости автоматически. | [requirements.txt:1](/home/kazakovaleksey/PythonProjects/autorepeater/requirements.txt:1) |
| C37 | M | **Офлайн replay имеет собственную копию application orchestration.** model_plan повторяет destination/profile/marks/recognized/unassigned/context/ownership/build_order_plan. Исправление runtime-прохода не обязательно попадёт в калибровку. Общая чистая сборка прохода должна использоваться обоими; модель заменяет внешние факты, не приложение. | [check_rebalance_policy.py:227](/home/kazakovaleksey/PythonProjects/autorepeater/scripts/check_rebalance_policy.py:227) |
| C38 | M | **Модельная приёмка использует более благоприятный execution contract.** ModelExecution всегда ready, имеет огромные caps, немедленный FILL по marks и подтверждённую cash при SELL; SDK cash={}. Об этом честно написано, но единая сценарная приёмка не покрывает реальные отложения/неопределённость. Добавить scripted replay неблагоприятных состояний на тех же портах; не объявлять модель гарантией экономии/исполнения. | [check_rebalance_policy.py:192](/home/kazakovaleksey/PythonProjects/autorepeater/scripts/check_rebalance_policy.py:192) |
| C39 | M | **Architecture fitness tests имеют разрешающие слепые зоны.** AST-проверка тела не опровергает I/O через helper/alias; неизвестный модуль вне NEUTRAL и суффикса _strategy по умолчанию почти не ограничен. Требовать явную классификацию всех runtime-модулей и проверять реальные pure-вызовы с запрещённым I/O/портами после snapshot. Это не попытка доказать чистоту произвольного Python статически. | [test_architecture.py:214](/home/kazakovaleksey/PythonProjects/autorepeater/test/test_architecture.py:214), [test_architecture.py:249](/home/kazakovaleksey/PythonProjects/autorepeater/test/test_architecture.py:249) |
| C40 | M | **Часть ключевых execution tests использует unrestricted Mock.** Найдены 36 присваиваний data/executor=Mock() без spec в одном модуле; это не число дефектных тестов. Неизвестные методы и формы порта могут проходить. Использовать autospec/spec_set собственных портов, сохраняя явные сценарные DTO; SDK constructors/network не нужны. | [test_order_execution.py:109](/home/kazakovaleksey/PythonProjects/autorepeater/test/test_order_execution.py:109) |
| C41 | L | **Общие fixtures/helpers принадлежат тестовым сценарным модулям.** Разные области импортируют support из большого test_autorepeater и друг из друга. Перемещение сценария меняет независимые consumers. Вынести реально общие builders/autospec fixtures в test support/conftest с явной областью; не делать одну мегабиблиотеку фикстур. | [test_execution_diagnostics.py:9](/home/kazakovaleksey/PythonProjects/autorepeater/test/test_execution_diagnostics.py:9) |
| C42 | M | **Глобальная autouse fixture меняет временной контракт всех обычных тестов.** SETTLEMENT_TIMEOUT=0 вместо production30; специальные тесты времени есть, но интегральные сценарии не проверяют production-переходы. Внедрить небольшой scripted clock/timing policy и явно выбирать сценарий ожидания; не заставлять тесты реально ждать 30 секунд. | [conftest.py:8](/home/kazakovaleksey/PythonProjects/autorepeater/test/conftest.py:8) |
| C43 | L | **Часть бизнес-тестов закрепляет внутреннюю трассу INDEX.** Помимо quantities/cost проверяются passes и промежуточные составы, затрудняя эквивалентную оптимизацию. Оставить это в отдельных tests диагностического формата/алгоритма; публичный финансовый контракт проверять результатами и инвариантами. Не удалять тесты преднамеренного порядка отсечения хвоста. | [test_autorepeater.py:2853](/home/kazakovaleksey/PythonProjects/autorepeater/test/test_autorepeater.py:2853) |
| C44 | L | **У ZIP нет полного provenance и побайтной воспроизводимости.** Автоматический обход берёт текущие untracked/ignored .py/JSON, timestamps и permissions; один HEAD не определяет единственный архив. Это удобно для локальной работы, но усложняет сопоставление cloud errors с кодом. Добавить manifest состава/хешей/версии и явный release mode; dirty local build не запрещать молча. | [build_yandex_archive.py:17](/home/kazakovaleksey/PythonProjects/autorepeater/scripts/build_yandex_archive.py:17) |

## Ограничения и риски, не выданные за дефекты

| ID | Риск/граница гарантий | Оценка и следующий шаг |
| --- | --- | --- |
| R01 | Quota retry неограничен, unary deadline ограничивает попытку, а не операцию. | Это явно согласованный контракт: RESOURCE_EXHAUSTED каждые 10 секунд, остальные ошибки не повторять. В CLI ожидание может быть бесконечным, в облаке ограничено общим timeout; 30-секундные окна не прерывают текущий RPC/retry. Сначала уточнить политику общей отмены/временного бюджета, не менять retry скрыто. |
| R02 | Свежие последовательные чтения portfolio/positions/orders/status/caps не атомарны; первый BUY и пакет SELL используют уже построенный snapshot. | Период между проверкой и отправкой является TOCTOU-окном. Own caps, блокировки, fresh reads и подтверждение уменьшают риск, но не дают атомарности. Нужен явный допустимый read window и сверка критических фактов; брокер всё равно остаётся последней защитой. Реальный инцидент из-за этого в аудите не показан. |
| R03 | Нет межпроцессной эксклюзивности торговли по одному dst. | Последовательный execute_plan не защищает от двух cloud invocations/CLI одновременно, которые оба увидели отсутствие active orders. В проверенном суточном логе перекрытий не было. Перед частым production-таймером проверить конфигурацию concurrency и account-scoped serialization. Если нужен lease, это отдельное внешнее состояние с ценой сопровождения, не повод немедленно добавить БД. |
| R04 | Владение shared UID пересчитывается из текущих коэффициентов, а не из истории покупок. | Те же 10 штук становятся 5/5 или 9/1 без сделки при изменении профилей. BUY_ONLY защищает владельца, приписанного сейчас. Это принятый риск эвристики; строгая историческая принадлежность требует журналирования/учёта, которого сейчас нет. Альтернатива упрощения: запретить пересечение составов, если бизнес от этого не пострадает. |
| R05 | После BUY область списывает оценку по marks, а не подтверждённый фактический debit. | Общая fresh availability ограничивает счёт, но не доказывает стоимость каждого владельца с комиссией/slippage. Резерв уменьшает риск, не является доказательством. Для строгой внутрисчётной бухгалтерии нужен контракт доказанных fill/debit facts. Не использовать недокументированные поля как чистую выручку. |
| R06 | Явно пустой dst облака заменяется окружением/default account. | Это существующая документированная политика, а не показанная неверная сделка. Риск неверного назначения существенно опаснее удобства fallback. Отдельно согласовать различение «ключ отсутствует» и «ключ пуст», сохраняя осознанный запуск по defaults. |
| R07 | Global registry и env-based discovery изменяемы и видны всем запускам процесса. | Подготовка сохраняет фабрику и источник, runtime повторно registry не читает: это хорошая защита. Для нынешней единовременной регистрации приемлемо. При runtime plugins/нескольких независимых приложениях нужен immutable registry/settings snapshot на composition root, не service locator внутри стратегий. |
| R08 | У цен нет явно определённой политики максимального возраста. | PriceQuote/IndexQuote сохраняют time, но сам факт наличия timestamp не определяет допустимость старой котировки/закрытого рынка. Это недостающий бизнес-SLA, а не доказанная неправильная цена. Уточнить, какие источники и интервалы допустимы для оценки/отправки. |
| R09 | Доказательная сила калибровки ограничена моделью и снимками. | ACCOUNT holdout не является реальным составом рабочего счёта; 1000-сетка и крайние диапазоны не покрывают любой бюджет. Текущие документы это явно признают. Нужны независимые сценарии/источники данных, а не повышение coverage ради процента. |
| R10 | Доступность облачного HTTP-входа, IAM, защита токена и внешняя политика запуска не проверены исходниками. | По коду нельзя доказать ни наличие, ни отсутствие внешней авторизации. Проверить способ передачи секретов, отсутствие секретов в query/access logs, запрет мутаций через GET при HTTP-экспозиции, роли, timeout и повторную доставку таймера. Это отдельный инфраструктурный аудит, без чтения локальных секретных файлов. |

## Реально обнаруженные паттерны

Полнота оценивается качественно: наличие названия класса или процента coverage не означает реализацию паттерна. Все источники относятся к таблицам замечаний и карте выше.

| Паттерн | Насколько реализован | Уместность в этом приложении |
| --- | --- | --- |
| Ports and Adapters / Hexagonal Architecture | Основной каркас реализован: StrategyData, ExecutionData, OrderExecutor и собственные DTO. Неполон из-за legacy SDK-shaped display, diagnostic_text и переводов ошибок. | Высокая: позволяет менять SDK и отдельно тестировать деньги/торговлю. Нового фреймворка не нужно. |
| Dependency Inversion и manual constructor DI | Исполнитель и AutoRepeater зависят от Protocol, Runner собирает adapters. Время, logging/settings и часть helpers остаются ambient. | Высокая; достаточно ручной сборки, DI-container не оправдан. |
| Composition Root | Runner/точки входа выполняют сборку; registry находится в strategies. Logging выбран в конструкторе и конфиг discovery читает env глубже. | Высокая; стоит сделать root более явным, но не переносить туда бизнес-арифметику. |
| Strategy | Общие пять методов, одинаковый runtime API, алгоритмы выбираются регистрацией. Opaque snapshot и prepared_source намеренно type-erased. | Полностью оправдан для добавления алгоритмов без правки исполнителя. Можно улучшить typed контракт snapshot внутри реализации, не навязывать общий SDK DTO. |
| Composite | Реальное рекурсивное дерево с одинаковым API ребёнка, событиями, бюджетами и агрегированной целью. Повторные обходы/инварианты требуют доработки. | Оправдан вложенностью и shared UID; это не декоративное использование паттерна. |
| Registry + Factory | Явная регистрация prepare/create; prepared factory сохраняется и не ищется повторно. Registry mutable, фабрики используют object. | Оправдан небольшим числом алгоритмов. Entry-point scanning и plugin framework сейчас лишние. |
| Two-phase preparation / validation before effects | Конфиг и цикл проверяются до токена/Client; финансовое дерево до отправки. Полнота ослаблена C01/C02/C04/C05/C06. | Очень высокая для торгового приложения. Нужно укрепить существующие границы, а не распределить проверки случайно по всем helpers. |
| Anti-Corruption Layer | SDK DTO/enums в основном преобразованы в свои модели, точные units/nano сохранены. Неполна через repr, legacy views и synthetic PostOrder identity. | Высокая; C01 наиболее важный дефект именно этого паттерна. |
| Functional Core / Imperative Shell | Pure target/policy/allocation отделены от SDK. Граница частичная: logging в расчётах, финансовые helpers в orchestrator, ambient Decimal. | Высокая; ключевой путь к простоте будущих доработок. |
| DTO / Data Mapper | Собственные dataclasses и конвертеры присутствуют. Frozen поверхностный, инварианты задаются внешними validators не полностью. | Оправдано. DTO с функциями не следует автоматически заменять тяжёлыми DDD-entities. |
| Pipeline и явные фазы исполнения | SELL -> подтверждение -> readiness/positions -> перепланирование BUY. Фазы есть в коде/логах, но нет полноценного результата прохода и явной модели всех переходов. | Оправдано; небольшой enum/outcome и отдельная чистая state transition лучше универсального workflow engine. |
| Command-like intent и Receipt | OrderIntent отделён от SDK-запроса; квитанция содержит выбранные факты. C01/C02/C15 показывают неполноту доверительной границы. | Оправдано; command bus и сериализация команд пока не требуются. |
| Event-driven reconciliation | Подписка на позиции инициирует проверку, но не разрешает сделку; cloud использует таймер. Нет восстановления состояния при reconnect. | Оправдано. Это не Event Sourcing: история событий не является источником состояния. |
| CQRS-подобное разделение capabilities | Чтение и отправка разделены портами. Нет отдельных read/write databases, projection и event store. | Лёгкое разделение оправдано; называть это полной CQRS-архитектурой неверно. |
| Retry policy + per-RPC deadline | Квотный retry централизован, один request ID сохраняется, timeout установлен в Runner. Общий operation deadline и cancel не реализованы. | Частично оправдано требованиями API. Общий retry всего прохода опасен для сделок. |
| Snapshot-scoped metadata cache | begin_snapshot очищает метаданные, dynamic account/status/prices/caps не кешируются. Это локальный session scope, не атомарный snapshot/UoW транзакции. | Оправдан для уменьшения запросов; глобальный stale cache не нужен. |
| Fail-safe / conservative execution | Неизвестная выручка не финансирует BUY, blockers закрывают доступность, FILL проверяется, short не создаётся. Защитные DTO-границы неполны. | Очень высокая; безопасность важнее попытки исполнить весь план любой ценой. |
| Architecture fitness functions и deterministic test doubles | AST/import tests, autospec, finite streams, replay существуют. Default-allow и unrestricted mocks ослабляют защиту. | Высокая; нужно закрывать пробелы, а не превращать tests в проверку каждого внутреннего вызова. |

Не обнаружены полноценные Saga с компенсациями, Event Sourcing, Outbox, Unit of Work БД, Repository persistence, микросервисы, broker-backed message bus, классический Template Method через наследование или Circuit Breaker. Они не обязательны: особенно автоматическая компенсация уже совершённой биржевой сделки могла бы ухудшить надёжность.

## Матрица антипаттернов

«Все известные антипаттерны» не образуют конечного общепринятого стандарта. Ниже приведена широкая проверяемая матрица из **60 категорий**, применимых к архитектуре, сопровождению, интеграции и доставке. «Не выявлен» означает отсутствие подтверждения в проверенной области, а не математическое доказательство отсутствия во всех возможных состояниях. «Частично» не означает, что любой похожий приём нужно удалить.

| № | Антипаттерн / smell | Результат | Основание |
| --- | --- | --- | --- |
| 1 | Big Ball of Mud | Не выявлен | Слои и направленные порты реально существуют; импортных циклов нет. |
| 2 | God Object / Blob | Не выявлен | Нет объекта, объединяющего SDK, стратегии, конфиги и торговлю. |
| 3 | God Module / функциональный dumping ground | Частично | reporting/money/portfolio смешивают разные обязанности, C16; execution содержит денежную политику, C11. Сам размер файла не доказательство. |
| 4 | Spaghetti control flow | Частично | Денежные состояния и деревья распределены по helpers; C11/C18/C46. Доменная сложность значительна и необходима. |
| 5 | Циклические зависимости | Не выявлен | AST-граф 33 runtime-модулей без циклов, включая imports через package. |
| 6 | Layer violation | Выявлен | Domain/config -> presentation; financial policy -> execution shell; C11/C12/C16. |
| 7 | Leaky Abstraction | Выявлен | SDK repr/SDK-shaped display и identity laundering, C01/C13/C14/C16. |
| 8 | Inappropriate Intimacy | Выявлен локально | Приватный нормализатор и скрытые переэкспорты, C17/C18. |
| 9 | Feature Envy | Частично | Execution интерпретирует денежное дерево вместо отдельной чистой политики, C11; функции над DTO сами по себе не дефект. |
| 10 | Shotgun Surgery | Частично | Общий display/SDK shape, копия replay и test support заставляют менять несвязанные места, C16/C37/C41. Новый алгоритм не требует глобальной правки. |
| 11 | Divergent Change | Выявлен | reporting/portfolio/money меняются по UI, SDK и domain причинам, C16; Runner по logging и wiring, C27. |
| 12 | Copy-paste / Duplicate Business Logic | Выявлен | model_plan дублирует приложение; discovery и units/nano helper-варианты, C17/C31/C37. |
| 13 | Temporal Coupling | Выявлен | begin_snapshot/read/validate/effect, readiness поздно и mutable plans; C02/C09/C23. Часть порядка неизбежна. |
| 14 | Hidden Side Effects | Выявлен | Logging в constructor, global Decimal, чтения внутри reporting; C10/C16/C26/C27. |
| 15 | Global Mutable State | Частично | Registry, logging и Decimal; C10/C27/R07. Registry используется только при prepare, что ограничивает риск. |
| 16 | Service Locator | Частично, ограниченно | Env и registry дают ambient подготовку, R07. Runtime получает сохранённые зависимости, не ищет сервисы глобально. |
| 17 | Singleton abuse | Не выявлен | Стандартный logger не равен singleton бизнес-сервиса. |
| 18 | Primitive Obsession | Частично | UID/path/side/status, currency maps и DTO инварианты, C04/C06/C09. Не оправдывает по отдельному классу на каждую строку. |
| 19 | Stringly Typed State | Частично | SELL/BUY/status/reason и shape guards; C01/C06/C46. Small enums у границ возможны. |
| 20 | Boolean Blindness / flag complexity | Частично | mode/redistribution/force/readiness связаны правилами; C04/C06. Флаги имеют смысл, но должны валидироваться как разрешённые состояния. |
| 21 | Data Clumps / parallel collections | Частично | ownership/marks/rules/money передаются связанными картами, C02/C04. Общий validated view лучше случайной синхронизации dict. |
| 22 | Anemic Domain Model | Частично как риск инвариантов | DTO + чистые функции здесь уместны; проблема не в отсутствии методов, а в C04/C05/C09. |
| 23 | Mutable validated object / aliasing | Выявлен | Frozen неглубокий и общий marks, C09; исполнение рассчитывает на сохранение проверки, C02. |
| 24 | Trusting internal producers / permissive boundary | Выявлен | C01/C02/C04/C05/C06/C07. Публичный контракт должен переживать ошибку нового adapter/strategy. |
| 25 | Validation scattered / inconsistent invariants | Выявлен | Строгие bool rules, truthy readiness, optional helper для mandatory marks, C04/C05/C06. |
| 26 | Overengineering / abstraction proliferation | Не подтверждён как системный | 33 модуля на около 3899 строк; многие границы защищают реальные правила. C18/C19 требуют упрощения, но не слияния всего приложения. |
| 27 | Speculative Generality | Не выявлен системно | Strategy/Composite/ports используются реальными алгоритмами. Поддерево исполнения имеет неполный контракт, C07. |
| 28 | Middle Man / forwarding layer | Частично | Переэкспорты C18 и legacy view C16; не основание удалять полезные ports/adapters. |
| 29 | Dead Code / Boat Anchor | Не подтверждён | Исторические assets и диагностические helpers имеют отдельное назначение. Полный public API usage за пределами репозитория неизвестен. |
| 30 | Lava Flow / устаревшие объяснения | Частично в навигации | Legacy threshold/display терминология сосуществует с новым контрактом; исторические calibration docs не runtime. Требуется явная маркировка, не массовое удаление. |
| 31 | Magic Numbers / hidden operational policy | Частично | 30/2/10 секунд и defaults доступны в разных модулях; C42/R01/R06. Сам согласованный параметр не ошибка. |
| 32 | Busy Waiting / retry storm | Выявлен | Немедленный reconnect без backoff, C25. Quota retry со sleep не busy wait. |
| 33 | Retry without idempotency | Для quota не выявлен; межпроходно ограничен | PostOrder kwargs сохраняют ID; C15/R03 показывают границу гарантий. |
| 34 | Exactly-once illusion | Риск неверной интерпретации | Нет межпроходного журнала/serialization. Здесь не следует обещать exactly-once, R03/C15. |
| 35 | Fire-and-forget trading | Не выявлен | Receipt/FILL/SELL polling проверяются; C01 ослабляет идентичность факта, но нет намеренного забывания ответа. |
| 36 | TOCTOU | Принятый риск | R02; C23 является отдельной подтверждённой ошибкой gating. |
| 37 | Split-brain / concurrent writers | Условный операционный риск | R03; фактической настройки deployment и гонки этот аудит не доказывает. |
| 38 | Stale / mixed snapshot | Принятый риск | Последовательные API-read и отсутствие age policy, R02/R08. Cache сознательно ограничен проходом. |
| 39 | Cache invalidation ownership leak | Частично | Lifecycle begin_snapshot требует правильного caller; глобального stale market cache нет. |
| 40 | Hidden shared monetary pool | Частично | Области и права явны, но C08/R04/R05 показывают ограничения расчёта и принадлежности фактов. |
| 41 | Swallowed Exceptions / false success | Частично на result-семантике | Ошибки SDK сохраняют cause и прекращают проход; fatal cloud errors не маскируются. Deferred/noop outcomes неоднозначны, C46/C32. |
| 42 | Exception Laundering / representation leak | Выявлен | SDK str наружу и catalog flattening, C14/C32; synthetic identity C01 имеет сходную проблему доверия. |
| 43 | Logging as sole source of workflow state | Выявлен | Stage/reason/отложенные покупки только в log, C46. |
| 44 | Sensitive / unbounded raw logging | Риск представления подтверждён | diagnostic_text и RequestError str, C13/C14; реальные secrets в них не обнаруживались. |
| 45 | O(n²) repeated work | Структурно выявлен | Профили, рекурсивная валидация, очередь BUY и trace retention, C19/C20/C21. Производственный профиль времени не измерен. |
| 46 | Resource-unbounded graph / algorithm | Частично | Trusted configs без depth/node limit, C19; quota ожидание отдельно принято, R01. |
| 47 | Order-dependent greedy allocation | Выявлен | Одинаковые денежные факты дают 150/200 в зависимости от порядка, C08. |
| 48 | Numeric context leak | Выявлен | C10 и искусственный rounding контрпример C45. |
| 49 | Fragile / White-box tests | Частично | Трассы калькулятора и imports из scenario tests, C41/C43. Нужные тесты tail-order сохранять. |
| 50 | Mock overuse / permissive mocks | Выявлен локально | Unrestricted Mock вместо autospec в важных execution tests, C40. Явные SDK DTO в других областях полезны. |
| 51 | Test-only execution contract | Выявлен | Global timeout0 и благоприятная ModelExecution, C38/C42; специальные negative/polling unit tests есть. |
| 52 | Common-mode oracle failure | Частично | Replay копирует orchestration и использует production калькуляторы, C37/C38. Независимые инварианты нужны дополнительно, не вместо reuse. |
| 53 | Coverage theatre / ложная уверенность процента | Риск, не установленная практика | 2190 tests прошли, но C01/C02/C39 остаются. Пороги не менялись; coverage не объявлен доказательством архитектуры. |
| 54 | Default-allow architecture rules | Выявлен | Новый модуль и indirect helper могут обходить fitness checks, C39. |
| 55 | Non-hermetic build/test | Частично | Текущие site-packages и раздельное разрешение dependencies, C35/C36. |
| 56 | Non-atomic artifact publication | Выявлен | ZIP перезаписывается до успеха, C34. |
| 57 | Artifact provenance drift | Выявлен как пробел | Dirty/untracked сборка без manifest, C44. |
| 58 | Path boundary / symlink trust | Выявлен как дефект проверки | Корень configs/package не защищён, C33; фактическая атака не установлена. |
| 59 | Silent config coercion/default/ambiguity | Частично | Duplicate keys, unknown economic fields и dst fallback, C29/C30/R06. Decimal coercion/bool/NaN в schema в основном отвергаются правильно. |
| 60 | Distributed monolith / chatty microservices / ORM-N+1 / transactional external I/O | Не применим к текущему устройству | Нет микросервисов, ORM или открытой store-транзакции. Есть много SDK-read, но это проверяется как API load/cache/snapshot, а не выдуманная ORM-проблема. |

## Что ещё проверить для надёжности и простоты

1. **Fault-injection прохода целиком:** timeout после принятия PostOrder, ошибочный ответ UID/side, PARTIALLYFILL, смена blockers между reads, падение после первой SELL, изменение cash при BUY, повторная доставка cloud event. Не только unit-проверка отдельного helper.
2. **Конкурентный сценарий одного dst:** два прохода с одинаковым начальным снимком; определить реальные гарантии deployment. При необходимости сериализация по счёту, а не глобальная блокировка всех стратегий.
3. **Property/metamorphic tests денежных инвариантов:** сохранение владения, отсутствие short и перепокупки, совместимость floors, перестановки кредитов, эквивалентность пакетного/пошагового BUY, независимость от mutable aliases. Новые библиотеки только после согласования; часть свойств проверяется генераторами стандартной библиотеки.
4. **Contract acceptance по реальным SDK-фактам:** выбранные документы/публичные контракты, обезличенные fixtures нестандартных responses, чистая установка архива. Реальную торговлю и sandbox запускать только отдельно санкционированно.
5. **Операционная стоимость:** числа RPC и максимальное окно снимка, холодный старт, memory после загрузки Shares/Etfs, время глубоких композитов и всех префиксных cuts. Не оптимизировать 44 бумаги по предположению без измерения.
6. **Секреты и внешняя доступность:** источник токена, HTTP method/IAM/access logs, dst default и права account token; по одному коду невозможно подтвердить безопасность инфраструктуры.
7. **Управление финансовой конфигурацией:** version/schema, duplicate keys, неизвестные поля, review изменений reserve/weights/limits, объяснимый manifest запущенного состава. Не менять расчёты как часть организационного refactor.
8. **Статическая проверка типов собственных ports/DTO:** в частности bool/state/maps, prepared factory и snapshot types. Это отдельная возможность; отсутствующий type checker не надо добавлять скрыто как новую зависимость.
9. **Проверяемое восстановление:** что делать оператору с unknown request ID, где видеть partial outcome, как безопасно дождаться active order и повторить reconciliation. Не считать повтор всей торговли безусловным восстановлением.
10. **Навигация и архитектурная документация:** актуальная карта владельцев modules и glossary budget/capital/cash/floor/receipt; исторические документы явно помечать. Каждое ADR должно объяснять альтернативы и цену выбора, а не только фиксировать текущие файлы.

## Предлагаемое направление, без большого переписывания

Сохранить один процесс и основные ports/strategies. Целевой разрез:

- **Чистое ядро:** свои DTO и инварианты, стратегии, атрибуция, бюджеты, денежная политика и выбор следующей сделки. Внешний SDK и presentation здесь не нужны.
- **Application shell:** подготовка запуска, общая сборка прохода, lifecycle событий и finite execution session с явным outcome. SDK-факты подаются через существующие ports.
- **SDK adapters:** normalization, реальные факты ответа, deadline/retry, безопасные ошибки. Общие units/nano helpers находятся здесь, не в денежном domain-formatting.
- **Composition root и доставка:** CLI/cloud defaults, logging, registry/settings snapshot, clean artifact acceptance/provenance.

Порядок отдельных небольших работ:

1. Защитить C01/C02/C05/C06/C07 отрицательными tests и проверками до эффектов; договориться об инвариантах C04. Сохранить нынешнюю торговую арифметику, кроме отдельно подтверждённых дефектов.
2. Исправить восстановление C24/C25, сохранить request ID C15, ввести outcome C46; C23 остаётся низкоприоритетной issue #5 согласно решению пользователя.
3. Выделить общую чистую сборку прохода и денежный пересчёт C11/C37; нормализовать владельцев примитивов C16/C17/C18. Не делать новый facade layer.
4. После этого убрать повторную работу C19/C20/C21 и зависимость диагностики C12/C13/C14; предварительно зафиксировать equivalence/invariant tests.
5. Отдельно укрепить parser, сборку и fitness/tests C29-C44. Без повышения порогов coverage, новых зависимостей или изменения GitHub triggers ради отчёта.

Не рекомендуются без новых требований: разбиение на микросервисы, DI-container, универсальный workflow engine, bus, Event Sourcing, автоматическая saga-компенсация сделок, БД ради паттерна, отдельный класс для каждого UID/Decimal, переписывание всех DTO в rich entities. Надёжность здесь прежде всего повышают более точные доверительные границы, явные результаты и единая проверяемая математика.

## Частные отчёты и воспроизведения

Это дополнительные рабочие артефакты аудита, а не runtime-зависимости. Основные выводы и весь объединённый список приведены выше.

- [Стратегии и контракты](/tmp/autorepeater-audit-strategies.md).
- [Исполнение](/tmp/autorepeater-audit-execution.md).
- [SDK-адаптеры](/tmp/autorepeater-audit-adapters.md).
- [Входы, конфигурация, lifecycle](/tmp/autorepeater-audit-entry-config.md).
- [Тесты и доставка](/tmp/autorepeater-audit-tests-build.md).
- [Шесть воспроизводимых офлайн-проб](/tmp/autorepeater-audit-execution-probes.py).

Во время аудита рабочий код, конфиги стратегий, workflow и торговые параметры не изменялись. Единственное добавление в репозиторий — этот отчёт.
