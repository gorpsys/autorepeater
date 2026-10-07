# План: достоверные квитанции брокера

Статус: проект для согласования, реализация запрещена до подтверждения пользователя.
Issue: [#6](https://github.com/gorpsys/autorepeater/issues/6), первая в архитектурной очереди.
Место: текущая рабочая папка, отдельная ветка `feature/architecture-broker-receipts`.

## Цель

Закрыть C01 и C15: исполнитель не должен принимать подставленную идентичность заявки за факт брокера; при неизвестном результате оператор должен видеть идентификатор отправленного запроса.

Рабочие пользователи: разработчики, агенты и оператор облачного/локального запуска. Данные: намерение заявки, ответ брокера, безопасный контекст ошибки. Внешнее хранилище, новые зависимости и новые торговые правила не нужны.

Состав портфелей, резервы, бюджеты, округления, правила SELL/BUY и повторной проверки исполнения не меняются. Не пересматриваем весь контракт стратегии в этом PR.

## Проверенные первоисточники

База плана: актуальный `origin/master`, SHA `e3987cac247f4bc413ba9495bea5aa925bd69f7c`. Незавершённые локальные правки не считаются утверждённой реализацией или первоисточником действующего контракта.

| Источник | Что подтверждено | Почему важно |
| --- | --- | --- |
| [order_execution.py:18](https://github.com/gorpsys/autorepeater/blob/e3987cac247f4bc413ba9495bea5aa925bd69f7c/autorepeater/order_execution.py#L18) | `_receipt` получает UID/side аргументами и копирует status/ID/счётчики из ответа | UID/side квитанции не обязательно являются фактами ответа |
| [order_execution.py:36](https://github.com/gorpsys/autorepeater/blob/e3987cac247f4bc413ba9495bea5aa925bd69f7c/autorepeater/order_execution.py#L36) | `submit_order` создаёт request ID внутри аргументов, передаёт в `_receipt` поля intent | После исключения ID недоступен; проверка идентичности становится тавтологией |
| [order_execution.py:51](https://github.com/gorpsys/autorepeater/blob/e3987cac247f4bc413ba9495bea5aa925bd69f7c/autorepeater/order_execution.py#L51) | `get_order_state` уже читает фактические direction/instrument_uid | Два ответа должны пользоваться одной границей преобразования |
| [execution.py:28](https://github.com/gorpsys/autorepeater/blob/e3987cac247f4bc413ba9495bea5aa925bd69f7c/autorepeater/execution.py#L28) | `OrderExecutionError` сейчас не содержит собственного контекста | Можно добавить небольшое необязательное поле без SDK-типа в исполнителе |
| [execution.py:55](https://github.com/gorpsys/autorepeater/blob/e3987cac247f4bc413ba9495bea5aa925bd69f7c/autorepeater/execution.py#L55) | `_receipt_matches` сравнивает собственную квитанцию с намерением | Это правильное место семантической проверки, если адаптер не подделывает факты |
| [execution.py:67](https://github.com/gorpsys/autorepeater/blob/e3987cac247f4bc413ba9495bea5aa925bd69f7c/autorepeater/execution.py#L67) | SELL NEW/PARTIALLYFILL опрашивается по broker order ID | Не подменять этот ID request ID и не отправлять повторную SELL |
| [execution.py:99](https://github.com/gorpsys/autorepeater/blob/e3987cac247f4bc413ba9495bea5aa925bd69f7c/autorepeater/execution.py#L99) | `_submit` принимает только совпадающий полностью подтверждённый FILL | Существующая остановка должна работать и на первом ответе PostOrder |

Дополнительно без сети и без конструирования клиента повторно проверены версия, поля и enum установленного SDK: `t-tech-investments==1.51.0`, как в [requirements.txt:3](https://github.com/gorpsys/autorepeater/blob/e3987cac247f4bc413ba9495bea5aa925bd69f7c/requirements.txt#L3). Через `dataclasses.fields` подтверждено: и `PostOrderResponse`, и `OrderState` содержат `instrument_uid`, `direction`, `execution_report_status`, `order_id`, `lots_requested`, `lots_executed`. Это проверка схемы DTO, не гарантия заполненности каждого поля в любом ответе API.

Контрпример из аудита: SELL expected-uid получает SDK FILL BUY other-uid при совпавших счётчиках. Старый адаптер возвращает SELL expected-uid, поэтому нейтральная проверка принимает ответ. Это офлайн-проба, не установленная неверная реальная сделка.

## Выбранное решение

Укрепляем существующий Anti-Corruption Layer, без нового фасада и общей платформы валидации.

1. Адаптер преобразует фактический ответ SDK в собственный `ExecutionReceipt` и проверяет форму SDK-полей.
2. Нейтральный исполнитель сравнивает квитанцию с intent, проверяет FILL/счётчики и принимает решение об остановке или существующем SELL polling.
3. Request ID создаётся один раз до PostOrder и сохраняется при транспортном отказе. Broker order ID поступает только из брокерского ответа; между ними не предполагается равенство.

Идентичность отсутствует или неоднозначна: остановка прохода, без fallback на intent. При несовпадении фактов запрещены последующие заявки. Уже отправленная заявка не отменяется и не откатывается автоматически.

## Границы ответственности

- `order_execution.py`: SDK enums/DTO, явное отображение допустимых значений, нормализация обязательных фактов, сохранение request ID на стороне отправки.
- `execution.py`: только собственные модели, сравнение с intent, подтверждение результата, безопасный контекст ошибки. SDK туда не импортируется.
- `tinvest_requests.py`: существующий quota retry; менять его политику в этой задаче нельзя.
- Диагностика: выбранные UID/side/лоты/request ID/broker ID/status, без SDK repr, raw kwargs и текстов с токенами.

Не распределяем дублирующие проверки по разным helpers: форма ответа принадлежит SDK-границе, соответствие намерению и допустимость продолжения принадлежат исполнителю.

## Технические детали

Общий преобразователь `_receipt(response)` больше не получает UID/side из intent. Допустим необязательный request ID как диагностический контекст, но не как источник фактов квитанции.

Проверки формы: непустые строковые UID/order ID; direction является экземпляром `OrderDirection` с допустимым BUY/SELL; status является экземпляром `OrderExecutionReportStatus` с явно поддерживаемым значением. Проверка класса enum предшествует сравнению или поиску в отображении: обычные числа, bool и строки отвергаются, даже если численно равны SDK enum. Лоты являются int, но не bool, неотрицательны и executed не больше requested. Ошибочные типы, UNSPECIFIED и неизвестные значения не превращаются в успешные факты. Корректные REJECTED/CANCELLED остаются отказами, а не FILL.

Отсутствующие или повреждённые обязательные поля ответа SDK приводят к `OrderExecutionError` с безопасной причиной, а не к `ValueError`/`TypeError`. Это сохраняет текущий жизненный цикл: CLI останавливает проход и продолжает обработку событий, run_sync/облако пробрасывают отказ без ложного Success. Не вводить общий catch-all: неожиданные программные ошибки не маскируются под ошибку данных брокера.

Смысловая проверка: UID/side/requested совпадают с intent; FILL требует executed=intent.lots. SELL NEW/PARTIALLYFILL продолжает существующее ожидание только после совпадения идентичности и корректных счётчиков; неподтверждённый BUY по-прежнему немедленно останавливает проход.

`OrderExecutionError` получает keyword-only `request_id: str | None = None` как собственный диагностический контекст. Существующие вызовы с одним message остаются допустимыми. Исходный RequestError сохраняется через exception cause; при распространении исключения контекст не теряется. Не менять текст ошибок всех остальных слоёв в этом PR.

При неизвестном результате PostOrder адаптер обязательно пишет безопасный ERROR-лог с тем же request ID, UID/side/лотами и фиксированной причиной, без `str(RequestError)`, raw kwargs или SDK repr. Это относится к двум путям: транспортному RequestError и повреждённому ответу, который невозможно преобразовать в достоверную квитанцию. В обоих случаях контекст сохраняется в OrderExecutionError; для транспортного отказа сохраняется исходный RequestError через cause. Наличие поля исключения само по себе не считается выполнением требования диагностики: текущий CLI выводит только `str(error)`. Request ID должен быть виден хотя бы в ERROR-записи адаптера в полном пути вызова; не требуется повторять его во всех общих логах исполнителя/CLI. Тест проверяет и видимость ID, и сохранение поля/исходного cause. Broker order ID выводится отдельно только при наличии проверенного факта ответа, не подставляется из request ID.

Request ID генерируется после успешного `validate_intent`, один раз до `call_api`, передаётся в тот же вызов и сохраняется при неизвестном результате. Невалидный intent не вызывает ни фабрику ID, ни SDK. RESOURCE_EXHAUSTED повторяет тот же unary-запрос с тем же ID каждые 10 секунд; timeout и другие ошибки не становятся поводом для повторной отправки. Межпроцессная exactly-once гарантия не заявляется.

## Точная карта изменений

Все ссылки в этом разделе закреплены на проверенном SHA. Номера строк относятся к исходному коду до реализации; после изменения файла они могут сдвинуться.

### 1. Общий перевод ответа SDK

Место: [order_execution.py:18](https://github.com/gorpsys/autorepeater/blob/e3987cac247f4bc413ba9495bea5aa925bd69f7c/autorepeater/order_execution.py#L18), функция `_receipt`.

На первом шаге заменить сигнатуру на `_receipt(response: PostOrderResponse | OrderState) -> ExecutionReceipt`, без UID/side/request ID в аргументах. Пользоваться существующим `OrderExecutionError(message)`; новый конструктор ошибки появится только на втором шаге. На втором шаге добавить keyword-only `request_id: str | None = None`, исключительно для контекста ошибок преобразования.

| Поле ответа | Собственное поле | Правило перевода и проверки |
| --- | --- | --- |
| `instrument_uid` | `uid` | Проверить непустую строку, передать исходное значение без нормализации и fallback на intent |
| `direction` | `side` | Сначала проверить `isinstance(..., OrderDirection)`, затем явное отображение BUY/SELL |
| `order_id` | `order_id` | Проверить непустую строку; сохранить broker ID без замены на request ID |
| `execution_report_status` | `status` | Сначала проверить `isinstance(..., OrderExecutionReportStatus)`, затем явное отображение поддерживаемых статусов |
| `lots_requested` | `lots_requested` | int, не bool, >=0; сравнение с intent остаётся в нейтральном исполнителе |
| `lots_executed` | `lots_executed` | int, не bool, 0..lots_requested; полное исполнение проверяется относительно intent отдельно |
| Денежные поля SDK | `cash` | Всегда `{}`: не превращать price/total_order_amount/commission в доказанную выручку |

Направления: `ORDER_DIRECTION_BUY -> 'BUY'`, `ORDER_DIRECTION_SELL -> 'SELL'`. `ORDER_DIRECTION_UNSPECIFIED` запрещён.

Статусы: `EXECUTION_REPORT_STATUS_FILL -> 'FILL'`, `REJECTED -> 'REJECTED'`, `CANCELLED -> 'CANCELLED'`, `NEW -> 'NEW'`, `PARTIALLYFILL -> 'PARTIALLYFILL'` с одинаковым SDK-префиксом для всех членов. UNSPECIFIED и отсутствующие в отображении значения запрещены. Не получать собственные статусы через произвольный `.name`, `str()` или удаление префикса: это не проверяет класс и допустимость enum.

Последовательность: прочитать только перечисленные поля; проверить класс direction и разрешённое значение, затем status, идентификаторы и счётчики; только после всех проверок создать ExecutionReceipt. Отсутствие поля через `getattr(..., None)` является плохими данными ответа, не поводом подставить данные запроса. Строки и числа не приводить к ожидаемому типу. Ошибки проверок выдавать с фиксированными причинами, например `invalid order direction`, `invalid order status`, `invalid order identity`, `invalid order lot counts`; сами неправильные значения не выводить.

Конвертер не сравнивает ответ с intent, не пишет логи и не читает SDK. Структурно корректный ответ с чужим UID/side должен оставаться квитанцией с чужими фактическими полями; его отклоняет существующий нейтральный исполнитель. Такое разделение необходимо для отрицательной регрессии C01, а не только проверки самого конвертера.

### 2. Отправка PostOrder и два пути неизвестного результата

Место: [order_execution.py:36](https://github.com/gorpsys/autorepeater/blob/e3987cac247f4bc413ba9495bea5aa925bd69f7c/autorepeater/order_execution.py#L36), `TInvestOrderExecutor.submit_order`.

Первый шаг: после существующего call_api вернуть `_receipt(response)`. Не менять генерацию request ID до второго шага.

Второй шаг: сохранить порядок `validate_intent(intent) -> request_id = str(order_id_factory()) -> call_api(..., order_id=request_id) -> _receipt(response, request_id=request_id)`. Фабрика вызывается ровно один раз на логическую отправку, а не на каждую квотную попытку. Не хранить последний request ID в executor: одновременные вызовы не должны перезаписывать локальный контекст друг друга.

Существующий блок `except RequestError` дополнить безопасным ERROR-логом с локальным ID и `raise OrderExecutionError(фиксированная причина, request_id=request_id) from error`. Отдельно обработать только OrderExecutionError от перевода полученного ответа: записать ERROR с тем же ID и фиксированной причиной и пробросить тот же объект ошибки с уже заполненным контекстом. Не объединять это с `except Exception`, не повторять PostOrder и не менять ID.

Перед call_api допустим небольшой INFO-лог с request ID и проверенным intent; после успешного перевода нужен INFO-лог связывания `request_id` и фактического `broker_order_id` с UID/side/status/лотами. Это даёт оператору связь со штатными подтверждениями. При корректной форме, но несовпадении с intent дальнейший ERROR нейтрального исполнителя идентифицируется по реальному broker ID; отдельный request ID не добавляется в структуру ExecutionReceipt ради этой связи.

### 3. Чтение уже отправленной заявки

Место: [order_execution.py:51](https://github.com/gorpsys/autorepeater/blob/e3987cac247f4bc413ba9495bea5aa925bd69f7c/autorepeater/order_execution.py#L51), `get_order_state`.

Сохранить call_api с аргументами account_id/order_id и текущий перевод RequestError с сохранением cause. Удалить отдельный словарь direction/проверку направления в этом методе: после чтения вызвать общий `_receipt(response)`.

Если преобразование ответило OrderExecutionError, записать безопасный ERROR с запрошенным broker order ID и фиксированной причиной, затем пробросить ошибку. Request ID для этого чтения не генерировать; фабрику ID не вызывать, PostOrder не отправлять. Сверка возвращённого broker ID с запрошенным остаётся в [execution.py:92](https://github.com/gorpsys/autorepeater/blob/e3987cac247f4bc413ba9495bea5aa925bd69f7c/autorepeater/execution.py#L92).

### 4. Собственная ошибка и неизменяемые границы исполнения

Место изменения: [execution.py:28](https://github.com/gorpsys/autorepeater/blob/e3987cac247f4bc413ba9495bea5aa925bd69f7c/autorepeater/execution.py#L28), только OrderExecutionError. Добавить конструктор с message и keyword-only request_id=None; сохранить поле и вызвать `super().__init__(message)`. Старые тексты и вызовы с одним аргументом остаются прежними; вывод ID обеспечивается ERROR-логом адаптера, а не обязательным изменением str всех исключений.

Оставить без изменения поля [ExecutionReceipt:33](https://github.com/gorpsys/autorepeater/blob/e3987cac247f4bc413ba9495bea5aa925bd69f7c/autorepeater/execution.py#L33), API OrderExecutor, `_receipt_matches`, `_submit`, `_wait_for_order` и execute_plan. Проверки типов в SDK-конвертере не заменяют уже существующие защитные проверки нейтрального порта: другой адаптер может вернуть плохую собственную квитанцию. Не удалять эти проверки как «дубли».

Оставить без изменения [call_api:10](https://github.com/gorpsys/autorepeater/blob/e3987cac247f4bc413ba9495bea5aa925bd69f7c/autorepeater/tinvest_requests.py#L10), [CLI _local_pass:100](https://github.com/gorpsys/autorepeater/blob/e3987cac247f4bc413ba9495bea5aa925bd69f7c/autorepeater/repeater.py#L100), [Runner.run_sync:51](https://github.com/gorpsys/autorepeater/blob/e3987cac247f4bc413ba9495bea5aa925bd69f7c/autorepeater/runner.py#L51) и [serverless.handler:33](https://github.com/gorpsys/autorepeater/blob/e3987cac247f4bc413ba9495bea5aa925bd69f7c/autorepeater/serverless.py#L33). Они уже пробрасывают ошибку или останавливают текущий проход нужным образом. Будущие события CLI могут инициировать новый расчёт по свежим данным: это не слепой retry той же неизвестной заявки.

### 5. Позитивные фикстуры, которые обновляются в первом шаге

| Место | Изменение | Что сохраняет тест |
| --- | --- | --- |
| [test_order_execution.py:47](https://github.com/gorpsys/autorepeater/blob/e3987cac247f4bc413ba9495bea5aa925bd69f7c/test/test_order_execution.py#L47) | Для двух отправок X/Z подготовить два PostOrderResponse с соответствующим фактическим UID/SELL, а не один общий ответ без идентичности | Один ID на отправку и прежний порядок/аргументы SDK |
| [test_order_execution.py:77](https://github.com/gorpsys/autorepeater/blob/e3987cac247f4bc413ba9495bea5aa925bd69f7c/test/test_order_execution.py#L77) | После квотного отказа успешный DTO содержит X/SELL | Повтор использует тот же request ID и одну фабрику |
| [test_order_execution.py:318](https://github.com/gorpsys/autorepeater/blob/e3987cac247f4bc413ba9495bea5aa925bd69f7c/test/test_order_execution.py#L318) | Позитивный ответ чтения оформить явным OrderState с существующими полями | UID/side/счётчики PARTIALLYFILL, без PostOrder |
| [test_order_execution.py:353](https://github.com/gorpsys/autorepeater/blob/e3987cac247f4bc413ba9495bea5aa925bd69f7c/test/test_order_execution.py#L353) | Заполнить X/SELL в успешном PostOrderResponse | BUY/SELL/BESTPRICE enum остаются на SDK-границе |
| [test_order_execution.py:379](https://github.com/gorpsys/autorepeater/blob/e3987cac247f4bc413ba9495bea5aa925bd69f7c/test/test_order_execution.py#L379) | Использовать PostOrderResponse с X/SELL и явными MoneyValue для существующих денежных полей | cash по-прежнему пуст даже при наличии price/amount/commission |
| [test_tinvest_requests.py:52](https://github.com/gorpsys/autorepeater/blob/e3987cac247f4bc413ba9495bea5aa925bd69f7c/test/test_tinvest_requests.py#L52) | Добавить X/SELL в успешный DTO сразу в Task 1 | Ровно два квотных вызова с одинаковыми аргументами и ID |
| [test_index_instrument_selection.py:130](https://github.com/gorpsys/autorepeater/blob/e3987cac247f4bc413ba9495bea5aa925bd69f7c/test/test_index_instrument_selection.py#L130) | В фиктивной успешной покупке заполнить UID `new` и BUY | Выбор доступного режима фонда и положительное исполнение не меняются |
| [test_strategy_contract.py:385](https://github.com/gorpsys/autorepeater/blob/e3987cac247f4bc413ba9495bea5aa925bd69f7c/test/test_strategy_contract.py#L385) | configure_filled_sdk возвращает в DTO instrument_uid и direction именно имитируемой заявки | Штатное исполнение независимых стратегий через общий адаптер |

Это изменения данных SDK-фикстур, не отдельный рефакторинг всех mocks/support. Отрицательные намеренно повреждённые ответы не исправлять в валидные. Контейнер SimpleNamespace для поля orders допустим; позитивные ответы методов оформлять реальными DTO. Новые SDK-сервисы создавать через create_autospec(inspect.unwrap(OrdersService), instance=True, spec_set=True), без конструирования клиента/сети. Новые нейтральные порты также autospec/spec_set; не добавлять общий support в сценарные test-модули.

### 6. Матрица отрицательных и сквозных проверок

Создать отдельный `test/test_broker_receipts.py` с компактными локальными builders валидных SDK DTO и own/SDK autospec. Не импортировать новое общее API фикстур из другого сценарного test-модуля. Корректный DTO изменять по одному полю, чтобы ошибка проверяла заявленное условие, а не более ранний отсутствующий реквизит.

| Сценарий | Уровень проверки | Ожидаемый результат |
| --- | --- | --- |
| PostOrder FILL BUY other-uid в ответ на SELL expected-uid | Общий исполнитель + реальный SDK-адаптер + подменённый OrdersService | Квитанция сохраняет факты other-uid/BUY; проход отклоняет их, один PostOrder, ноль polling и последующих заявок |
| Пустой UID/ID, отсутствующие поля/None, неправильный enum/type, bool/нецелые/отрицательные/несогласованные счётчики | Каждый публичный метод SDK-адаптера | OrderExecutionError, без приведения типов/подстановки идентичности; повреждённые поля не попадают в логи |
| Невалидный intent | submit_order | Существующий ValueError до отправки; ноль вызовов ID factory/SDK. Это не повреждённый ответ брокера |
| RequestError DEADLINE_EXCEEDED/UNAVAILABLE после отправки | submit_order, затем сквозное распространение | request_id совпадает с отправленным ID, ERROR-запись содержит его, cause является исходным RequestError; повторной отправки нет |
| Повреждённый ответ уже отправленного PostOrder | submit_order на втором шаге | Такой же ID в OrderExecutionError и ERROR-записи; ноль повторных PostOrder, context не теряется при пробросе |
| RESOURCE_EXHAUSTED -> корректный FILL либо другой RequestError | call_api через submit_order | Пауза 10s с подменённым временем, одна фабрика, тот же ID во всех попытках; другой отказ прекращает retry |
| SELL NEW/PARTIALLYFILL -> FILL | execute_plan и чтение статуса | Поля и broker ID совпадают, существующий опрос по broker ID, без повторной SELL |
| Чужой broker ID/UID/side/счётчики при чтении статуса | Общий исполнитель + SDK-адаптер | Ошибка и остановка, не подтверждение и не новая отправка |
| BUY NEW/PARTIALLYFILL либо REJECTED/CANCELLED | Общий исполнитель | Немедленная остановка текущего прохода; pending BUY не получает новое ожидание |
| TypeError программирования в SDK-вызове | submit_order | Исходный TypeError не маскируется под OrderExecutionError |

Для сквозного CLI-пути расширить [test_runtime_policy.py:135](https://github.com/gorpsys/autorepeater/blob/e3987cac247f4bc413ba9495bea5aa925bd69f7c/test/test_runtime_policy.py#L135): публичный mainflow получает реальную ошибку адаптера на первом проходе, сохраняет её request_id/ERROR-запись, а поток завершается контролируемым тестовым исключением. Последующее событие может инициировать новый проход; это проверяется отдельно от отсутствия retry внутри первого.

Для облачного пути использовать существующую границу [test_runtime_policy.py:161](https://github.com/gorpsys/autorepeater/blob/e3987cac247f4bc413ba9495bea5aa925bd69f7c/test/test_runtime_policy.py#L161): handler не перехватывает OrderExecutionError и не возвращает Success. Проверить передачу того же собственного исключения с request_id/cause через подменённый Runner; отдельно подтвердить путь SDK-адаптер -> execute_plan -> sync_accounts без подмены самого адаптера. Подмена Runner проверяет только границу handler, не выдаётся за полную проверку SDK Client wiring или live cloud.

Сохранить [test_order_execution.py:371](https://github.com/gorpsys/autorepeater/blob/e3987cac247f4bc413ba9495bea5aa925bd69f7c/test/test_order_execution.py#L371) и [test_order_execution.py:442](https://github.com/gorpsys/autorepeater/blob/e3987cac247f4bc413ba9495bea5aa925bd69f7c/test/test_order_execution.py#L442): программная ошибка не маскируется, транспортный cause не теряется. Сохранить текущие neutral malformed receipt tests: SDK-проверки не доказывают безопасность любого другого адаптера.

Тесты polling должны явно установить окно и fake clock: [test/conftest.py:8](https://github.com/gorpsys/autorepeater/blob/e3987cac247f4bc413ba9495bea5aa925bd69f7c/test/conftest.py#L8) сейчас ставит timeout=0 глобально. В этих новых сценариях локально восстановить 30s/2s и подменить monotonic/sleep, не ждать в реальном времени. Глобальную fixture и временную политику в этом PR не рефакторить: это issue #12. Не менять общие 30/2/10 секунд.

Проверки логов: assert уровня ERROR и конкретного request ID, отдельно broker ID при валидном ответе; исключить искусственный секрет из details/metadata/SDK repr в сообщениях приложения. Считать отсутствие новых SDK-чтений ради логов по mock_calls; не закреплять количество всех лог-записей или полный текст traceback провайдера. Хранение cause не является обещанием очистить сторонний системный traceback: транспортный перевод всех ошибок относится к будущей issue #16.

### 7. Порядок проверки после согласования реализации

Команды ниже предназначены для будущей реализации, сейчас не запускаются. Interpreter `/tmp/autorepeater-install-check/bin/python`; PATH должен включать его bin для subprocess-тестов архива.

```bash
PATH=/tmp/autorepeater-install-check/bin:/usr/local/bin:/usr/bin:/bin pytest -q test/test_broker_receipts.py test/test_order_execution.py test/test_tinvest_requests.py test/test_index_instrument_selection.py test/test_strategy_contract.py test/test_runtime_policy.py
PATH=/tmp/autorepeater-install-check/bin:/usr/local/bin:/usr/bin:/bin pytest --cov --cov-report=term-missing --cov-report=xml -q
PATH=/tmp/autorepeater-install-check/bin:/usr/local/bin:/usr/bin:/bin coverage report --fail-under=90
PATH=/tmp/autorepeater-install-check/bin:/usr/local/bin:/usr/bin:/bin diff-cover coverage.xml --compare-branch=origin/master --fail-under=90
PATH=/tmp/autorepeater-install-check/bin:/usr/local/bin:/usr/bin:/bin pylint $(git ls-files --cached --others --exclude-standard '*.py')
PATH=/tmp/autorepeater-install-check/bin:/usr/local/bin:/usr/bin:/bin flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics
git diff --check
```

До учёта нового test-файла в Git diff diff-cover не видит его: построить diff-file без staging либо учесть отдельно, не заявлять полное покрытие неподтверждённых строк. После первого и второго шагов полный pytest зелёный; финальный шаг добавляет линтеры и явную проверку процентов. Проверки SDK-независимости исполнителя уже входят в общую свиту. Ничего не исключать из coverage ради результата.

В [docs/execution-adapter.md:75](https://github.com/gorpsys/autorepeater/blob/e3987cac247f4bc413ba9495bea5aa925bd69f7c/docs/execution-adapter.md#L75), [README.md:326](https://github.com/gorpsys/autorepeater/blob/e3987cac247f4bc413ba9495bea5aa925bd69f7c/README.md#L326) и [AGENTS.md:235](https://github.com/gorpsys/autorepeater/blob/e3987cac247f4bc413ba9495bea5aa925bd69f7c/AGENTS.md#L235) описать actual identity и различие двух ID. В разделах [README.md:366](https://github.com/gorpsys/autorepeater/blob/e3987cac247f4bc413ba9495bea5aa925bd69f7c/README.md#L366) и [AGENTS.md:244](https://github.com/gorpsys/autorepeater/blob/e3987cac247f4bc413ba9495bea5aa925bd69f7c/AGENTS.md#L244) уточнить неизменный request ID при quota retry; в [README.md:372](https://github.com/gorpsys/autorepeater/blob/e3987cac247f4bc413ba9495bea5aa925bd69f7c/README.md#L372) и [AGENTS.md:261](https://github.com/gorpsys/autorepeater/blob/e3987cac247f4bc413ba9495bea5aa925bd69f7c/AGENTS.md#L261) добавить ERROR-контекст транспортного отказа/повреждённого ответа и сохранение текущего жизненного цикла. Обновить только затронутый контракт, не переписывать документацию остальных алгоритмов. Реальный заполненный ответ API и установка/работа облака подтверждаются отдельной санкционированной проверкой, не локальным SDK DTO.

## Результат повторной проверки

Сверены текущий HEAD/origin/master, issue #6, исходники SDK-адаптера/исполнителя/оболочек, зависимые фикстуры и схема SDK 1.51.0. Противоречий текущему коду, C01/C15 и согласованной очереди не обнаружено. При детализации дополнительно закреплены два ранее недоопределённых пути: повреждённый ответ после PostOrder сохраняет request ID; ID генерируется только после успешного validate_intent. Эти уточнения сохраняют текущие границы и не меняют торговую арифметику. Новая техническая карта повторно проверена независимым агентом; технических блокеров не найдено. Проверены пути и номера строк закреплённых ссылок. Код/тесты не правились и не запускались.

## Шаги после согласования

### Task 1: Закрепить форму и идентичность квитанции

**Файлы:** изменить `autorepeater/order_execution.py`; создать `test/test_broker_receipts.py`; обновить только зависимые SDK-фикстуры в `test/test_order_execution.py`, `test/test_tinvest_requests.py`, `test/test_index_instrument_selection.py`, `test/test_strategy_contract.py`.

- [x] Написать RED-проверки на реальные SDK DTO с несовпадающими UID/side и отсутствующими обязательными полями. Тесты проверяют внешний результат, не внутреннюю трассу helpers.
- [x] Сделать общий перевод PostOrderResponse/OrderState из фактических полей, без synthetic identity и строкового преобразования произвольного enum.
- [x] Обновить старые позитивные фикстуры: заполнить факты реального ответа, не ослаблять новый контракт ради mocks. В частности, уже здесь добавить UID/direction в PostOrderResponse теста `test_post_order_retries_keep_one_idempotency_key` (`test/test_tinvest_requests.py:52`), не откладывать исправление этой фикстуры до Task 2.
- [x] Проверить правильные BUY/SELL FILL, SELL NEW/PARTIALLYFILL, REJECTED/CANCELLED, UNSPECIFIED, пустые UID/ID, bool/отрицательные/несогласованные лоты. Для direction/status отдельно проверить SDK enum против строки, обычного числа и bool.
- [x] Проверить `OrderExecutionError` на повреждённых ответах; несовпадающий SELL не вызывает polling и никакой следующий BUY. Общий исполнитель не импортирует SDK. Целевые проверки и полная свита GREEN до следующего шага.

### Task 2: Сохранить request ID при неизвестном результате

**Файлы:** изменить `autorepeater/order_execution.py`, `autorepeater/execution.py` только для контекста `OrderExecutionError`; проверки в `test/test_broker_receipts.py`, `test/test_tinvest_requests.py`, при необходимости `test/test_runtime_policy.py` для публичного CLI/облачного пути распространения ошибки. Исправление позитивных SDK-фикстур уже выполнено в Task 1.

- [x] Написать RED-проверки на оба пути неизвестного результата: timeout и повреждённый ответ после PostOrder оставляют тот же заранее заданный request ID в безопасном контексте ошибки и в ERROR-логе; для транспортного отказа cause сохранён. Логи приложения не выводят token/kwargs/SDK repr. Проверить распространение до рабочего CLI/облачного пути: ID остаётся видим оператору, поле исключения не теряется, run_sync не возвращает ложный Success.
- [x] Создавать ID локально после validate_intent, один раз до call_api; невалидный intent не вызывает фабрику/SDK. Добавить необязательный собственный контекст ошибки и передавать его в общий перевод ответа, без изменения SDK-независимого API методов executor.
- [x] При quota retry использовать тот же ID; после другой ошибки повторного PostOrder нет. Не приравнивать client request ID к broker order ID.
- [x] Проверить вызовы с инъекцией ID factory, точные аргументы/число вызовов, безопасную диагностику и обратную совместимость остальных OrderExecutionError.
- [x] Целевые проверки и полная свита GREEN до следующего шага; никаких live-запросов или диагностических сделок.

### Task 3: Приёмка и документация

**Файлы:** обновить `README.md`, `AGENTS.md`, при необходимости `docs/execution-adapter.md`; существующие архитектурные проверки только по изменённой границе.

- [x] Зафиксировать actual identity и request/broker ID distinction в текущем контракте. Не внедрять более широкий перенос reporting/errors из будущей issue #16.
- [x] Полный pytest, pylint для tracked/new Python, flake8 E9/F63/F7/F82, diff-check. Overall/diff coverage >=90%, стремиться к осмысленным100% изменённой границы без exclusions/pragma/изменений workflows.
- [x] Untracked тесты учесть в diff-проверке отдельно либо через diff-file; результат полного проекта не выдавать за sandbox/production acceptance.
- [x] Перед PR проверить, что изменённые фикстуры принадлежат этому контракту, расчётные модули и торговая арифметика не изменены.
- [x] Создать отдельный PR по issue #6 только после одобрения реализации. До проверки на проде issue не считать завершённой.

### Локальная проверка Task 3, 2026-10-06

Пользователь разрешил только локальную часть Task 3 в общей рабочей папке,
ветка `feature/architecture-broker-receipts`, после Task 1 `c76e3f8` и Task 2
`f65bad6`. Прочитаны полный план и AGENTS.md; перед изучением исходников
использован CodeGraph. README.md, AGENTS.md и docs/execution-adapter.md описывают
фактические поля ответа, строгий OrderExecutionError, разные request/broker ID,
неизменный request ID квотных попыток и безопасный ERROR с сохранением cause
и действующего жизненного цикла. Общий рефакторинг issue #16 не выполнялся.

Pylint обнаружил замечания в разрешённых файлах Task 1/2. Исправлены только
переносы двух строк логирования в autorepeater/order_execution.py и оформление
существующих проверок в test/test_broker_receipts.py/test/test_runtime_policy.py
(callable-форма pytest.raises для keyword-only аргумента и меньше локальных
переменных). Смысл проверок и торговое поведение сохранены; новые тесты ради
покрытия, зависимости, исключения/pragma и изменения workflows не добавлялись.

Финальные команды выполнялись с
`PATH=/tmp/autorepeater-install-check/bin:/usr/local/bin:/usr/bin:/bin`:

| Команда | Фактический результат |
| --- | --- |
| `pytest --cov --cov-report=term-missing --cov-report=xml -q` | 2395 passed, 18 warnings, 211.78s; 2915/2915 строк, 100.00% |
| `coverage report --fail-under=90` | PASS, 100.00%; execution.py 201/201 и order_execution.py 58/58, оба 100.00% |
| `diff-cover coverage.xml --compare-branch=origin/master --fail-under=90` | PASS, 34/34 изменённых исполняемых строк, 100%, missing=0 |
| `pylint $(git ls-files --cached --others --exclude-standard '*.py')` | PASS, все 73 tracked/new Python-файла, 10.00/10; writable cache через PYLINTHOME=/tmp/broker-receipts-task3-pylint-cache |
| `flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics` | PASS, 0 |
| `git diff --check` | PASS |

test/test_broker_receipts.py уже отслеживается Git. Список untracked Python
пуст; игнорируемых untracked runtime Python в autorepeater/, scripts/, main.py
и handler.py нет. Diff проверен относительно origin/master: runtime затронут
только в order_execution.py и конструкторе OrderExecutionError в execution.py.
Изменённые SDK-фикстуры, retry- и lifecycle-проверки принадлежат контракту
квитанций; расчётные модули и финансовая арифметика не менялись. Посторонние
аудиты, архитектурный план и его журнал сохранены без правок.

Публикация ожидает parent review и явного разрешения пользователя на push;
ответ на запрос разрешения ещё не получен. PR не создавался, push/GitHub/
merge/rebase/live API не выполнялись. Sandbox/production, облачная установка
и внешняя приёмка не выполнены и не считаются завершёнными. Последний пункт
Task 3 остаётся незавершённым; локальная работа останавливается на этом шаге.

### Завершение ревью и публикация, 2026-10-06

Пять независимых ревью выявили два замечания к одному пробелу в тестах:
не хватало сквозных polling-сценариев с корректным по форме, но несовпадающим
OrderState. В коммите `a1278ba` добавлены пять сценариев; рабочий код не менялся.
Повторное ревью не обнаружило критических или существенных дефектов.

Независимая финальная проверка: 2400 passed, 18 warnings; общее покрытие
100% (2915/2915), diff-покрытие 100% (35/35); pylint всех 73 Python-файлов
10.00/10; flake8 и git diff --check прошли. Эти результаты заменяют показатели
предыдущего локального прогона в таблице выше.

После явного разрешения пользователя ветка опубликована, создан
[PR #26](https://github.com/gorpsys/autorepeater/pull/26) с `Refs #6`.
Публикация Task 3 завершена. Автоматическое закрытие issue не запрашивается:
merge, проверка на проде и подтверждение пользователя ещё не выполнены.

## После PR

Merge требует отдельного решения пользователя. Затем санкционированная проверка на проде: штатные подтверждения содержат фактические UID/side/broker ID, а диагностика неизвестного результата сохраняет безопасный request ID. Не провоцировать неверные ответы или timeout реальными сделками: негативные случаи доказываются офлайн.

Записать PR, merge SHA, cloud version и подтверждение пользователя. Только после этого можно начинать планирование следующей задачи #7.

## Согласованный порядок

Пользователь подтвердил удаление преждевременных черновых правок кода и тестов с сохранением аудита, планов и issues. Черновик реализации убран; он не считается одобренным решением.

Сейчас подробно согласовывается только план issue #6. Код и тесты разрешены лишь после отдельного подтверждения реализации. Следующие подробные планы составляются после merge и подтверждения проверки предыдущего PR на проде.

Пользователь подтвердил четыре уточнения проверки плана: обновление SDK-фикстуры в первом шаге; строгие типы enum; `OrderExecutionError` для повреждённых ответов; видимость request ID в безопасном ERROR-логе и тест распространения контекста. Эти уточнения внесены в план, но не являются разрешением начинать реализацию.
