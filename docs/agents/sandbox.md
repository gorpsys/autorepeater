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
(ref master, pr_number с номером открытого PR).
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
Версию выбирает инициатор: workflow задаёт E2E_EVIDENCE_PROTOCOL=retry-v2
и E2E_TESTED_SHA, trusted verifier — --evidence-format retry-v2 с --collection
и --sha. Отсутствие выбора сохраняет retry-v1 и его фиксированный набор 17
сценариев; в v1 report нет sha и новых полей. Это позволяет проверить сам
инфраструктурный PR действующим master workflow без обхода защиты.
После мержа новый trusted master включает v2; расширенные наборы требуют v2.
Подготовленный `retry-v3` сохраняет независимую полную коллекцию и точный SHA.
Он выбирается только инициатором через E2E_EVIDENCE_PROTOCOL=retry-v3 и trusted
verifier --evidence-format retry-v3, не по полям недоверенного артефакта.
Поддержка v3 и fail-closed auth-разбор выпущены в master 1b015c6 (5.2.2, PR #42).
Trusted dispatcher этого master всё ещё выбирает v2. Candidate workflow PR #40
подготовлен с v3 в live env и verify command; dispatch с ref master продолжает
брать оба выбора с master, поэтому изменение только PR #40 не активирует v3.
Для активации до мержа PR #40 нужен отдельно согласованный PR в master:
изменить оба выбора формата в .github/workflows/sandbox-e2e.yml, пройти
обязательные проверки точного SHA и merge без обхода защиты. Только после
этого явно разрешённый запуск с ref master использует v3 для PR #40.
До активации producer PR #40 сохраняет совместимость с v2. Не проверять
evidence кодом PR. Старый master 0492181 не поддерживает v3.
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
их assertions. Он создаёт два принадлежащих текущему run ID счёта с одинаковым
native CNY pay-in: одна BESTPRICE BUY на два лота против двух BUY по одному.
До/после каждой заявки сохраняются native balances, проверенные OrderState
total/commission с units/nano и executed BUY/BROKER_FEE из истории.
Operation IDs считаются один раз; неполная история, различающийся principal
или неразличимые модели дают inconclusive. Consistent-вердикт описывает
наблюдение, а не документированный контракт API и не основание менять runtime
rounding. Бюджет, own caps, идентичность FILL и отсутствие обоих счетов после
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
