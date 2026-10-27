# Инструкции по деплою

Действующие требования к изменениям деплойной обвязки.
Команды и процедура — в [техническом README](../../scripts/README.md#production-деплой).
Причины выбора механизмов — в [истории находок](history.md#деплой).

## Контракт деплоя

.github/workflows/deploy-prod.yml запускается после успешного push-master
Sandbox live E2E либо вручную только на master и использует default-архив
с установкой зависимостей в облаке. deploy-prod-reserve.yml запускается
только вручную после отдельного согласования: mode=vendored включает локальный
SDK, mode=offline дополнительно упаковывает установленный runtime.
Резерв не включается автоматически при ошибке обычного деплоя.
Оба workflow допускают только gorpsys/autorepeater/master.
scripts/deploy_gate.py
требует текущий SHA и успешные Lint/Offline checks/Sandbox E2E с проверкой
производителей; workflow_run проверяется по path файла, не динамическому name;
statuses читается newest-first из полного endpoint,
не combined status без creator. Устаревший SHA пропускается. Перед OIDC
повторная проверка. Сборка использует существующий CI и цель
make claude-yandex-archive -> build/yandex-function.zip; резервные режимы
задаются USE_VENDORED_SDK=1 и OFFLINE_ARCHIVE=1. Сборка offline требует PyPI,
но готовый архив не устанавливает зависимости в облаке. Оба workflow имеют
общую очередь deploy-prod, cancel-in-progress=false, queue=max, checkout
точного SHA и повторный gate перед credentials. yc 1.40.0 скачивается
официально; резервного бинарника нет. OIDC без environment,
subject repo:gorpsys/autorepeater:ref:refs/heads/master.
GITHUB_OIDC_AUDIENCE=https://github.com/gorpsys — подтверждённый AUD федерации
для запроса GitHub; audience обмена Yandex IAM — отдельно SA_ID.
Пустой/отсутствующий AUD фатален до запросов токенов, без fallback на SA_ID.
Ключ объекта уникален по SHA/run ID/attempt, пакет проверяется SHA-256.
Оба workflow задают один обязательный FUNCTION_ID существующей функции.
Чтение текущей версии, создание новой и чтение $latest используют только
--function-id; имя функции или каталога не участвует в выборе ресурса.
До upload functionId текущей версии должен совпасть с FUNCTION_ID.
Ответ создания и итоговый $latest независимо сверяются с тем же ожидаемым ID,
даже если ответы согласованы между собой. Отсутствующий/неверный ID фатален;
fallback на FUNCTION_NAME и автоматического создания функции нет.
Оба workflow и строгая проверка settings задают для новой версии 256MB/90s:
к прежним 60s добавлено окно начальной готовности 30s. Переход текущей версии
60s -> новой 90s и read-only проверка executionTimeout=90s покрыты unit-тестами.
FUNCTION_ID, память, runtime/SA/Lockbox сохраняются. Эти изменения ещё не
развёрнуты и не прошли live-приёмку; конфигурация не доказывает активные 90s.
Runtime SA и Lockbox t_token обязательны; текущие env/настройки сохранять либо отказ
до upload при неподдерживаемых полях. Проверять ACTIVE и реальные настройки
через read-only чтение версии; это способ проверки деплоя. Credentials маскировать,
yc запускать с отдельным временным HOME и собственным временным профилем.
yc 1.40.0 требует профиль/endpoint и не подхватывает YC_TOKEN. Для каждого
вызова создавать временный JSON-as-YAML config с current=ci и profiles.ci:
cloud-id из CLOUD_ID, folder-id из FOLDER_ID, endpoint api.cloud.yandex.net:443.
Передавать явные --config/--profile и IAM только через YC_IAM_TOKEN в окружении
дочернего процесса, заменяя унаследованный токен, не меняя окружение родителя.
--token в этой версии запускает OAuth-обмен IamTokenService/Create, не применять
для готового IAM-токена. Credentials не писать в профиль/argv.
Конфиг удаляется и при ошибке; argv/stdout/stderr CLI не логировать.
Ошибки CLI показывают фиксированный reason и полные UUID x-request-id/
x-client-trace-id, если доступны, без произвольных полей и текста провайдера.
Service account ... is not available распознаётся как service_account_unavailable.
Deployment SA нужна iam.serviceAccounts.user на runtime SA либо эквивалентное
унаследованное право; IAM-привязки исправлять отдельно, не менять runtime SA
и не убирать secret ради успешного деплоя.
Для диагностики использовать безопасную категорию ошибки и проверенные request/trace ID.
Полный ответ создания версии и build log текущая обвязка не сохраняет.
Если категории недостаточно, получить их через облачную консоль и удалить
credentials перед передачей; корреляционные ID не заменяют ответ метода.
Read-only ACTIVE/ID/параметры подтверждают публикацию, но не Python cold start.
Облачная import-only приёмка требует отдельного согласования; production
handler для диагностики не вызывать. Исторические результаты — в
[отчёте от 2026-10-13](../history/2026-10-13-sdk-deploy.md).
Защита master требует PR (approvals=0), строгие три checks и enforce_admins.
