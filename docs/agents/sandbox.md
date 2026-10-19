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
Действующий workflow пока остаётся на v2. Master 0492181 отвергает новые поля
stale и mixed subset после постоянной ошибки, поэтому сначала требуется
мерж поддержки v3 с полной приёмкой по прежнему v2. Активация в PR #40 —
отдельный следующий этап после обновления trusted master; она должна менять
оба выбора формата в trusted workflow. Не проверять evidence кодом PR.
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
Перекрывающиеся облачные вызовы/активные заявки проверить перед промышленным
частым таймером; истории/кеша/внешнего хранилища этот проект не добавляет.
