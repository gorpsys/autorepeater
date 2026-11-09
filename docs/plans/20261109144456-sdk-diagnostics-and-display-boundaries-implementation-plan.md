# Task 11: полная диагностика SDK и разделение display-слоёв — C13/C14/C16/C17

## Статус и полномочия

План подготовлен 09.11.2026 по master
`87775a3b485a6b993a66a83c0e9368c21bdbe0bb`, версия приложения `7.1.0`.
Тикет: [issue #16](https://github.com/gorpsys/autorepeater/issues/16).
**Решения владельца зафиксированы; блокирующих вопросов нет.**
Холодное ревью плана: **PASS**, 2 итерации по три независимых читателя.
Всего 1 замечание, 1 исправлено, 0 отклонено; вторая итерация — полный
проход без новых замечаний. Разбор приведён в журнале в конце плана.
Исходный этап только планирования завершён. По последующему поручению владельца
реализация выполняется в отдельном worktree
`/tmp/autorepeater-task11-sdk-diagnostics`, ветка `release/task11-sdk-diagnostics`,
от той же базы master. B1–B6 выполнены и проверены: действующий контракт
и версия 7.2.0 прошли финальную offline-проверку. Подробные результаты —
в блоках ниже и progress-task11-sdk-diagnostics.txt. До отдельного этапа review
не создавать code commit/push. Live E2E, production и credentials не затрагиваются.

Исходные рекомендации Task 11 по ограничению SDK-текста заменены более поздним
решением владельца. Не выполнять одновременно старый запрет SDK repr и
согласованное расширение диагностики.

## Решения владельца и цель

1. По C13/C14 дословно: «Принято в новой формулировке (в обратную сторону) -
   расширяем возможности логгирования и исключаем лишнюю фильтрацию».
2. Уровни вывода: **полные ошибки SDK/API в ERROR, успешные ответы в DEBUG**.
   Обычные логи не должны заполняться полными каталогами инструментов.
3. По C16: внешних потребителей старых display helpers нет; внутренние импорты
   можно обновить. Сохранить `reporting.postiton_to_string(client, position)`.
4. C16 и C17 также входят в задачу.
5. Торговое поведение не менять; ошибка количества в Sandbox остаётся отдельным
   обращением к поставщику. Допуск E2E сохраняется только в его harness.

Пользователь результата — владелец личного проекта при разборе локальных,
облачных и SDK-сбоев. Нужны исходная причина, сообщение сервера и доступный
tracking ID, а также возможность включить полные успешные SDK-ответы.
Данные — SDK response DTO, `RequestError.code/details/metadata`, traceback,
собственные Decimal/портфельные DTO и прежний человекочитаемый обзор счетов.
Новых БД, сервисов, API, workers или сетевых интеграций нет.

Выбран узкий вариант: общий диагностический wrapper одного SDK-вызова,
отдельный legacy display adapter и общий нормализатор двух существующих
адаптеров. Перехват всего gRPC-транспорта не нужен: он добавил бы работу с
исходящими metadata и иную границу ответственности. Массовая переделка всех
DTO ради удаления `diagnostic_text` противоречила бы новому решению владельца.

## Основания и актуальность

- [Общий план, Task 11](20261006160000-architecture-hardening-implementation-plan.md#task-11-полная-anti-corruption-граница-dto-диагностики-и-ошибок-sdk).
- [Отчёт актуальности](/tmp/autorepeater-task11-actuality-report.txt),
  [офлайн-пробы](/tmp/test_task11_audit.py): 17 пройденных probes и 1471
  затронутый штатный тест; это результаты аудита, а не реализация этого плана.
- [C13: создание SDK-текста](https://github.com/gorpsys/autorepeater/blob/87775a3b485a6b993a66a83c0e9368c21bdbe0bb/autorepeater/tinvest_strategy_data.py#L243)
  и [вывод stream event](https://github.com/gorpsys/autorepeater/blob/87775a3b485a6b993a66a83c0e9368c21bdbe0bb/autorepeater/reporting.py#L226).
  SDK repr действительно попадает в собственные строки/логи; владелец принял
  подробный диагностический текст. Реальная утечка токена не установлена.
- [C14: strategy wrapper](https://github.com/gorpsys/autorepeater/blob/87775a3b485a6b993a66a83c0e9368c21bdbe0bb/autorepeater/tinvest_strategy_data.py#L67)
  уже сохраняет текст и cause; execution/FX/security/order wrappers сохраняют
  cause, но собственные сообщения обобщены. Общий
  [call_api](https://github.com/gorpsys/autorepeater/blob/87775a3b485a6b993a66a83c0e9368c21bdbe0bb/autorepeater/tinvest_requests.py#L16)
  выводит только method/status/duration.
- В SDK `t-tech-investments==1.51.0` проверены `exceptions.py`, `_errors.py`
  и `logging.py`: `RequestError` создаётся из входящих gRPC code/details и
  Metadata с tracking ID, ratelimits и message. SDK не добавляет сюда
  автоматически credentials или kwargs вызова. Сам SDK также логирует
  server details и tracking ID. SDK logger не заменяет собственный маршрут
  диагностики приложения и его облачный JSON-формат.
- [C16: legacy I/O в reporting](https://github.com/gorpsys/autorepeater/blob/87775a3b485a6b993a66a83c0e9368c21bdbe0bb/autorepeater/reporting.py#L115),
  [SDK-shaped money helpers](https://github.com/gorpsys/autorepeater/blob/87775a3b485a6b993a66a83c0e9368c21bdbe0bb/autorepeater/money.py#L38)
  и [legacy protocols рядом с TargetPortfolio](https://github.com/gorpsys/autorepeater/blob/87775a3b485a6b993a66a83c0e9368c21bdbe0bb/autorepeater/portfolio.py#L8).
- [C17: приватный импорт](https://github.com/gorpsys/autorepeater/blob/87775a3b485a6b993a66a83c0e9368c21bdbe0bb/autorepeater/tinvest_execution_data.py#L14)
  остаётся. `tinvest_validation.py` проверяет scalar fields, но не units/nano.
  Новые FX/security/order конвертеры используют более строгий
  `MoneyAmount.from_units_nano`; их правила не унифицировать механически.
- Действующие инструкции: [контракт](../agents/contracts.md),
  [проверки](../agents/development.md), [SDK units](../execution-adapter.md),
  [Sandbox](../agents/sandbox.md), [версии](../../scripts/README.md#версии-и-выпуск).

Основной checkout при планировании находился на старой ветке
`docs/readme-versioning` с чужими изменениями. Его не переключать и не очищать.
Код изучен в `/home/kazakovaleksey/PythonProjects/autorepeater-task10`:
HEAD `266a94d`, Git tree `a5011aa40ca53bbe7d7948a71d0e7560dda76194` совпадает
с проверенным master. При планировании туда добавлялся только этот план;
реализация теперь ведётся в отдельном worktree, указанном выше.
Read-only сверка origin 09.11.2026 перед B6 подтвердила master `87775a3`
и опубликованный v7.1.0 на том же SHA; база версии не изменилась.
Если master изменится, сначала пересверить этот план;
не переносить изменения старой рабочей копии без проверки.

## Неприкосновенные свойства

- Типы/идентичность исключений, `__cause__`, программные ошибки и fail-stop
  сохраняются. Диагностика не становится условием торгового решения.
- Не менять receipt identity/count validation, request ID, quotas, retry,
  unary deadlines, stream reconnect, readiness, settlement или последовательность
  торговых эффектов. Не переотправлять заявку ради подробного лога.
- Не менять FX/cash/floors/ownership/Decimal-арифметику, `TargetPortfolio`
  и финансовые JSON-конфиги. Не переоткрывать принятое решение Task 5/C10.
- Число/порядок SDK-вызовов сохраняются. Успешные DTO логируются из уже
  полученного результата до его конверсии; новых RPC ради полей/ID нет.
- Не выводить credentials, клиент целиком, request kwargs или исходящие
  authorization metadata. Полный входящий ответ/ошибка SDK — согласованный
  диагностический материал; его поля не урезать новым allowlist или truncation.
- Не добавлять универсальную logging/redaction-платформу, межслойный SDK error DTO,
  транспортный interceptor, новое хранилище или зависимости.
- Не менять CI evidence protocol и фильтры машинных Sandbox-артефактов:
  они имеют отдельный контракт. Подробный эксплуатационный лог и evidence —
  разные выходы; существующий компактный RPC event продолжает работать.
- Сохранить атомарную сборку, manifest и воспроизводимость архива из PR #55.

## Архитектура и размещение

| Компонент | Ответственность |
| --- | --- |
| Новый `autorepeater/tinvest_diagnostics.py` | SDK-only `call_once`, вывод полной RequestError и DEBUG ответа; без quota/retry, доменной политики и просмотра клиента/kwargs. |
| `tinvest_requests.py` | Прежний quota loop/тайминг; каждая существующая попытка вызывает `call_once`. Компактные terminal/quota записи сохраняются. |
| `tinvest_strategy_data.py` | Прежняя конверсия/кеш; прямой stream отдельно использует те же diagnostic functions при открытии/чтении и для каждого уже полученного response. |
| Новый `autorepeater/tinvest_values.py` | Публичный `decimal_value(value, context)` — механический перенос существующего `_decimal_value`. Без SDK imports, I/O и logging; helper принадлежит слою SDK-конверсии. |
| `tinvest_execution_data.py` | Использует публичный helper; собственные nonnegative/domain проверки сохраняет. |
| Новый `autorepeater/display_data.py` | Frozen DTO для уже загруженного legacy display и собственная ошибка lookup. Только скаляры/Decimal/строки, без SDK-shaped вложений и методов чтения. |
| Новый `autorepeater/legacy_display.py` | Нынешний обзор счетов: SDK-shaped Protocol/read/conversion/metadata lookup и orchestration вывода. Все бывшие прямые reads вызываются через `call_once`, без нового quota retry. |
| `reporting.py` | Чистое форматирование/вывод собственных DTO. Единственное исключение — совместимый `postiton_to_string`, явно делегирующий legacy adapter. |
| `money.py` / `portfolio.py` | Decimal formatting и TargetPortfolio/validation соответственно. SDK-shaped чтения и конвертеры сюда не возвращаются. |
| `runner.py` | Прежний local browse перед mainflow вызывает новый legacy module. `run_sync` не обзаводится обзором всех счетов. |
| `logging_config.py` | Прежние local/JSON handlers; уровень собственного logger задаётся `LOG_LEVEL`. SDK не импортирует. |

SDK errors не переносятся в движок как raw objects или новые DTO.
Существующие нейтральные DataAccessError/ExecutionDataError/OrderExecutionError
сохраняют свой смысл; полная диагностика происходит один раз на SDK-границе,
исходный cause остаётся доступен выше. Не заменять domain reason на SDK-тип.

## Точные контракты диагностики — C13/C14

### Одиночный SDK-вызов

`call_once(method, **kwargs)` возвращает тот же объект, который вернул method.
Вызов ровно один. При `RequestError` логирует полный исходный error и делает
bare raise того же исключения. `TypeError`, `ValueError` и прочие ошибки
не переводит в RequestError и не повторяет.

Успех: DEBUG-запись `SDK response: method=<name> response=<полный repr>`.
Сначала проверить `logger.isEnabledFor(DEBUG)`; только затем получить защищённый
от ошибки самого repr полный текст и передать его в logger через `%s`.
При выключенном DEBUG repr не вычисляется. Отдельный proxy/serializer не нужен.
Никаких `asdict` клиента, сериализации kwargs или доступа к transport metadata.
Полученный SDK response не модифицировать. В DEBUG полные большие каталоги
разрешены осознанно; усечения или sampling в задаче нет.

Ошибка: ERROR-запись с методом, полным исходным `str(error)` и traceback
этого же исключения/цепочки через стандартный `exc_info`. Для облачного JSON
дополнительно сохранить `api_method`, `grpc_status`, `sdk_details`,
`sdk_metadata` как полное строковое представление metadata и `tracking_id`,
если он действительно есть в SDK Metadata. `None`/отсутствующий tracking ID
не заменять order ID или придуманным значением. Полный error text не обрезать.
Известный ответ с server message/ratelimit metadata должен сохраняться целиком.

Не создавать новый сериализатор произвольных SDK-объектов: стандартное
строковое представление и штатный traceback достаточны. Для объекта,
чей `__str__`/`__repr__` сам падает, сохранить исходный результат/исключение
и диагностический placeholder с именем типа и фактом ошибки форматирования;
не логировать исключение форматирования рекурсивно. Этот редкий fallback
не является фильтрацией нормального SDK-ответа. Проверить его отдельно.

### Quota и stream

`call_api` сохраняет текущие `measure_rpc_part`, perf-counter и read deadline.
Не-quota ошибка после `call_once` всё ещё даёт прежнюю компактную
`API request failed: method=... status=... elapsed_seconds=...` и пробрасывается.
Quota-попытка даёт полный ERROR от `call_once`, затем прежний WARNING и sleep;
число попыток, интервал 10 секунд/остаток бюджета и request ID прежние.
Получаются одна подробная запись на ошибочную попытку и прежние компактные
события. Не вводить mutable-маркеры на исключениях для дедупликации.
Таким образом, terminal failure имеет полный ERROR и прежний компактный ERROR;
quota failure — полный ERROR и прежний компактный WARNING. Это намеренно два
разных вида записи, а не повтор полной ошибки в каждом adapter wrapper.

Stream: при ошибке получения iterator и при RequestError из `next()` вывести
полную ошибку через тот же helper до прежнего `raise ... from error`.
Каждый успешно полученный PositionsStreamResponse вывести в DEBUG до конверсии;
не материализовать stream и не вычислять repr iterator как замену payload.
EOF не считать SDK-ошибкой, сохранять backoff и работу событий.

### Успешные ответы и существующий вывод

`diagnostic_text` PortfolioEntry/PositionEvent и его исходное содержимое сохраняются:
владелец не требует удаления строкового SDK-представления из DTO.
Существующие INFO/IMPORTANT финансовые факты, причины пропуска, native/RUB
цены и подтверждения сделок остаются. Полный diagnostic_text пропущенного
события переносится с IMPORTANT в DEBUG; краткая INFO-причина остаётся.
Для fallback-типов позиции полный SDK-text выводится в DEBUG, а на обычном
уровне — `Position: uid=... type=... quantity=... currency=... price=...`
из уже загруженных собственных полей с прежним format_decimal для чисел.
Расчётные formatter-ветки для share/ETF/currency и явной valuation сохраняют
числа и привычный вид. Полные обычные ответы уже доступны на SDK-границе.

DEBUG-успех не запускает дополнительные metadata/portfolio reads и не требует
ни одного поля, отсутствующего в реальном response. Tracking ID успешного
GetOrderState, которого DTO не содержит, новым RPC/interceptor не добывать.

### Включение DEBUG

Добавить единственную переменную `LOG_LEVEL`: отсутствие означает `INFO`,
допустимы `INFO` и `DEBUG` без учёта регистра, с удалением внешних пробелов.
Пустое/другое значение даёт понятный ValueError при настройке до создания Client.
Это уровень собственного `tinkoffBot`, а не root или всех сторонних библиотек.
Оба configure-функции выбирают его одинаково; повторный вызов не добавляет
handler и не заменяет существующий облачный JSON formatter.

Не менять назначение CLI `--debug`: оно остаётся dry-run, а не настройкой логов.
`LOG_LEVEL=DEBUG` сам по себе не включает и не выключает торговлю.
Расположение существующих configure-вызовов в этом PR не перестраивать:
composition-root задача C26 относится к Task 14.

## Разделение legacy display — C16

Новые DTO в `display_data.py`:

- `DisplayAccount(id: str, name: str)`.
- `DisplayPosition(uid: str | None, instrument_type: str, currency: str | None,
  current_price: Decimal | None, quantity: Decimal | None,
  instrument_label: str | None, diagnostic_text: str)`.
- `GetInstrumentException` для прежней ошибки неединственного lookup.

Все DTO frozen. Никаких `.current_price.units`, response/client или callable
в этих данных нет. Оригинальные currency case и строки имени/тикера сохраняются.
`None` допускается в display DTO только для сохранения прежнего ошибочного
входа до formatter; это не разрешение такой валюты в торговых DTO.
`instrument_label` нужен только для share/ETF: name/ticker получаются тем же
FindInstrument и форматируются чистой функцией после прежней первой конверсии
quantity, до чтения price; ровно один match, иначе та же GetInstrumentException.
Для остальных типов новых metadata lookup нет. Исходный SDK-text сохраняет
fallback-диагностику.

Уточнение B4 по точному legacy parity: совместимый wrapper для bond/unknown
раньше сразу возвращал `str(position)`, даже при `current_price=None` и
`quantity=None`. Поэтому raw-only DTO оставляет незагруженные uid/price/quantity
равными None, а не читает новые поля и не придумывает Decimal(0). Численные
formatters вызываются только с загруженными значениями. Готовая label сохраняет
также прежний TypeError для повреждённого name до чтения повреждённого price.
При browse fallback DEBUG выводится перед конверсией, compact INFO — после неё,
а отдельная повторная price/quantity-конверсия total остаётся после печати
позиции: поздняя ошибка quantize не удаляет прежний частичный вывод. Это узкое
уточнение DTO/проекции, без нового условия допуска, валидации или framework.

Legacy adapter переносит текущую последовательность
GetAccounts → для каждого счёта GetPortfolio → для каждой share/ETF FindInstrument.
Сохраняются порядок счетов/позиций, заголовки, вычисление total и отсутствие
торговых методов. Чтения, которые раньше выполнялись напрямую, используют
`call_once`, поэтому ошибка quota там по-прежнему не создаёт retry loop.

Внутри последовательности adapter передаёт в reporting уже готовые
DisplayAccount/DisplayPosition; reporting не читает клиент или порт.
Header печатается перед чтением портфеля, каждая позиция после её lookup,
total — после всех позиций: сохраняется полезный частичный вывод при ошибке.
Никакого общего предварительного чтения всех счетов/материализации вместо
этого порядка. Изменение самой необходимости обзора перед CLI trading — C27,
здесь он остаётся на прежнем месте.

Форматтеры в reporting используют Decimal values из DTO. Умножение
`price * quantity`, quantize `1e-9` каждой позиции до суммирования и порядок
суммы остаются как в legacy path; `format_decimal` и его `.0` не меняются.
Новые функции: `display_position_value(price: Decimal, quantity: Decimal)`,
`display_position_to_string(position)`, `print_display_position(position)`,
`print_display_account_header(account)`, `print_display_account_footer(total)`.
Они не знают units/nano и не вызывают API. Для unsupported типов строковый
fallback сохраняется в `display_position_to_string`; print-функция соблюдает
согласованные DEBUG/обычный summary уровни.

Уточнение после comprehensive review: `display_position_value` получает два
готовых Decimal напрямую, поскольку для округления произведения DTO не нужен.
`legacy_display.print_position` возвращает None; total повторно читает price,
затем quantity после печати и передаёт их formatter без копирования DTO.
Это внутреннее упрощение сигнатуры, без изменения арифметики/ошибок/SDK calls.

Совместимость: `reporting.postiton_to_string(client, position)` остаётся
с прежней сигнатурой, результатом для корректного SDK input и lookup behavior.
Это один явно обозначенный legacy wrapper: локально импортирует конвертер
из legacy_display и передаёт готовый DTO чистому formatter. Исключение lookup
остаётся доступно как `reporting.GetInstrumentException`, импортированное из
чистого display_data. Для этого единственного wrapper архитектурный тест
разрешает точный legacy import; не разрешать SDK/I/O всем функциям reporting.
Остальные старые SDK-shaped helpers и imports меняются во внутренних
потребителях/тестах, без пакета совместимых фасадов на каждый helper.

`money.py` оставляет `format_decimal`/`format_decimal_map`; SDK-shaped
money/currency/position helpers удаляются после переноса их нужных функций
в adapter conversion и pure display formatting. Положительные/отрицательные,
nano и крупные значения из прежних тестов переносятся в новые тесты, а не
теряются вместе с удалёнными exports. `portfolio.py` оставляет TargetPortfolio
и validate_target, без client/read protocols/get_portfolio.

Новых проверок допустимости currency в legacy display не добавлять.
Перенести прежний acceptance/error contract: case, пустые и нестандартные
строки сохраняются буквально; там, где прежнее форматирование с `None`
вызывало TypeError, оно остаётся ошибкой того же класса после тех же чтений.
Не добавлять currency_code, нормализацию, fallback в RUB или новый ValueError.
Для unsupported типов прежний строковый fallback не становится валютным
валидатором. Проверки валюты торговой valuation/execution остаются прежними.

Это существенно для границы задачи: обзор всех счетов выполняется до
mainflow. Ужесточение display на постороннем счёте могло бы создать новое
условие отказа торговли выбранного счёта. Поэтому сохраняются также прежние
правила числовой конверсии legacy: локальный `_legacy_decimal(value)` в
legacy_display переносит выражение `Decimal(units) + Decimal(nano) / 1e9`
из прежних money helpers без новых checks. Не заменять его более строгим
strategy-helper только ради унификации. Тесты должны сопоставлять исходную
ошибку/успех, порядок SDK calls и допуск к mainflow до/после переноса.

## Общая units/nano-конверсия — C17

Публичный `tinvest_values.decimal_value(value, context)` переносит нынешние
`NANO_FACTOR` и `_decimal_value` без изменения арифметики, проверок и сообщений.
Strategy adapter и execution `_quotation` импортируют его по публичному имени.
Execution сохраняет дополнительный nonnegative check. Приватный экспорт из
tinvest_strategy_data не оставлять как новый межадаптерный compatibility API:
внешних потребителей нет; внутренние тесты обновить.

Не заменять этот helper на MoneyAmount.from_units_nano и не заменять новые
строгие MoneyAmount paths более терпимым helper. У них уже разные контракты
для повреждённых значений; C17 закрывает приватную зависимость, а не меняет
политику допуска входных данных. В частности, нынешнее принятие некоторого
malformed nano strategy-helper и его Decimal-context sensitivity остаются.
Legacy display сохраняет свой converter contract отдельной локальной функцией,
как описано в C16; новый общий helper обязателен только для двух прежних
strategy/execution потребителей. После получения Decimal расчёт legacy
остаётся в прежнем порядке. Валидация валюты не добавляется ни в C16,
ни в безвалютный units/nano helper.

## Подход к разработке и проверки

TDD RED → GREEN; каждый блок доводится до конца, включая обновление старых
ожиданий и проверки, до следующего блока. Порты/SDK boundaries —
`autospec(spec_set=True)`; SDK Services —
`autospec(inspect.unwrap(Service), instance=True, spec_set=True)` с настоящими
DTO и Metadata. Client/сеть в unit-тестах не создавать.

Не выдавать синтетическую строку в details за реальный секрет поставщика.
В тестах разделить server diagnostic marker, который теперь обязан попасть
в лог, и credential marker только в request kwargs/клиенте, которого в логе
быть не должно. Не удалять проверку отсутствия реальных credential sources
ради разрешения подробного текста ответа.

Стандартные полные проверки: `make test`, `make lint`, `make coverage`,
`git diff --check`; точечные pytest — по docs/agents/development.md.
Порог overall/diff >=90%, исключения/пороги не менять. Для нового Python файла
проверить включение untracked diff по действующей инструкции без фиктивного
staging. Generated-изменения штатных целей сохранять в рамках задачи,
не откатывать destructive-командами. Зависимости/locks не обновляются.

План — контрольный список выполняемой реализации. Отмечать `[x]` только после
фактического выполнения и тестов; новые существенные решения сначала
вносить в план. Не откладывать design choice на момент написания кода.

## Шаги реализации

### B1. Вынести существующий units/nano helper

**Files:**
- Create: `autorepeater/tinvest_values.py`, `test/test_tinvest_values.py`.
- Modify: `autorepeater/tinvest_strategy_data.py`, `autorepeater/tinvest_execution_data.py`,
  `test/test_tinvest_strategy_data.py`, `test/test_tinvest_execution_data.py`.

- [x] RED: запретить импорт приватного helper соседнего адаптера и закрепить
  публичный converter contract, включая точные сообщения/causes.
- [x] Перенести helper и обновить оба импорта без изменения операций Decimal.
- [x] Tests success: signed values, zero, nano=1/999999999, int64 extremes,
  точные результаты и прежние nonnegative execution checks.
- [x] Tests error/compatibility: None/bool/nonfinite/malformed parts,
  прежнее принятие sign/range cases; строгие MoneyAmount paths не ослаблены.
- [x] GREEN затронутых tests и полный `make test` до B2.

### B2. Добавить диагностику SDK и управляемый DEBUG

**Files:**
- Create: `autorepeater/tinvest_diagnostics.py`, `test/test_tinvest_diagnostics.py`.
- Modify: `autorepeater/logging_config.py`, `test/test_execution_diagnostics.py`.

- [x] RED: полные RequestError details/Metadata/traceback на ERROR; полный
  response на DEBUG; `LOG_LEVEL` и отсутствие repr при default INFO.
- [x] Реализовать diagnostic functions и `call_once` с тем же return/exception
  object и ровно одним вызовом, без retry/чтения kwargs/client metadata.
- [x] Добавить `LOG_LEVEL` с согласованным default/валидацией и повторной
  настройкой без дублирования handlers/смены облачного formatter.
- [x] Tests success: реальные SDK DTO/Metadata, все входящие поля,
  INFO vs DEBUG, lazy repr, local stderr и облачный валидный JSON в свежем
  процессе, одинаковая конфигурация CLI/cloud, отсутствие third-party DEBUG.
- [x] Tests errors: None tracking, multiline/unicode/большой server text без
  усечения, fallback при падающем repr/str, programming errors и identity/cause,
  credential marker в kwargs не прочитан/не выведен.
- [x] GREEN новых/затронутых tests и полный `make test` до B3.

### B3. Подключить диагностику unary/stream и уровни C13

**Files:**
- Modify: `autorepeater/tinvest_requests.py`, `autorepeater/tinvest_strategy_data.py`,
  `autorepeater/reporting.py`.
- Tests: `test/test_tinvest_requests.py`, `test/test_tinvest_strategy_data.py`,
  `test/test_tinvest_execution_data.py`, `test/test_tinvest_fx_data.py`,
  `test/test_tinvest_security_data.py`, `test/test_broker_receipts.py`,
  `test/test_execution_diagnostics.py`, `test/test_autorepeater.py`,
  `test/test_sandbox_support.py`, `test/test_repeater_lifecycle.py`,
  `test/test_runtime_policy.py`, `test/test_book_timing_diagnostics.py`.

Интеграционная корректировка B3: первый полный offline-прогон выявил ещё
пять старых запретов server text в runtime-policy и quota timing tests.
Эти два test-файла добавлены в область B3 для приведения ожиданий к принятой
политике ERROR diagnostics. Порядок/число эффектов, stage totals и отдельный
запрет вывода исходящих credentials сохраняются; B2 helpers не меняются.

- [x] RED: полный RequestError наблюдаем из strategy/execution/FX/security/order
  путей и stream opening/iteration; DEBUG response до конверсии.
- [x] Подключить `call_once` внутри прежнего quota loop; stream подключить
  явно, без изменения его ленивости, cause, EOF/reconnect и trade behavior.
- [x] Перенести полный raw diagnostic_text события/fallback-позиции на DEBUG,
  сохранив компактные обычные сообщения и исходные DTO diagnostic strings.
- [x] Обновить старые тесты, запрещавшие весь SDK error text: server marker
  теперь ожидается; запрет вывода kwargs/credentials остаётся отдельным тестом.
- [x] Tests error/edge: quota → success/другая ошибка, deadline после quota,
  один request ID, один вызов на попытку, неизменный cause, отсутствие дубля
  полного лога на каждой обёртке, stream после одного события падает как прежде.
- [x] Tests evidence: прежние компактные RPC events по-прежнему распознаются;
  полные SDK records не добавляются в machine evidence. Пороги/парсер
  артефактов и допуск Sandbox lots не ослаблять.
- [x] GREEN затронутых tests и полный `make test` до B4.

### B4. Разделить legacy reads и чистое отображение

**Files:**
- Create: `autorepeater/display_data.py`, `autorepeater/legacy_display.py`,
  `test/test_legacy_display.py`, `test/test_display_reporting.py`.
- Modify: `autorepeater/reporting.py`, `autorepeater/money.py`,
  `autorepeater/portfolio.py`, `autorepeater/runner.py`.
- Tests: `test/test_autorepeater.py`, `test/test_strategy_contract.py`,
  `test/test_strategy_selection.py`, `test/test_execution_diagnostics.py`,
  `test/test_grpc_deadline.py`, `test/test_runtime_policy.py`,
  `test/test_sandbox_wiring.py`, `test/test_yandex_archive.py`.

- [x] RED: pure formatter работает на собственных DTO при запрещённом I/O;
  legacy reader сохраняет точную последовательность SDK calls и прежний вывод.
- [x] Добавить frozen display DTO и SDK-shaped adapter conversion; formatter
  получает только готовые Decimal/строки. Перенести обзор счетов в legacy module.
- [x] Удалить SDK-shaped helpers/read protocols из money/portfolio/reporting,
  обновить внутренние импорты и wiring Runner.run; `run_sync` оставить без обзора.
- [x] Сохранить единственный совместимый `reporting.postiton_to_string`
  и lookup exception; wrapper делегирует, не копирует конверсию/форматирование.
- [x] Tests success: share/ETF/currency/bond/unknown, empty accounts/portfolio,
  исходный порядок/число lookup, прежний header/partial output/total и формат
  positive/negative/nano/large values, отсутствие post_order/cancel.
- [x] Tests errors/parity: lookup 0/>1, первый/поздний RPC failure с полным
  логом; для None/empty/нестандартной currency и повреждённых числовых полей
  сохраняются прежние успех/тип ошибки и порядок чтений, без выдуманного RUB.
  Готовые DTO formatter не читает из клиента; совместимый wrapper прежний.
- [x] На CLI-фикстуре с исправным выбранным счётом и посторонним счётом
  сравнить допуск к mainflow до/после рефакторинга для тех же legacy inputs:
  никаких новых отказов из-за currency или более строгого scalar helper.
- [x] GREEN новых/затронутых tests и полный `make test` до B5.

### B5. Закрепить границы и сквозную приёмку

**Files:**
- Modify: `test/test_architecture.py`, `test/test_strategy_contract.py`,
  `test/test_fx_reporting.py`, `test/test_execution_diagnostics.py`.

- [x] Классифицировать новые модули как конкретные adapter/pure components;
  не вводить общий default-deny framework из Task 15.
- [x] Запретить I/O/SDK-shaped fields для всех новых pure formatter entrypoints,
  приватные межадаптерные импорты и перенос reader обратно в portfolio/money.
- [x] Для reporting разрешить ровно compatibility import внутри
  `postiton_to_string`; negative fixtures должны отвергать тот же импорт
  в любой другой renderer и прямой SDK import в wrapper.
- [x] Tests success/error: SDK-blocked импорт pure моделей/reporting,
  архитектурные negative fixtures, штатный Runner run/run_sync на строгих
  Services mocks с прежним количеством calls и выдаваемых торговых intents.
- [x] Выполнить `make test`, `make lint`, `make coverage`, `git diff --check`;
  проверить >=90% overall/diff и осмысленные branches новых helpers.
- [x] Сверить каждый Cxx с матрицей ниже; до документации все проверки GREEN.

Результат B5: изменены только четыре предусмотренных test-модуля, этот план
и progress. Runtime-исправлений предыдущих блоков не потребовалось.
Фиксированные группы компонентов расширяют существующие AST-проверки;
общий default-deny framework не вводится. Проверены все новые presentation
entrypoints, включая `display_instrument_label`, точный локальный import
compatibility-wrapper и запрет SDK import даже внутри него, приватные
межадаптерные импорты с алиасами, DTO-поля и возврат reader в money/portfolio.
SDK/I/O-blocked subprocess также строит и выводит native FX-план.

Сверка матрицы: C13 — INFO/DEBUG не меняют calls/intents; C14 — ошибки обоих
реальных Runner entrypoints сохраняют full details/Metadata/tracking/traceback
и cause, останавливаясь до intent; C16 — все pure функции и собственные DTO
проходят guards, прежние 420 legacy parity cases сохраняются; C17 — запрещены
приватные межадаптерные зависимости, public converter и строгие MoneyAmount
paths проходят полный набор. Runner сохраняет BUY 16 лотов / 48 единиц,
точные чтения назначения (6 для трёх streaming-проходов, 2 для одного sync)
и один browse/lookup только в `run`; дополнительные RPC для DEBUG отсутствуют.
Эксплуатационные local/cloud LOG_LEVEL и dry-run checks также GREEN.

Проверки с `PYTHON=/home/kazakovaleksey/PythonProjects/autorepeater-c32/.venv/bin/python`:
targeted pytest четырёх модулей — 323 passed; полный `make test` —
6973 passed, 18 warnings (318.29s); `make lint` — Pylint 10.00/10,
Flake8 fatal/full 0, Mypy 29 files; `make coverage` — 6973 passed,
19 warnings (1243.18s), overall 99.44% (10410 statements, 58 missing),
штатный diff 100% (56 lines, 0 missing). Отдельный `--diff-file`, собранный
без staging из tracked diff от origin/master и четырёх новых runtime-модулей,
даёт 100% (186 lines, 0 missing). Все новые runtime-модули покрыты на 100%:
display_data 16, legacy_display 58, tinvest_diagnostics 34, tinvest_values 22
исполняемых строки. Дополнительное предупреждение coverage — недоступный
C tracer; Python tracer завершил полный прогон. Порог/область покрытия,
зависимости и exclusions не менялись. `git diff --check` GREEN.
На момент завершения B5 live E2E/deploy не выполнялись, B6 был следующим блоком.

### B6. Обновить контракт и выпуск

**Files:**
- Modify: `docs/agents/contracts.md`, `docs/agents/development.md`,
  `docs/execution-adapter.md`, `scripts/README.md`, `README.md`,
  `CHANGELOG.md`, `autorepeater/__init__.py`.
- Modify: этот файл плана и `docs/agents/history.md` для обоснования решения.
- Modify: `docs/agents/README.md` для актуального диапазона ID истории H01–H45.
- Tests: существующие `test/test_release_version.py`, `test/test_yandex_archive.py`.

- [x] Заменить старую рекомендацию об исключении SDK text принятой политикой:
  полные ошибки/DEBUG responses, сохранённый cause и отсутствие kwargs/credentials.
  Зафиксировать `LOG_LEVEL`, независимость от dry-run и compatibility wrapper.
- [x] Документировать границы display и helper и сохранение прежнего legacy
  acceptance/error contract; не описывать новый финансовый функционал.
- [x] При неизменной базе `7.1.0` назначить `7.2.0`: добавляется пользовательское
  включение полной DEBUG-диагностики, совместимость известного public API
  сохранена. Если база/теги уже изменились — обновить план и согласовать новую
  версию до правок, не обходить Check release version.
- [x] Обновить краткий CHANGELOG; исторические причины поместить в history,
  README оставить описанием текущей функции, без журнала работ агента.
- [x] Проверить release/version и реальную archive parity после переноса
  модулей штатными tests; повторить lint/coverage после изменений исполняемого
  кода/версии, сохранить generated outputs, если штатные цели их создали.
- [x] Зафиксировать результаты и фактический объём в этом плане; не помечать
  live E2E/deploy пройденными по одним offline-тестам.

Результат B6: обновлены все предусмотренные действующие документы, краткий
CHANGELOG и __version__=7.2.0. История H43–H45 объясняет принятое расширение
диагностики, точное legacy parity и отдельные converter contracts; индекс
docs/agents/README.md дополнительно обновлён до H01–H45 для согласованности.
Полные входящие ошибки разрешены на ERROR, полные успешные ответы на DEBUG,
LOG_LEVEL независим от --debug/dry-run; credentials/client/kwargs и исходящие
authorization metadata не выводятся. Зафиксированы optional raw-only DTO,
instrument_label и прежний порядок числового чтения/ошибок, отдельная конверсия
total после печати и публичный helper без ужесточения прежнего допуска.
Внешних изменений, нового финансового функционала или зависимостей нет.

Release RED: прежняя 7.1.0 отклонена относительно полного SHA базы как
недостаточное повышение. После правки штатный scripts.release_version check
с базой `87775a3b485a6b993a66a83c0e9368c21bdbe0bb` — GREEN, Release 7.2.0.
Первый вызов с symbolic ref вместо SHA также был отклонён; после исправления
аргумента проверялось именно правило версии. Targeted существующих
test_release_version/test_yandex_archive, включая реальные archive builds,
extraction/parity: 109 passed, 1 warning (29.00s).

Финальный make lint — GREEN, Pylint 10.00/10, Flake8 fatal/full 0, Mypy 29 files.
Финальный make coverage — exit 0, полный набор 6973 tests, без failures;
18 штатных warnings. Полный test count дополнительно подтверждён collect-only
(6973, 1.75s), поскольку терминальный вывод середины итогового coverage-отчёта
с точными pytest seconds был усечён. Прогон занял примерно 5 минут; ради timing
успешный полный прогон не повторялся. Overall 99.44%: 10410 statements,
10352 covered, 58 missing; штатный diff 100% (57 lines, 0 missing).
Отдельный diff-cover coverage.xml --diff-file=/tmp/task11-b6-all-source.diff
с явной базой origin/master — 100% (187 lines, 0 missing), включая четыре
untracked runtime-модуля без staging и новую строку версии. Новые модули все
100%: display_data 16, legacy_display 58, tinvest_diagnostics 34, tinvest_values 22.

Все команды используют прежний c32 PYTHON. Допустимый native core
COVERAGE_CORE=sysmon выбран после read-only проверки coverage 7.16.2 на CPython
3.12.3: SysMonitor поддержан, .coveragerc/branch=False/source/omit/пороги прежние,
calibration не исключалась. XML facts filename/line/covered до/после идентичны
B5 Python tracer, SHA-256
`288c986b2fea4ae6348db06f70fd7e56761ae515fd80bbb27a2ba80200af2680`;
branch counts по-прежнему 0/0. Это смена штатного core без изменения измерения,
не установка/обновление пакета; предупреждения о недоступном CTracer нет.
Сохранены generated .coverage/coverage.xml и отдельный JSON diff-отчёт
/tmp/task11-b6-all-source-coverage.json; archive tests собирали временные ZIP,
новый worktree build/yandex-function.zip не создавался. Generated source,
dependencies/locks/vendor, Make/thresholds/exclusions не менялись.
git diff --check GREEN. B6 завершён; commit/stage/push, live E2E, broker RPC,
production deployment и его приёмка не выполнялись. Следующий этап — review.

## Comprehensive review реализации — исправление итерации 1

Quality/implementation/testing/smells: замечаний нет. Единственное замечание
simplification подтверждено по исходному коду: total копировал DisplayPosition
через replace ради двух повторно прочитанных Decimal. Копирование и возврат
DTO из print_position удалены; display_position_value принимает price/quantity.
Уточнение внутренней сигнатуры внесено в C16 выше. Порядок print → price read →
quantity read → multiply/quantize сохраняется, SDK-вызовы и ошибки не меняются.

TDD RED: 6 ожидаемых TypeError новой сигнатуры, 459 passed. Обновлены внутренние
test calls и SDK/I/O-blocked FX subprocess; negative architecture fixture
по-прежнему запрещает units. Добавлены пять регрессий повторного чтения total
после печати для currency/share/ETF/bond/unknown, прежние 420 exact parity cases
не менялись. Итоговый targeted семи модулей: 1519 passed, 18 warnings (51.50s).

Повторные штатные проверки с прежним c32 PYTHON: make lint — Pylint 10.00/10,
Flake8 fatal/full 0, Mypy 29 files; COVERAGE_CORE=sysmon make coverage —
6978 passed, 18 warnings (306.25s), overall 99.44% (10408 statements, 58 missing),
штатный diff 100% (57 lines). Отдельный combined tracked + четыре untracked
runtime-модуля --diff-file без staging — 100% (185 lines, 0 missing),
/tmp/task11-fixer-all-source.diff и /tmp/task11-fixer-all-source-coverage.json.
Новые runtime-модули все 100%; legacy_display теперь 56 statements вместо 58.
git diff --check GREEN, index пуст. Coverage configuration/пороги/зависимости
не менялись; отдельный полный make test не дублировал coverage-прогон.
Commit/push, live E2E, broker RPC, production и другие checkout не затрагивались.
Следующий этап — независимая critical re-check исправления.

## Матрица приёмки

| Пункт | Итоговая проверка |
| --- | --- |
| C13, новая формулировка | Полные SDK responses/events доступны в DEBUG до конверсии, их нет как полного payload в default INFO. Нужные короткие финансовые факты сохраняются. Diagnostic strings не удалены ради независимости repr. |
| C14, новая формулировка | На каждой реальной SDK error attempt есть ERROR с исходными details/metadata/traceback/доступным tracking ID. Wrapper не скрывает первопричину из собственного маршрута логов, cause и fail-stop прежние. |
| C16 | Чистые presentation functions не знают клиента/units/nano и не делают I/O; legacy read/conversion выделены; TargetPortfolio/format_decimal остаются чистыми. Сохранены acceptance/error contract и допуск к CLI mainflow; единственный compatibility wrapper отдельно проверен. |
| C17 | Оба прежних адаптера используют public converter своего SDK-слоя; приватного импорта соседа нет, valid results/старые checks сохранены, MoneyAmount не ослаблен. |
| Торговое поведение | Те же calls/order IDs/правила/порядок effects, те же Decimal results, без production fallback для ошибочных Sandbox lots и без новых broker reads ради лога. |
| Эксплуатация | Default INFO и opt-in DEBUG работают в local/JSON cloud без дублирующихся handlers; `--debug` остаётся dry-run. |

## После реализации — внешние действия

Отдельная ветка реализации для issue #16 уже создана от указанного master.
После review и итоговой проверки остаются один содержательный code/test/docs
commit и push/PR по поручению владельца; промежуточные блоки не коммитятся.
Зависимостей план не добавляет.
Не коммитить токены/сертификаты/логи с credentials. Исследовательский отчёт и
случайные untracked материалы не добавлять автоматически. План не переносить
в другой каталог без принятой в проекте причины.

Штатные Lint/Offline checks/Check release version должны пройти. Полный Sandbox
E2E и production deployment — только после отдельного разрешения владельца
для этого PR, по штатному workflow для конкретного SHA и торговому окну.
Успешный прошлый run PR #55 не является приёмкой нового SHA. Evidence protocol
и допуск ошибочного Sandbox lots остаются прежними.

При разрешённой выкатке подтвердить ACTIVE и runtime settings read-only.
Для диагностической приёмки достаточно сравнить default INFO и разрешённый
DEBUG на штатных чтениях/контролируемых offline ошибках; не создавать реальную
неизвестную заявку и не запускать дополнительные сделки ради server error.
Не включать DEBUG в production автоматически самим PR или этим планом.
До следующей группы требуется отдельное подтверждение приёмки владельцем.

## Вопросы и закрытые решения

Блокирующих вопросов нет. Уровни вывода и отсутствие внешних display consumers
подтверждены владельцем в этой сессии. Строгость C17 сохраняется как механический
рефакторинг: отдельного согласования ужесточения strategy-path не требуется,
поскольку его в этом плане нет. Legacy conversion/валютные inputs также
переносятся без ужесточения; ни новых display отказов, ни изменений допуска
к CLI mainflow план не вводит.

Неактуальны для этого плана: прежнее требование удалить SDK repr/отсечь
server details как главную цель C13/C14; предположение, что сохранение cause
ещё не реализовано; предположение, что tinvest_validation уже устранил C17;
массовая совместимость всех старых display imports при отсутствии потребителей.

## Журнал холодного ревью плана

Единица учёта — отдельное замечание читателя. PASS без замечаний добавляет
нулевое число; одинаковые замечания разных читателей получают отдельные ID.
Журнал фиксирует разбор, а не добавляет новые требования поверх текущего текста.

### Итерация 1

Читатели A и C: непротиворечив / полон / решения конкретны, PASS без замечаний.
Читатель B: непротиворечивость и полнота нарушены одним пунктом, остальные
решения в основном конкретны.

- **R1-B1 — исправлено.** Новая currency-валидация в обзоре всех счетов до
  mainflow противоречила обещанию не менять торговое поведение. Ужесточение
  удалено; сохранён прежний legacy acceptance/error contract, включая
  числовую конверсию, и добавлена parity-проверка допуска к mainflow при
  постороннем счёте. Новый отказ на display не объявляется изолированным от
  запуска торговли. Количество: 1 замечание, 1 исправлено, 0 отклонено.

### Итерация 2 — финальный полный проход

Новая независимая тройка A/B/C получила только текущий текст плана,
прочитала его целиком и ответила на все три вопроса. Все три читателя:
план непротиворечив, полон в заявленной области, решения конкретны;
неразрешённого откладывания решений на реализацию нет. **PASS без новых
замечаний** у каждого. Количество новых замечаний: 0.

Итог: 2 итерации, 1 замечание, 1 исправлено, 0 отклонено, **PASS**.
Счётчики сходятся: 1 + 0 = 1. Замечаний, оставленных без разбора, нет.
