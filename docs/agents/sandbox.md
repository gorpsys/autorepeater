# Live Sandbox E2E

Действующие требования к live-тестам, запуску, очистке и свидетельствам.
Практические команды — в [техническом README](../../scripts/README.md#песочница-локально)
и [инструкции GitHub](../../scripts/README.md#песочница-в-github).
Обоснования — в [истории находок](history.md#разработка-и-приёмка).

Sandbox-workflow не подписан на pull_request: открытие/обновление PR не
создаёт автоматический прогон или красный marker. Отсутствие обязательного
Sandbox E2E для нового head SHA блокирует merge через защиту master.
Live sandbox/GitHub CI отдельный санкционированный запуск; локальный pytest
не закрывает их. SANDBOX_TOKEN передавать только через окружение live-задания,
без production fallback. Sandbox E2E проверяет конкретный head SHA PR;
новый SHA требует полного повторного прогона. Fork/ошибка/отмена/неполный
прогон не дают success. GitHub запуски PR/master сериализуются вместе с
cleanup; локальный запуск при общей песочнице не пересекается с GitHub.
Дамп/JUnit содержат только безопасные проверенные факты.
История bootstrap PR #28 сохранена в [истории версий](../history/versioning.md#bootstrap-sandbox-e2e-2026-10-08).
Ручной запуск через Actions > Sandbox live E2E > Run workflow
(ref master, pr_number с номером открытого PR). В candidate 5.3.0 поле
evidence_protocol по умолчанию retry-v3; retry-v2 доступен явно.
Trusted prepare проверяет allowlist до live и фиксирует единый output для
producer и независимого verifier. Неизвестный выбор, включая retry-v1,
отклоняется; совместимость legacy v1 в Python helpers сохраняется.
После merge 5.3.0 master push выбирает v3 независимо от полей payload.
Обычный ручной запуск и автоматический master push одобрения не требуют.
Live hook до fixture пишет flush-прогресс: attempt N [index/selected_count]
и node ID. Повторы считают только выбранный subset; прогресс не evidence.
Локально вывод в реальном времени включается E2E_ARGS="-s" (CI уже использует -s).
Супервизор запускает scripts.sandbox_retry в одной группе процессов/run ID
под общей блокировкой и общим E2E_TIMEOUT. До трёх дополнительных попыток
только failed cases при DEADLINE_EXCEEDED/UNAVAILABLE; пауза 10s в том же
бюджете времени. Assertions/auth/закрытые торги/INTERNAL/collection/teardown/
cleanup/отмена/общий тайм-аут не повторять. Любая постоянная ошибка стоп.
Каждая попытка свежий pytest/счета, предыдущая очистка обязательна. После
входа в сессию искать неизвестные созданные ID только по текущему run ID;
ошибка первого read-only preflight допускает пустую cleanup без нового RPC.
Cleanup доказывать структурными facts, не видимостью stdout при capture.
retry-v2: collection.json полного набора проверяемого SHA создаётся отдельным
collect-only процессом до live fixtures, без inherited pytest filters/plugins.
Dirty source, deselection, пустая коллекция и collection errors запрещены.
Исходные attempts/NN/report.json+junit.xml+cleanup.log и дампы, retry.json
и итоговый уникальный junit.xml сохраняются. GitHub независимо выводит
законность всех попыток/итогов из originals: первая попытка содержит ровно
полную коллекцию, повторные — только допустимый failed subset. SHA каждой
попытки совпадает с collection и SHA, зафиксированным trusted workflow.
Skipped/xfail/xpass, лишние/повторные/недостающие cases, ошибки teardown,
collection или cleanup не дают success. Passed count — только диагностика.
В протоколе v2 инициатор задаёт E2E_EVIDENCE_PROTOCOL=retry-v2
и E2E_TESTED_SHA, trusted verifier — --evidence-format retry-v2 с --collection
и --sha. Отсутствие выбора сохраняет retry-v1 и его фиксированный набор 17
сценариев; в v1 report нет sha и новых полей. Это позволяет проверить сам
инфраструктурный PR действующим master workflow без обхода защиты.
После мержа новый trusted master включает v2; расширенные наборы требуют v2.
Подготовленный `retry-v3` сохраняет независимую полную коллекцию и точный SHA.
Он выбирается только инициатором через E2E_EVIDENCE_PROTOCOL=retry-v3 и trusted
verifier --evidence-format retry-v3, не по полям недоверенного артефакта.
Поддержка v3 и auth-fix выпущены в master 1b015c6 (5.2.2, PR #42).
Activation PR #43 проверен и вмержен в b4044a0 (5.2.3): trusted master
принимает evidence_protocol=retry-v3 для полного запуска PR #40 с ref master.
Его default и master push остаются v2. Candidate 5.3.0 сохраняет future default
v3 для guard 90s и использует тот же validated output в producer и verifier.
Candidate workflow PR сам по себе не переключает trusted dispatcher master.
Старый master 0492181 отвергает v3. Не проверять evidence кодом PR.
V3 повторяет только eligible failed cases даже при постоянных ошибках других
сценариев. Их результаты сохраняются в итоговом JUnit и запрещают общий success;
пройденные и постоянно упавшие сценарии не запускаются повторно.
Кроме прежних transport timeouts, допустим только market_time_stale:
исключение из известной загруженной точки валидации, совпадающие UID,
aware market/observed timestamps и возраст строго больше 90 секунд.
Verifier сам проверяет возраст и идентичность; ValueError по тексту и логи
не дают retry. Классификатор не импортирует будущие валютные модули приложения.
Future/missing/naive/invalid timestamps, UID mismatch и assertions не повторяются.
Auth останавливает все повторы. Неполный auth-разбор тоже запрещает все повторы:
цикл или остаток raw-цепочки после восьми исключений, либо восемь и более
сохранённых SandboxFailure.api_facts (окончание цепочки не доказано).
Collection/teardown/cleanup/cancellation/deadline
и ошибки протокола сохраняют прежние стоп-проверки. Лимит — те же три
дополнительных процесса с общим бюджетом и подтверждённой очисткой.
Local -k в legacy режиме допускается, не даёт full-suite acceptance.
Linux /proc проверяет остановку всей группы перед аварийной очисткой;
неподтверждённая остановка запрещает cleanup. Raw run.log не публиковать.
Диагностика отказа выбора инструмента выводится одной строкой stdout:
query, заданные ticker/type/currency, исходное число кандидатов, число
прошедших отбор и причины отсева. Не более 20 записей UID/ticker/
class/kind/currency/API flag; остальные отражены счётчиком omitted_count.
Отсутствующие, невалидные и ещё не загруженные поля различаются; имена,
SDK repr и тексты внешних исключений не выводятся. Ожидаемые type/currency
служат только диагностике, preflight проверяет их отдельно.
Дополнительный диагностический CNY-кейс не запускает стратегии и не меняет
их assertions. Он создаёт два принадлежащих текущему run ID счёта, пополняет
только RUB и покупает одинаковое целое число лотов CNYRUB_TOM с собственным
бюджетом и резервом комиссии. FILL и native settlement FX проверяются до
контрольных baseline с равными CNY: одна BESTPRICE BUY облигации на два лота
против двух BUY по одному. RUB-остатки могут различаться после FX, но каждая
покупка облигации и весь опыт должны сохранять свой начальный RUB-остаток.
Setup principal/fee не входят в результаты облигаций; поздние операции FX
исключаются по UID пары и связи fee с ID FX BUY в полной истории счёта.
Неатрибутированная RUB-комиссия не игнорируется, guards bond fee сохраняются.
До/после каждой заявки сохраняются native balances, проверенные OrderState
total/commission с units/nano и executed BUY/BROKER_FEE из истории.
Учитываются только исполненные operation IDs; pending сохраняются отдельно
и не дают complete. При тайм-ауте уже наблюдавшиеся факты не теряются.
Сравниваются per-fill native цены и raw-комиссии на лот, а не только суммарный
principal. Для направления raw 7dp / history и balances 6dp сохраняются
численные прогнозы округления каждой комиссии и cumulative final balance,
их остатки и совпавшие модели. При пропущенных или различающихся фактах
совпадение не считается контролируемым. Итог остаётся inconclusive с явными
неисключёнными альтернативами: это не доказанный контракт API и не основание
менять runtime rounding. Бюджет, own caps, идентичность FILL и отсутствие обоих счетов после
cleanup проверяются независимо. Полная коллекция теперь 26 (прежние 17+8 и
один probe); skip, фильтры и частичный success по-прежнему запрещены.
Денежный oracle прежних сценариев остаётся 1e-9.
Во всех восьми валютных E2E INDEX-конфиги явно задают preferred_currency=CNY;
после проверки CNYRUB_TOM harness задаёт FX_PAIR_UIDS из фактического UID пары.
Setup state и обычный Runner используют один и тот же mapping через RunnerParams;
полный FX-каталог сохраняется, guards неоднозначности и метаданных не обходятся.
setup-helper использует тот же чистый приоритет по фактической валюте загруженных
метаданных. Его default RUB совпадает с INDEX. Уникальность после приоритета
остаётся дополнительным guard тестового fixture: несколько кандидатов в
выбранной группе запрещены setup, включая fallback без совпадений.
Рабочий INDEX выбирает первый с WARNING по контракту стратегий; этот guard
и assertions восьми live-сценариев при этом не меняются.
Summary включает preferred_currency, eligible_count после приоритета и
nonpreferred_currency для исключённых этим приоритетом кандидатов.
Отсутствующая/невалидная валюта не считается RUB. Новых RPC нет; проверка
RUSAL ISIN, типа и CNY, SDK UID, лотности и финансовые assertions сохраняются.
Warning адаптера о UID/времени стакана содержит stage/scenario/phase текущего
pytest-узла. Узкий handler дублирует только строки этого безопасного emitter
в stdout при -s, поскольку captured-секции исключаются из отчёта.
Эти строки не добавляют root-поля report/evidence или сырые логи в артефакты;
повторы, exact SHA и cleanup по-прежнему проверяются исходным протоколом.
Валютный catalog preflight дополнительно сохраняет выбранные UID/ticker/class,
валюты, lot, nominal и increment с исходными units/nano и Decimal. Отказ любой
валидируемой FX-пары до CNY-фильтра содержит её UID, конкретное поле,
исходный тип/значение и ограничение, а также отдельно ожидаемый CNY UID.
ValueError сохраняет исходные тип/сообщение и получает безопасную note.
Имена/произвольные поля SDK, credentials, headers, окружение и тексты внешних
исключений не сериализуются. Публичные выбранные числовые metadata не маскируются.
Нулевые служебные записи (nominal=0, целочисленный lot=0, явные три False)
исключаются до FxPair после проверки идентичности и units/nano. Один WARNING
fx_catalog_skipped содержит до20 metadata с UID/nominal/lot/API flags, причину
и omitted_count из уже полученного ответа; UID выбранной пары ему не приписывается.
Валидные недоступные пары для оценки сохраняются; прочие ошибки фатальны.
Записи WARNING отказов, INFO выбранного setup и одна сводка времён этапов
попадают в stdout и существующий список events scenario dump, в том числе
при отказе до configs/accounts. Fixture включает INFO до preflight и
восстанавливает прежний уровень после сцены; чужие SDK-сообщения не форвардятся.
Стакан связывается с ticker/class/currency из уже прочитанных metadata;
отсутствующие labels отмечаются missing. Monotonic/UTC фиксируют начало
запроса, получение ответа и проверку. Возраст при получении вычисляется по
наблюдательным UTC-часам, возраст при проверке — по прежним часам валидации.
Сводка содержит count/duration по именованным этапам, RPC attempts и отдельно
quota waits. Вложенные интервалы включают дочерние и не суммируются как общее
время приложения. Новых RPC, изменений retry/clock guards и оптимизаций нет.
По решению пользователя от 19.10.2026 общий лимит возраста стаканов бумаг и FX
составляет 90 секунд включительно; исторические evidence с лимитом 60s сохраняются.
Fixture сохраняет financial_rpc только для принадлежащих текущему lifecycle
счетов: уже выполняемые PostOrder/GetOrderState/GetPositions, raw native money
и blocked, суммы/комиссии/цены, ID и лоты, UTC и monotonic-длительности.
Наблюдатель охватывает отдельные SDK-клиенты Runner и снимается после сценария.
Повреждённые поля получают безопасное представление, запись проверяется на JSON
до добавления в журнал; исходный ответ остаётся у штатной валидации адаптера.
Секреты, аргументы транспорта и произвольные SDK-объекты не сериализуются;
новых RPC нет. Порядок финансовых RPC задаётся их monotonic-временем, а не
положением в events (журнал приложения добавляется при завершении fixture).
Settlement events отдельно раскладывают expected на исходный остаток,
security principal, FX quote/base и единственную комиссию каждой заявки.
Дополнительных промежуточных снимков нет: отсутствие снимка после отдельной
сделки ограничивает вывод о точной точке расхождения.
Валютный harness до сверки денег сохраняет в events currency_order_money:
проверенные ID/сторону/лоты, total_order_amount, executed_commission и
executed_order_price из уже прочитанного GetOrderState, включая исходные
currency/units/nano и нормализованные Decimal. Каждый существующий settlement
poll до assertion сохраняет currency_settlement_poll с UTC, номером наблюдения,
блокировками и native before/expected/actual, expected_delta/actual_delta/residual
по валютам; residual = actual - expected. Даже при отказе сверки или финального
снимка счёта события остаются в дампе. Новых RPC, округления, изменений денежных
assertions/допусков, повторов или верхнеуровневых полей evidence нет.
Перекрывающиеся облачные вызовы/активные заявки проверить перед промышленным
частым таймером; истории/кеша/внешнего хранилища этот проект не добавляет.
