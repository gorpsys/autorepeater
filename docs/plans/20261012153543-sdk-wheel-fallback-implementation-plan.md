# Резервный SDK и автономный облачный архив

## Статус и цель

План согласованных доработок. Task 1–10 выполнены и проверены; документация
опубликована, draft PR создан. Общее review и финальная приёмка не завершены.
Inventory с явно записанными conditional
результатами готов, но не объявляет невыясненные обязательства выполненными.
Проверен 12 октября 2026: технические противоречия исправлены.
Пользователь принял D1–D3 и расширил хранение в репозитории на все нужные
бинарные артефакты, лицензии которых допускают распространение, исключив
проекты с исходниками нужной версии на GitHub. После проверки Task 1
пользователь явно разрешил исключение только для восстановленного
`t-tech-investments==1.51.0` с указанным ниже SHA-256 и размером: этот бинарник
включается в Task 2 независимо от matching GitHub source. Наличие готового wheel там
не требуется. Автоматическая сборка этих проектов из source не входит в PR.
Одобрена процедура однократного подтверждения точной пары хешей и версии SDK.
Ни одна конкретная пара не одобрена; начальный список approvals пуст.
Автоматический резерв для остальных сохранённых Python wheels
тоже подтверждён; открытых вопросов по выбранной политике не осталось.

Ветка: `ci/sdk-wheel-fallback`, от `origin/master`
`ba3668702bf2d5f46f6445826c5398615a5e05f1`.
Рабочая копия: `/tmp/autorepeater-sdk-fallback-20261012`.
Незакоммиченная документация в основной рабочей копии остаётся отдельно.
Перед реализацией проверить свежесть master; чужие изменения не переносить.

Цель: сохранить установку SDK `t-tech-investments==1.51.0` при отказе
официального индекса, не скрывая ошибки целостности и других зависимостей.
Добавить два режима существующего облачного архива и отдельный ручной деплой.
Боевые алгоритмы, CLI, SDK API, торговые сценарии и разрешения не меняются.

## Согласованные решения

1. Основной источник SDK — официальный индекс T-Bank. Резерв — неизменённый
   wheel в этом репозитории, версия строго 1.51.0.
2. Автоматический резерв разрешён при DNS/сетевой недоступности, таймаутах,
   HTTP 401/403/404/429/5xx и отсутствии версии в корректном ответе индекса.
   Применяется и к индексу, и к скачиванию самого wheel.
3. Ошибки целостности, несовместимости и установки других пакетов не маскируются
   резервом. Частные правила TLS, формата индекса и источника изложены ниже
   как технические ограничения, а не как дополнительные согласованные причины fallback.
4. Локальный `make setup` и CI используют один механизм. Каждое переключение
   на резерв выводит `WARNING`, причину, версию и путь резерва без credentials.
5. `USE_VENDORED_SDK=1` включает wheel в облачный архив. Установка функции
   использует PyPI для остальных пакетов, но не обращается к T-Bank.
6. `OFFLINE_ARCHIVE=1` включает все runtime-зависимости уже установленными.
   Сборке нужен доступ к PyPI и/или резерву; распакованному архиву не нужна
   установка пакетов. Это не отключает обращения приложения к API брокера.
7. Автономная сборка требует Linux x86_64 и Python 3.12, без Docker.
   Несовместимое окружение отклоняется до скачивания зависимостей.
8. Обычный `.github/workflows/deploy-prod.yml` оставить без изменений.
   Новый workflow запускается только вручную, с выбором `vendored`/`offline`,
   и сохраняет все ограничения исходного деплоя.
9. Происхождение восстановленного wheel принимается с явной оговоркой:
   независимая сверка хеша с официальным реестром не выполнена.
10. Новые сторонние Python-зависимости, ослабление проверок и новые ключи
    облачного доступа не требуются и не входят в план.
11. Реальный package smoke нужен и как обязательный этап CI, и как отдельный
    ручной workflow; оба запускают одну Make-цель (D1).
12. Хеш резервного SDK проверяется по локальному manifest, официального —
    по индексу. При доступном индексе и различии этих хешей обязательно
    ручное одобрение; молчаливый выбор источника запрещён (D2).
13. Нужен полный runtime lock, общий для setup/E2E/облачных сборок (D3).
14. В репозитории хранится весь необходимый набор бинарных артефактов,
    за исключением проектов, доступных на GitHub, и тех артефактов,
    чьи условия не допускают такого распространения.
    Лицензия с дополнительными обязательствами не приравнивается к запрету.
    Единственное явное исключение GitHub-правила: recovered SDK 1.51.0,
    SHA-256 `f0fe22cbed727cecd9971200032d893c19702838b971c08abc2831fe25fa2856`,
    размер 316190. Пользователь разрешил его хранение после подтверждения
    matching source. Это не разрешает другую версию/байты, source build,
    общий default allow или одобрение конкретной пары reserve/index hashes.
15. Для всех сохранённых Python wheels автоматический резерв применяется
    при разрешённой недоступности PyPI/файла, с WARNING и проверкой целостности.
    Источник остаётся PyPI-first; runtime version/artifact обязан соответствовать
    lock. yc исключён из хранения: не хранится и не резервируется,
    используется только стандартная загрузка с отказом при недоступности.
    Основной deploy workflow не меняется.

## Границы локального набора

### Состав и GitHub-исключение

Область inventory: runtime dependencies и инструменты
make lint/test/coverage/e2e вместе с их полным dependency graph.
Бинарники проектов, доступных на GitHub, в наш репозиторий не добавлять.
Из этого правила исключён только явно одобренный recovered SDK 1.51.0.
Исходников нужной версии на GitHub достаточно для исключения, даже без
готового wheel. Проверять tag/commit и доступность исходников, а не только
ссылку в METADATA. Другая версия не удовлетворяет runtime lock.
Для подходящего GitHub источника в inventory хранить точную identity,
а не копировать его бинарник или использовать floating main/latest.
Это не меняет штатную установку из PyPI: GitHub source служит возможностью
ручного восстановления при аварии, не новым автоматическим resolver.
Development profile не может менять зафиксированный runtime graph.
Проектные deployment tools, включая yc CLI 1.40.0, проверить по тому же
правилу source/лицензии; данные inventory не дают права менять обычный deploy
workflow или исполнять cloud calls. Системные Python/Git/Make/Actions не
копировать: они предоставляются рабочим окружением, а не нашим пакетом.
yc исключён из хранения независимо от результатов licence/source проверки;
ожидания разрешения binary/storage нет, резервного канала доставки нет.

### Выполнение условий лицензий

Для Apache/MIT/BSD сохранять применимые licence/copyright/NOTICE.
Для GPL/LGPL/MPL проверить конкретные обязанности; при необходимости хранить
corresponding source/source distribution вместе с wheel и notices.
Эти материалы входят в обязательное выполнение лицензии, а не являются
основанием автоматически исключать copyleft-пакет. Не дублировать GitHub
source в нашем репозитории без необходимости по применимым условиям.
Неизвестная или действительно запрещающая redistribution лицензия — отказ
от публикации артефакта до выяснения. Нельзя молча урезать обязательный runtime
graph; полностью автономный режим в таком случае не объявляется успешным.
Для iprotopy 0.3.0 действует notice-only состав: исходная METADATA и
THIRD_PARTY_NOTICES/iprotopy только с автором, версией и exact immutable source
link. Отдельного upstream LICENSE нет; MIT текст/copyright/год не добавлять,
к автору не обращаться. Conditional/fulfilled=false остаётся явным фактом,
не объявлять неустановленные upstream obligations выполненными. Для остальных
пакетов проверки всех применимых licence/source/notice obligations сохраняются.

### Предпочтение источников

Сохраняется изначально согласованный резервный характер: основной источник
SDK — T-Bank, остальные штатные packages — PyPI. Включённый локальный бинарник
не становится автоматически основным источником. Полностью автономный ZIP
использует собранный runtime payload и не устанавливает зависимости в облаке.
Все сохранённые Python wheels доступны автоматическому fallback в общем
installer и подготовке offline payload. Переключение выполняется для
конкретного package, не меняет источник SDK или версии соседних пакетов.
Разрешённые transport/status причины те же, что для SDK; TLS/metadata/hash/
совместимость и solver/install errors остаются фатальными. WARNING содержит
имя/version/reason и безопасный путь выбранного reserve, без сырого stderr.
При недоступности PyPI для GitHub-backed проекта явно завершаться ошибкой:
автоматическую native source сборку не вводить без отдельного плана.
Согласованная проверка SDK по официальному индексу и одобрение расхождения
хешей сохраняются в любом варианте; vendoring не отменяет эту границу доверия.
Forced-vendored выбирает локальный SDK, но проверка доступного индекса не
может обходить требование одобрения. Выполнить bounded index probe; при
разрешённой недоступности индекса резервная сборка всё равно допускается.
Не делать установку из T-Bank в облаке для этого режима.

### Одобрение SDK

Одобрение требуется один раз для точной пары reserve/index SHA-256 и версии
SDK, с записью в repo manifest
после явного подтверждения пользователя, датой и ссылкой на обсуждение.
Новый index hash, reserve hash или SDK version аннулирует применимость записи.
Генератор lock/SDK download не может автоматически создать такую запись.
Без совпадающего одобрения SDK mismatch завершается approval_required без установки
и упаковки; общий ALLOW_HASH_MISMATCH и неограниченный CLI обход не допускаются.
Запись содержит version, reserve_sha256, index_sha256, approved_by,
approved_at_utc и evidence; отсутствие/неверный тип поля, duplicate pair или
некорректный digest отклоняются. Подтверждение этой процедуры не является
одобрением конкретной пары: начальный approvals список пуст.
После одобрения новый официальный artifact добавляется в runtime lock только
явным обновлением, с проверкой metadata/dependency closure и повторными checks.
Одобрение manifest не разрешает install незакреплённого в lock artifact.

## Контекст кода

| Файл | Текущее поведение | Планируемое изменение |
| --- | --- | --- |
| `Makefile` | setup создаёт .venv и вызывает pip с requirements-dev.txt | Установка через общий helper; два дополнительных флага архива |
| `requirements.txt` | Глобальный extra-index T-Bank, trusted-host, SDK 1.51.0 и python-json-logger 3.3.0 | Разделить источники SDK и общие PyPI-зависимости без изменения состава |
| `requirements-dev.txt` | Включает runtime requirements и инструменты разработки | Вынести список инструментов для переиспользования без индекса T-Bank |
| `scripts/build_yandex_archive.py:17` | archive_files выбирает приложение, requirements и JSON | Учитывать общие runtime requirements и режим архива |
| `scripts/build_yandex_archive.py:27` | build_archive пишет build/yandex-function.zip | Дополнительные режимы без переименования цели и результата |
| `scripts/build_yandex_archive.py:39` | argparse для project-root/output | Добавить явные параметры двух режимов |
| `.github/workflows/pylint.yml` | make setup, make lint | Сохранить Make; обозначить установку с резервом |
| `.github/workflows/python-app.yml` | make setup, lint и один coverage | Сохранить один прогон тестов и действующие gates |
| `.github/workflows/sandbox-e2e.yml` | make setup с лимитом 6 минут | Тот же helper, без изменений SHA/evidence/cleanup/retry |
| `scripts/deploy_gate.py:18` | Только master и доверенный инициатор | Переиспользовать без копирования gate в новый workflow |
| `scripts/deploy_gate.py:96` | Lint, Offline checks, Sandbox E2E для точного SHA | Сохранить producer identity, pagination и latest status |
| `scripts/yandex_deploy.py` | OIDC, upload, create version, read-only verify | Переиспользовать без изменения авторизации и ресурсов |
| `pyproject.toml` | Явный список типизируемых helpers | Включить новые helpers и изменяемую сборку |
| `scripts/make_environment.py:29` | При недостающем модуле советует прямой pip install | Рекомендовать helper с резервом для выбранного интерпретатора |
| `test/test_yandex_archive.py:117` | Изолированная копия Makefile и одного builder script | Сохранить default entrypoint, обновить fixtures для новых requirements |

Важно: preinstall wheel вместе с исходным requirements-dev.txt недостаточен.
Глобальный extra-index остаётся активным для поиска других зависимостей.
Резервный путь должен исключать его, в том числе из вложенных requirements.

## Подход и проверка

TDD RED -> GREEN для каждого изменённого поведения. Каждая кодовая задача
заканчивается успешным прогоном своих новых и затронутых тестов.
Моки — autospec(spec_set=True); явные response DTO/bytes вместо permissive Mock.
Offline pytest не скачивает пакеты и не обращается к брокеру/облаку.
Реальные установки проверять отдельно в чистых временных окружениях.

Сохранить строгий lint и общее/diff-покрытие >=90%; новую инфраструктуру
не исключать из покрытия. Новые функции типизировать.
Ручные правки через apply_patch. Wheel переносить как неизменённый бинарный
артефакт и подтверждать SHA-256, не пересобирать его.
После каждой задачи отмечать чекбоксы; изменения объёма фиксировать здесь.

## Техническое решение

### Артефакт и лицензия

Перед добавлением бинарников создать `vendor/inventory.json` с version,
source URL/tag/commit, результатом GitHub-исключения, licence/evidence,
artifact filename/SHA-256 и причиной хранения либо отказа. Для unknown source
или licence не объявлять артефакт разрешённым; сначала завершить проверку.
GitHub-backed бинарники не копировать, кроме явного SDK-only override выше.
Фактический список остальных файлов
закрепить в Files-блоке задачи после inventory, до их добавления.
Для файлов больше лимита обычного GitHub Git прекратить добавление и отдельно
согласовать способ хранения; Git LFS/Release assets не вводить скрытно.

Хранить в `vendor/t-tech-investments/`:

- `t_tech_investments-1.51.0-py3-none-any.whl`;
- `LICENSE` — точная копия Apache-2.0 из wheel, включая Copyright 2023 Tinkoff;
- `manifest.json` — name, version, filename, size, sha256, wheel tag,
  license, официальный project/index URL, источник восстановления и
  `official_hash_verified: false`;
- `README.md` — назначение резерва, условия использования и процедура обновления.

Источник копирования:
`/tmp/autorepeater-sdk-recovered/t_tech_investments-1.51.0-py3-none-any.whl`.
Размер: 316190 байт. SHA-256:
`f0fe22cbed727cecd9971200032d893c19702838b971c08abc2831fe25fa2856`.
ZIP CRC и 128 хешей RECORD проверены; содержимое этих файлов совпало
с установленным SDK. Это проверка целостности, не доказательство происхождения.
Не публиковать абсолютные пользовательские пути из исходного verification.json.

METADATA содержит Apache-2.0; отдельного NOTICE в восстановленном wheel нет.
Не добавлять выдуманный upstream NOTICE. Сохранить все имеющиеся лицензии,
copyright и attribution. Не объявлять собственное приложение Apache-2.0
на основании лицензии SDK и не подразумевать одобрение T-Bank.
Ссылки: https://www.apache.org/licenses/LICENSE-2.0 и
https://opensource.tbank.ru/invest/invest-python .

### Разделение требований и установка

Добавить canonical `runtime-lock.json`: CPython 3.12/cloud target, все
обязательные runtime packages с точными версиями, artifact hashes и source
identity; SDK version/артефакты согласуются с vendor manifest и approvals.
Начальные версии сверить с рабочим Python 3.12 окружением и подтвердить чистой
установкой, не обновлять их до latest побочно. Перечень 24 packages изученного
окружения — исходная проверка, не жёстко заданное число.
`scripts/runtime_lock.py` валидирует schema, генерирует pip input/constraints
и проверяет installed graph. `scripts/update_runtime_lock.py` — явное обновление
через pip resolver/его JSON report, без внутренних API pip. Обычные сборки
потребляют готовый lock, не перегенерируют его и не разрешают ranges заново.

Генерировать requirements-runtime.txt (runtime кроме SDK с hashes),
requirements-runtime-pins.txt (весь runtime, включая SDK, без hash-mode)
и runtime constraints (весь runtime, без hashes) из lock;
requirements-dev-tools.txt сохраняет существующий набор инструментов.
Сохранить публичные requirements.txt/requirements-dev.txt и использовать
единственный генератор, с тестом соответствия checked-in файлов canonical lock.
requirements.txt содержит основной SDK pin с разрешёнными hashes и включает
requirements-runtime.txt. requirements-dev.txt включает hashless runtime pins
и dev tools; не включает hashed requirements.txt, иначе pip потребует hashes
также для всех unpinned dev tools. Source/index directives генерируются из
того же source policy, без независимых вручную поддерживаемых SDK pins.
Raw pip exports сохраняют совместимость, но документированный путь проверки
SDK mismatch/резерва — Make/helper; raw pip не обещает эту дополнительную границу.
В обычный ZIP включить все вложенные runtime requirements/lock references;
deploy-prod.yml остаётся прежним, но версии зависимостей в его ZIP теперь locked.
Hash-mode input должен содержать hashes всех runtime требований, включая
выбранный SDK, а не смешивать hashed runtime и unpinned dev tools в одном pip
вызове. Устанавливать verified runtime отдельно, затем dev tools с runtime
constraints; после этого снова проверить неизменность runtime graph.
Одинаковая установленная version не доказывает artifact identity. Runtime
устанавливать из выбранных проверенных wheels с --force-reinstall и --no-deps:
полный dependency closure уже задан lock, повторный resolver не нужен.
Receipt с lock digest/source/hash писать атомарно только после успешной
установки и проверки graph; перед изменением runtime аннулировать прежний
receipt, failed/partial install не оставляет success receipt.
До установки проверить конфликты существующего окружения; не удалять
посторонние packages. Guard Make для lint/test/coverage/e2e дополнительно
проверяет runtime versions/closure и receipt; отсутствие или устаревший receipt
требует setup, а не запуска тестов на произвольном SDK. Это контракт Make,
не новая обязательная установка перед прямым запуском боевого CLI.
Обычный архив и runtime export requirements.txt не выполняют сетевой probe:
они допускают лишь заранее проверенные SDK hashes из lock. Новый неизвестный
официальный hash не устанавливается до отдельного approval и обновления lock.
Hashless requirements-dev.txt обеспечивает совместимость raw pip, но не
гарантирует проверку artifact hashes/approvals и не выдаёт installed receipt.
Для последующих Make checks/e2e нужен helper setup: raw dev install не
считается подтверждённым окружением даже при совпадении versions.
Оговорка о raw pip относится к approvals, но не к TLS: generated requirements
не должны содержать --trusted-host, отключающий проверку сертификата T-Bank.
Поддерживаемые Python/платформы setup сохранить; если текущий lock не покрывает
заявленную платформу, явно согласовать дополнительный профиль или ограничение,
а не молча устанавливать незакреплённые зависимости.
Проверка installed graph в dev .venv проверяет все locked runtime packages
и их closure/версии, но не объявляет pytest/pylint/bootstrap pip лишним runtime.
В чистом cloud target, напротив, разрешён ровно runtime payload из lock.
Не удалять чужие packages из существующей .venv ради прохождения проверки;
обнаруженный конфликт отображать как ошибку с советом создать чистое окружение.

`scripts/sdk_wheel.py` отвечает только за проверку артефакта и выбор источника:
SDK source `auto` или `vendored`. В auto читать официальный simple index
через urllib с TLS-проверкой; HTML разбирать HTMLParser, URL — urllib.parse,
METADATA — email parser, manifest — json. Это не общий resolver пакетов:
поддерживается только известный wheel точной версии и его безопасный URL.
Не использовать внутренние API pip или распознавание локализованных pip-ошибок.

Валидировать ожидаемые filename/name/version/tag и метаданные wheel,
не доверять произвольному пути из manifest. Для резервного файла обязательны
точные size/hash из manifest. Для первичного скачивания обязателен hash из
индекса: отсутствие digest — ошибка проверки, а не доверие резервному hash.
Не требовать равенства index/reserve hashes как условия целостности:
их различие — отдельное approval_required по процедуре «Одобрение SDK».
Файл, не соответствующий своему index/manifest hash, фатален без fallback;
ручное одобрение не разрешает повреждённые или неверно идентифицированные bytes.
Не следовать произвольным внешним redirect/URL: только HTTPS утверждённого
официального origin без credentials; неизвестный источник — фатальная ошибка.
Неверный сертификат TLS также фатален. Запрашивать text/html у project endpoint
`https://opensource.tbank.ru/api/v4/projects/238/packages/pypi/simple/t-tech-investments/`.
Некорректный media type/формат, неоднозначные подходящие ссылки и явно yanked
файл отклонять; не считать их отсутствием версии и не обходить резервом.
HTMLParser терпим к повреждённому HTML: отсутствие найденных anchors само по себе
не доказывает корректность индекса. Определить поддерживаемый формат и отдельно
проверить пустой корректный индекс, login/error page и посторонний JSON.

Проверять хеш до разбора ZIP и работать с теми же ограниченными bytes
(например, BytesIO), а не перечитывать mutable path после валидации.
Сохранять владение скачанным временным файлом до конца pip/упаковки и очищать
его при успехе, ошибке и прерывании. Отдельно отклонять symlinked manifest/LICENSE.
Лимит скачивания не ограничивает распакованный ZIP. Для SDK отдельно задать
пределы 32 MiB суммарного uncompressed size и 512 entries; проверить CRC/RECORD,
duplicate/absolute/parent-traversal paths, symlink entries и шифрование до pip.
Повреждение этих структур фатально, даже при совпадении внешнего digest.

Одна попытка выбора источника, без длительной серии сетевых повторов:
таймаут отдельного запроса <=15 секунд, общий SDK этап <=60 секунд,
ограничение ответа/загрузки <=2 MiB. Общий deadline должен ограничивать
DNS и slow streaming, а не только socket timeout: SDK acquisition выполнять
в ограниченном дочернем процессе с timeout и подтверждённым завершением.
Контракт результата worker — проверенный JSON с source/reason и собственным
временным путём; произвольный stderr/response body не публиковать.
По разрешённой ошибке — один резерв.
В forced vendored T-Bank не является источником установленного SDK, но
bounded index probe сохраняет mismatch approval; выводить WARNING с причиной
`explicit_vendored_mode`. Без исправного резерва установка завершается ошибкой.

`scripts/install_dependencies.py` запускается Python из создаваемой .venv.
Он получает SDK wheel от sdk_wheel и передаёт pip явный локальный файл,
полный locked runtime input. Остальные runtime wheels поступают из PyPI
либо подходящего разрешённого repo reserve, с hashes из lock, без T-Bank
extra-index для прочих packages. Для setup отдельно установить dev tools
под runtime constraints и проверить, что lock не нарушен.
`scripts/package_wheels.py` отвечает за выбор/проверку остальных сохранённых
Python wheels, а не за новый dependency resolver. Runtime получает точные
version/filename/hash/target из lock; dev reserve — конкретную проверенную
version/filename/hash из inventory. Не выбирать произвольную older/latest
version лишь ради успешного fallback. Dev graph разрешает pip под runtime
constraints; разрешённый reserve не даёт права на изменение runtime closure.
Fallback выполняется на этапе получения wheels до install, по typed
transport/status facts, не по тексту pip stderr и не после любой pip ошибки.
Проверки bytes/metadata/path/license общие с inventory; приобретение official
PyPI artifact и fallback соблюдают source allowlist, TLS, ownership и deadlines.
Лимит 2 MiB относится только к SDK: остальные wheels имеют собственные
зафиксированные size limits с учётом native файлов; нельзя обрезать их SDK cap.
В auto SDK сначала скачивается с официального источника, поэтому резерв
не становится основным источником. В текущем setup всегда проверять источник;
имеющаяся установка SDK не является доказательством доступности индекса.

Для управляемых pip-вызовов отключить сторонние pip-config/index overrides,
задавать явный PyPI URL, timeout/retries и subprocess timeout; не печатать
произвольные credentials-bearing URL или окружение. Общий бюджет helper
setup <=300 секунд (SDK <=60, оставшееся — pip), чтобы укладываться в 6 минут CI.
Недоступность PyPI даёт исправный разрешённый reserve только соответствующего
package; отсутствие такого reserve, solver/install/TLS/hash errors возвращают
ненулевой код и не переключают источник SDK. Суммарный deadline не увеличивается
на число пакетов или повторных fallback попыток.
VENV_PYTHON, как и сейчас, создаёт .venv для make setup; PYTHON выбирает
интерпретатор остальных Make targets, но не подменяет bootstrap-интерпретатор.
Для явного внешнего PYTHON предусмотреть документированный запуск helper
этим интерпретатором из корня проекта, например
`/path/to/python -m scripts.install_dependencies --development`.
Обновить make_environment и его тест: сообщение об отсутствующих модулях
должно предлагать этот путь, а не обходящий резерв pip -r requirements-dev.txt.
Сам helper до установки импортирует только stdlib/собственные stdlib helpers,
без SDK, приложения, packaging или инструментов разработки.

### Режимы облачного архива

| USE_VENDORED_SDK | OFFLINE_ARCHIVE | Результат |
| --- | --- | --- |
| 0 | 0 | Обычный архив; прежняя установка через требования и основной индекс |
| 1 | 0 | Локальный SDK wheel и требования только к PyPI для остальных пакетов |
| 0 | 1 | Уже установленные runtime-зависимости; SDK выбирается auto |
| 1 | 1 | Уже установленные runtime-зависимости; SDK принудительно из резерва |

Значения флагов только 0/1; опечатка не должна молча включать обычную сборку.
Make передаёт явные параметры `--use-vendored-sdk`/`--offline-archive`.
Существующие --project-root/--output, PYTHON и выбор .venv сохраняются.
Сохранить непосредственный `python scripts/build_yandex_archive.py`, runpy и
вызов из другого cwd с --project-root. Прямой script entrypoint имеет scripts/,
а не корень проекта в sys.path: новые `from scripts...` не должны ломать его.
Новые helper imports загружать только для соответствующего режима; default
остаётся stdlib-only и работает в изолированной копии без SDK/vendor/helpers.
В новых режимах отдельно проверить полный комплект helpers и разрешение
project-root, без опоры на унаследованный PYTHONPATH или основную рабочую копию.

Режим vendored: включить wheel с тем же относительным путём, LICENSE,
manifest и runtime requirements. Сгенерировать requirements.txt только внутри
архива: ссылка на локальный wheel плюс runtime requirements, без extra-index,
trusted-host и удалённой ссылки на SDK. Исходники requirements не переписывать.
Установку распакованного результата проверить отдельно с заблокированным T-Bank.

`scripts/cloud_dependencies.py` реализует только подготовку автономного payload.
Проверять sys.platform, platform.machine, implementation и sys.version_info
(Linux, x86_64/amd64, CPython 3.12), а также glibc >=2.17 до сети.
Musl/Alpine и PyPy не являются совместимыми build-host даже при подходящем
номере Python. Использовать отдельные TemporaryDirectory для
wheelhouse и target; не копировать site-packages текущей .venv.

Сначала pip download полного runtime-графа по canonical lock, без нового выбора
версий: SDK wheel плюс runtime requirements и их применимые hashes,
только binary wheels под CPython 3.12, x86_64, manylinux2014/manylinux_2_17
и совместимые universal/abi3 tags. Ограничение manylinux предотвращает
случайную привязку к более новому glibc GitHub runner/локального хоста.
Отсутствие подходящего wheel — явная ошибка, без неявного запуска компилятора.
Затем pip install --no-index --find-links wheelhouse --target target.
Применимость environment markers должна определяться реальным Python 3.12.
Два этапа имеют отдельные deadlines <=300 секунд и конечные pip timeout/retries.

Включить содержимое target в корень ZIP, чтобы обычный handler импортировал
пакеты без bootstrap-кода. Сохранить .dist-info, LICENSE/NOTICE и runtime-data;
не включать requirements-dev-tools.txt, кэш, credentials, .venv или wheelhouse.
Сохранять весь обязательный runtime-граф по metadata, без фильтра по названию:
SDK -> iprotopy -> grpcio-tools/mypy-protobuf/ANTLR/setuptools остаются,
хотя их названия похожи на инструменты разработки. Extras `all` не включать.
Не включать requirements.txt, вызывающий установку на стороне облака.
Не разрешать файлам зависимостей перезаписать main.py, handler.py,
autorepeater/ или другие корни приложения; symlink/path escape — ошибка.
Включить dependency manifest с разрешёнными версиями и SHA-256 исходных wheels.
Manifest содержит digest canonical lock и выбранный SDK artifact hash; набор
версий обязан совпадать с lock. Запрещены свободный resolver fallback, missing
package, лишний runtime package и незафиксированный artifact. Bootstrap pip
не считать приложением; обязательный через SDK setuptools, напротив, включать.
SDK source может отличаться только по явно разрешённой процедуре одобрения.
PyPI wheels GitHub-backed проектов можно включать в генерируемый облачный ZIP:
запрет относится к хранению их бинарников в нашем Git, не к runtime payload.
Перед распространением автономного ZIP проверить лицензии всего разрешённого
графа, а не только SDK. Если лицензия неизвестна или требует дополнительных
действий, выполнить применимые требования и зафиксировать их в inventory;
при неясности остановить распространение и согласовать решение.
Например, в текущем графе certifi использует MPL-2.0: одной Apache-2.0
недостаточно. Проверить сохранность его license/исходной формы, notices и
сведений о доступе к исходникам. Допустимость MPL-2.0 не подменять запретом
на любые непермиссивные лицензии. Инвентарь относится к фактическому графу ZIP.

До публикации проверить CRC ZIP и отсутствие конфликтов путей.
Для offline обязательно выполнить изолированный import smoke по payload;
для vendored проверять структуру/requirements/wheel, а import smoke делать
после отдельной установки распакованного архива в чистое окружение.
Не требовать import smoke от vendored-builder: он намеренно не устанавливает
остальные зависимости и не ограничивает платформу локальной сборки.
Новые режимы писать во временный ZIP рядом с
результатом и заменять его только после проверки; не удалять готовый архив
при отказе скачивания/сборки. Не расширять задачу до общего рефакторинга builder.
Минимальный isolated payload import smoke реализовать в cloud_dependencies
уже в задаче 7; задача 9 переиспользует его, а не создаёт требуемую builder
проверку позже. Новый checker не должен быть зависимостью незавершённой задачи 7.

Provider contract: официальная документация подтверждает pip install в корне
проекта и Ubuntu 22.04 для runtime. Источники:
https://yandex.cloud/ru/docs/functions/lang/python/dependencies и
https://yandex.cloud/ru/docs/functions/concepts/runtime/index .
Следовательно, local wheel в requirements имеет обоснованный относительный путь.
Локальный smoke не доказывает успешный cold start в Yandex: native imports в
облаке и пропуск installation phase без requirements проверяются отдельно
при согласованной внешней приёмке. ACTIVE/read-only проверка подтверждает
публикацию и параметры версии, а не исполнение её Python-кода.

### Ручной резервный деплой

Новый `.github/workflows/deploy-prod-reserve.yml`:
только workflow_dispatch, input `mode` типа choice (`vendored`, `offline`).
Нет push/workflow_run/pull_request trigger. В vendored вызывать
`make claude-yandex-archive USE_VENDORED_SDK=1`; в offline —
`make claude-yandex-archive USE_VENDORED_SDK=1 OFFLINE_ARCHIVE=1`.
Выбор через проверенный case/условия, без интерполяции произвольных shell args.

Сохранить две jobs gate/deploy и минимальные permissions. Gate переиспользует
scripts.deploy_gate, допускает только gorpsys/autorepeater/master, требует
успешные Lint/Offline checks/Sandbox E2E для текущего полного SHA с проверкой
producer. Checkout приложения — точный SHA; до OIDC повторить --check-once.
Сборка нового режима не даёт дополнительных оснований обойти E2E.

Concurrency — тот же `deploy-prod`, cancel-in-progress=false, queue=max,
чтобы обычный и резервный деплой не выполнялись одновременно.
Скопировать подтверждённые параметры исходного workflow, но не логику Python:
256MB/60s, python312, handler.handler, deployment/runtime SA, Lockbox,
Object Storage, уникальный key/digest и read-only ACTIVE verification.
OIDC без environment; AUD GitHub и audience IAM остаются различными.
Не добавлять открытый вывод токенов, yc profile credentials или production invoke.
Workflow-тестом контролировать паритет с обычным файлом; сам обычный файл не менять.

## Реализация по задачам

### Task 1: Inventory источников и лицензий

**Files:** создать `vendor/inventory.json`, `vendor/README.md`,
`scripts/vendor_inventory.py`, `test/test_vendor_inventory.py`;
изменить `pyproject.toml`.

- [x] RED-тесты schema и решения о хранении: source доступен на GitHub,
  только другой release, разрешённая/условная/запрещённая/неизвестная licence.
- [x] Проверить runtime/dev/project-managed deployment dependencies;
  записать source URL/tag/commit, licence evidence, version и обоснование.
- [x] Для GitHub source нужной версии исключить wheel даже без готового asset;
  transient network error проверки не превращать в доказательство отсутствия source.
  Явный override пользователя применяется только к recovered SDK 1.51.0;
  остальные matching GitHub binaries исключены.
- [x] Для remaining packages разрешать публикацию только после выполнения
  условий лицензии; дополнить Files списка бинарников конкретными именами.
- [x] Проверить malformed fields, duplicate identity, unsupported tag/hash,
  symlink/path escape и отсутствие неразрешённых бинарников в tracked/untracked vendor.
- [x] Прогнать inventory tests и typecheck до GREEN; binaries пока не добавлять
  по одним лишь metadata classifiers или предположениям о наличии GitHub repo.

**Проверки Task 1:** начальный RED — pytest collection exit 2, отсутствовал
`scripts.vendor_inventory`; после реализации GREEN — 148 новых/затронутых
tests. Полный `make coverage` — 3187 passed, 0 failed, 18 warnings,
общее покрытие 99.73%; helper/diff 98.46% (untracked проверен отдельным
`--diff-file`, не пропущен Git). `make lint` exit 0, Pylint 10.00/10,
Flake8 без замечаний, Mypy без ошибок в 16 files; `git diff --check` exit 0.
Использован существующий внешний Python, окружение/site-packages не менялись.
Сеть применялась только для bounded source/licence/package metadata evidence;
pytest offline, broker/cloud/live E2E/build/deploy не выполнялись.

**Уточнение scope по проверенным фактам Task 1 (2026-10-12):**
inventory описывает seed CPython 3.12/Linux без extras: 47 Python distributions,
24 из них runtime; число не становится инвариантом будущего lock. У всех есть
GitHub source нужного release/commit. Только recovered SDK разрешён к хранению
по явному указанию пользователя, его бинарник и LICENSE добавляются в Task 2.
Системные Python/Git/Make/Actions предоставляются окружением, не vendoring.

SDK source: https://github.com/Invest-SDK/invest-python,
commit `0110e451cb0316e975ac5fc5ee027b95246f6396`.
`pyproject.toml` объявляет name `t-tech-investments` и version `1.51.0`
в project и poetry; complete tree содержит 338 blobs. Все 125 файлов `t_tech/`
в recovered wheel совпали с source по Git blob SHA1, пропусков/различий нет;
ZIP CRC исправен. Это сопоставимость кода, не независимая сверка official hash.
Backend `poetry.core.masonry.api`, requirements `poetry-core>=1.0.0`;
сборка source и добавление backend dependencies не выполнялись и не разрешены.
Выбранный вариант: неизменённый recovered wheel в Task 2; возможная source
сборка требует отдельного решения, автоматический source build не входит в PR.
После override условный SDK blocker Task 2 закрыт решением пользователя;
проверки hashes/licence, provenance caveat и пустой approvals список сохранены.

Inventory также фиксирует внешние ограничения, а не объявляет их решёнными:
`iprotopy 0.3.0` имеет явное `license="MIT"` и author attribution
Ruslan Sirazhetdinov в author pyproject и опубликованной METADATA; отдельного
текста licence/copyright в repo/README/wheel/sdist нет. Это не доказательство
запрета или отсутствия MIT permission. Все 20 файлов кода wheel совпали
с tag `v0.3.0`, commit `031ae0a6536f07964c9802dfe099efeb701e1c25`,
несмотря на stale pyproject version 0.1.0. Repo binary исключён по GitHub
независимо от лицензии. Можно сохранять оригинальные author/licence/source
declarations и METADATA без выдуманного copyright. Полнота notices для
публичной раздачи mandatory payload отмечена conditional/fulfilled=false.
Применяется notice-only состав: оригинальная METADATA и notice только с
автором, версией и immutable source link, без добавленного MIT текста/года.
Контакт с автором не требуется и не выполняется; неподтверждённые upstream
notice obligations не объявляются выполненными.
`yc 1.40.0`: official Linux/amd64 HEAD HTTP200, Content-Length 192229560;
matching source и redistribution licence не подтверждены. Файл превышает
обычный GitHub Git limit. yc исключён из хранения и резерва;
ожидания licence/storage решения нет. Ни binary, ни LFS/Release assets не
добавляются. В inventory исходные licence/source evidence остаются unknown;
deployment использует штатную загрузку и завершается ошибкой при недоступности.

### Task 2: Резервный артефакт и строгая проверка

**Files:** создать:
`vendor/t-tech-investments/t_tech_investments-1.51.0-py3-none-any.whl`,
`vendor/t-tech-investments/LICENSE`, `vendor/t-tech-investments/manifest.json`,
`vendor/t-tech-investments/README.md`, `scripts/sdk_wheel.py`,
`test/test_sdk_wheel.py`; изменить `pyproject.toml` для нового helper.
После задачи 1 дополнить этот Files-блок конкретными разрешёнными бинарниками,
лицензиями/исходными материалами и manifests остальных inventory entries;
изменить `scripts/vendor_inventory.py`, `test/test_vendor_inventory.py`.
По inventory Task 1 единственный разрешённый бинарник уже перечислен выше:
recovered SDK `t_tech_investments-1.51.0-py3-none-any.whl` (316190 bytes,
SHA-256 `f0fe22cbed727cecd9971200032d893c19702838b971c08abc2831fe25fa2856`),
с неизменённым LICENSE из wheel. Остальные Python candidates исключены
по matching GitHub source; `yc` исключён из хранения независимо от
неустановленных прав/source и размера 192229560. Резерв yc не требуется.
Дополнительных разрешённых binaries для Files нет.

- [x] Написать RED-тесты manifest/проверки wheel и ошибок до любых эффектов.
- [x] По явному SDK-only override пользователя перенести recovered wheel
  `t-tech-investments==1.51.0` с указанными hash/size и неизменённый LICENSE;
  записать безопасный manifest. Matching GitHub source подтверждён и не
  блокирует этот конкретный артефакт. Для иных packages/версий override
  не применяется; official_hash_verified=false и approvals=[] сохраняются.
- [x] Реализовать typed validation: хеш, ZIP, metadata, ожидаемая версия/путь.
- [x] Завершить набор eligible binaries по inventory: дополнительных eligible
  binaries нет. Сохранён единственный разрешённый SDK с обязательным LICENSE;
  фактические hash/size обоих файлов проверены inventory validator.
- [x] Проверить неверный hash/размер/name/version/tag, symlink и отсутствующий файл.
- [x] Проверить ZIP expansion limits, CRC/RECORD, duplicate/traversal/symlink
  entries и отказ без pip при повреждённом wheel.
- [x] Проверить лицензию/copyright и official_hash_verified=false на реальном wheel.
- [x] Прогнать test/test_sdk_wheel.py и typecheck; получить GREEN.

**Проверки Task 2 (2026-10-12):** начальный RED — pytest collection exit 2:
`scripts.sdk_wheel` отсутствовал. После реализации GREEN — 224 SDK/inventory
tests и 74 затронутых Make/environment/archive tests (298 всего).
`make lint` exit 0: Pylint 10.00/10, Flake8 без замечаний, Mypy 17 files;
отдельный `make typecheck` также GREEN. SDK helper покрыт на 100%, оба
затронутых helpers — 99.24%; explicit tracked + untracked diff-file — 100%
из 201 исполняемой строки. Пороги и `.coveragerc` не менялись.
Полный suite не повторялся: для Task 1 уже подтверждены 3187 passed/99.73%,
а этот этап меняет только локальную stdlib validation и SDK vendor policy.
`scripts.vendor_inventory` CLI и `git diff --check` — GREEN.

**Уточнение scope Task 2:** `ValidatedWheel` владеет immutable wheel/LICENSE
bytes; проверка hash предшествует ZIP, consumer не получает mutable path.
Чтение всех компонентов локального пути через directory descriptors с
`O_NOFOLLOW` отклоняет symlink manifest/LICENSE/wheel и каталоги, включая
подмену при обходе; regular-file/size guards исключают FIFO и unbounded reads.
ZIP проверяет 512 entries/32 MiB, CRC всех members, полное RECORD соответствие,
metadata/tag/license, unsafe paths, collisions, encrypted/symlink entries.
Inventory разрешает SDK README/manifest только при exact-byte SDK override.
LICENSE извлечён без изменения 11540 байт/CRLF; его pinned hash сохранён.
Сеть, pip, runtime lock и source selection в этом этапе не реализованы:
они принадлежат Task 3–5. Новых dependencies/изменений общего окружения нет.
`official_hash_verified=false`, `approvals=[]` сохранены. `yc` не добавлен;
он не хранится и не резервируется. Для iprotopy применяется notice-only состав
с factual missing LICENSE и fulfilled=false, без контакта/добавленного MIT.

### Task 3: Выбор официального источника или резерва

**Files:** изменить `scripts/sdk_wheel.py`, `test/test_sdk_wheel.py`,
`pyproject.toml`; создать `scripts/sdk_worker.py`, `test/test_sdk_acquisition.py`.
Stdlib worker необходим для жёсткого ограничения DNS/slow streaming и
подтверждённого завершения/очистки процессов; добавлен в действующую область
typecheck. Тесты acquisition вынесены отдельно от существующих integrity tests.
Установка, runtime lock, Make и CI wiring остаются задачами 4–5.

- [x] RED-тесты доступного индекса/скачивания, всех разрешённых причин резерва.
- [x] Добавить bounded transport, HTMLParser/URL validation, typed результат source/reason.
- [x] Добавить auto/vendored; WARNING с причиной и без credentials.
- [x] Проверить отказ без fallback при TLS certificate/hash/metadata/URL ошибке,
  malformed index/yanked/неоднозначной ссылке; отдельное approval_required при
  корректном официальном hash, отличающемся от reserve hash.
- [x] Проверить timeout/deadline/size limit, forced mode без скачивания SDK
  из T-Bank, bounded index probe, approved/unapproved pair и испорченный резерв.
- [x] Проверить повторное использование одобрённой точной пары, отказ после
  смены каждого hash/версии, пустой начальный approvals и отсутствие
  автоматического одобрения в downloader/lock generator; corruption не обходится.
- [x] Проверить end-to-end timeout при зависшем DNS и очистку worker/temp files;
  чтение ZIP и упаковка используют именно проверенные bytes, без path re-read.
- [x] Прогнать весь test/test_sdk_wheel.py и typecheck до следующей задачи.

**Проверки Task 3 (2026-10-12):** RED — pytest collection exit 2:
`scripts.sdk_worker` отсутствовал до реализации. Финальный GREEN — 475 tests:
полные SDK/inventory suites и затронутые Make/environment/archive tests,
20.18 секунд в native coverage run. `make lint` exit 0: Pylint 10.00/10,
оба Flake8 этапа без замечаний, Mypy 18 files; отдельный `make typecheck`
также GREEN. SDK и worker покрыты на 100%, затронутые helpers — 99.61%;
explicit tracked + untracked diff-file — 100% из 386 исполняемых строк.
`git diff --check` GREEN. Пороги, область сбора coverage и lint не менялись.
Отчёт покрытия этого этапа относится к затронутым helpers; полный suite
не повторялся, обязательный полный прогон остаётся в Task 9/final.

**Уточнение scope Task 3:** публичный `acquire_sdk` возвращает immutable
`Selection` с typed source/reason, digest индекса и `ValidatedWheel` bytes.
Рабочие файлы не передаются потребителю: bounded JSON переносит owned bytes,
проверенные повторно против локальной policy. Simple index поддерживает
полный HTML GitLab с заголовком пакета, пустой индекс, относительные HTTPS
ссылки и sdists как неселектируемые записи; выбирается только известный wheel.
Forced vendored выполняет index probe и approval, не скачивая remote SDK.
Timeout запроса ограничен 15 секундами, общий SDK budget — 60 секунд:
selection worker получает 40, recovery worker — не больше 15 и остатка budget.
Вложенный request worker наследует группу процесса и owned temporary root;
SIGTERM/при необходимости SIGKILL, wait и очистка проверены реальными
офлайн-процессами, включая зависший DNS stub и внешнюю отмену selection.
Небольшой атомарный маркер стадии сохраняет index digest; внешний timeout
разрешает резерв только на network stage, а timeout validation фатален.
Повторная проверка exact approval pair действует и после отмены download.
Одобрение не отменяет hash/metadata/LICENSE/CRC/RECORD checks.
Repo manifest/inventory, reserve/license pins и пустые approvals не менялись;
официальный hash независимо в сети не проверялся. Новых dependencies,
установки SDK, lock/Make/workflow изменений, live E2E, broker/cloud/deploy нет.
Условные iprotopy notices остаются явным фактом notice-only состава;
yc исключён из хранения и резерва, штатная загрузка обязательна.

### Task 4: Полный runtime lock

**Решение пользователя по yc (2026-10-12):** yc не хранить в репозитории,
резерв не требуется; статус `excluded-by-user`, прежнее ожидание решения о
binary/storage закрыто. При недоступности источника yc деплой завершается
ошибкой; LFS/Release binaries не добавлять. Исходный deploy-prod.yml не меняется.
Это решение не закрывает conditional notices iprotopy для публичного payload.

**Последнее решение пользователя по iprotopy (2026-10-12), отменяет предыдущий
вариант:** не обращаться к автору, не создавать issue/comment/contact и не
ожидать ответа. В Task 7 сохранить оригинальную METADATA с её license=MIT;
в THIRD_PARTY_NOTICES/iprotopy указать только исходного автора, версию и
неизменяемую точную source-ссылку. Стандартный MIT текст, предполагаемый
copyright/год не добавлять. Task 10 честно описывает отсутствие отдельного
upstream LICENSE. Контакта с автором не было. Решение пользователя не создаёт
upstream licence notice; fulfilled не становится true автоматически.
Task 7 сохраняет отдельную policy только для указанного iprotopy artifact:
declaration MIT есть, отдельный upstream permission/copyright notice не
подтверждён. Для остальных packages unfulfilled obligations блокируют payload.
Task 4 проверяет частный runtime lock, не публикует cloud payload и не меняет SDK.

**Files:** создать `runtime-lock.json`, `scripts/runtime_lock.py`,
`scripts/update_runtime_lock.py`, `test/test_runtime_lock.py`,
`test/test_update_runtime_lock.py`, `requirements-runtime-constraints.txt`,
`requirements-runtime-pins.txt`, `requirements-dev-tools.txt`;
изменить `Makefile`, `pyproject.toml`, `requirements.txt`,
`requirements-dev.txt`, `requirements-runtime.txt` (создать при отсутствии),
`scripts/build_yandex_archive.py`, `test/test_yandex_archive.py` (только default
selection новых вложенных runtime файлов и соответствующие fixture lists),
`test/test_make_commands.py` (затронутые тесты состава вложенных requirements).

- [x] RED-тесты полного graph/schema: точные versions, hashes, target,
  canonical digest, SDK identity и approved source artifacts.
- [x] Получить начальный lock из проверенного resolver report под CPython 3.12,
  сверить с working seed versions, учесть manylinux/cloud ABI и лицензии.
- [x] Генерировать pin/constraint/hash inputs единым renderer; обычная установка
  не меняет lock. Обновление выполняется отдельной Make-целью с review результата.
- [x] В той же задаче обновить default archive selection/fixtures для новых
  вложенных requirements; не оставлять сломанный archive contract до задачи 5.
- [x] Создать requirements-dev-tools.txt здесь, одновременно с generated
  requirements-dev.txt, а не ссылаться на файл, появляющийся только в задаче 5.
  Здесь же обновить затронутые tests в test/test_make_commands.py: проверять
  эффективный состав вложенных requirements, сохранив проверки инструментов,
  точных pins и единственного coverage-прогона.
- [x] Разделить runtime hash-mode и dev install: pip не принимает unpinned dev
  tools в общем --require-hashes вызове. Сохранить работоспособность публичных
  requirements-dev.txt через generated pinned runtime input без global hash-mode;
  проверенные Make flows устанавливают runtime по полному hash input отдельно.
- [x] Добавить tests для missing/extra/version drift/hash drift, несовместимого
  platform marker, malformed report, unapproved SDK hash и dev resolver conflict.
- [x] Проверить различие dev .venv и чистого cloud target: dev/bootstrap packages
  не являются runtime drift, но missing/изменённый locked package — ошибка.
- [x] Прогнать оба lock test modules и typecheck до GREEN; запретить неявное
  расширение artifact hashes при SDK mismatch или недоступности wheel.
  В этой же задаче прогнать затронутые tests из test/test_make_commands.py
  и test/test_yandex_archive.py до GREEN, не откладывая их до задачи 5.

**Проверки Task 4 (2026-10-12):** RED — collection exit 2 до создания обоих
lock helpers. GREEN — 560 новых/затронутых tests: lock/updater, SDK acquisition
и integrity, inventory, Make/environment/default archive. Strict `make lint`
exit 0: Pylint 10.00/10, оба Flake8 без замечаний, Mypy 20 files; отдельный
typecheck также GREEN. Покрытие затронутых helpers 99.55%; runtime_lock 99.49%,
updater 99.11%, builder 100%. Explicit tracked+untracked diff: 311 исполняемых
строк, 2 пропуска, 99.36%; пороги/coverage scope конфигурации не менялись.
`make check-runtime-lock` и `git diff --check` GREEN. Полный suite остаётся
обязательным в Task 9/final, здесь подтверждён только указанный affected scope.

Реальный supported pip install --dry-run JSON report под CPython 3.12/Linux
x86_64 разрешил два runtime roots при exact artifact constraints. Closure
содержит 24 пакета, совпадает с reviewed working seed по версиям и hashes;
число не зашито в validator. Используются manylinux2014/2_17, universal/abi3,
без native source build. Повторная генерация дала тот же canonical digest
`e7418637e952ec3101bdc666252f192a762bbcf31eefcfc712bbedf21e66a3a7`.
Lock содержит portable public report projection/digest, opaque Requires-Dist,
reviewed active edges из inventory; markers/constraints разрешает pip, своего
PEP 508 parser нет. SDK dependency metadata сверяется с owned validated bytes.

В отдельной чистой временной venv подтверждены supported install report hashes
всех 24 artifacts, pip check без conflicts и inspect metadata/target profile.
Dev graph допускает bootstrap/tools; exact runtime payload view не допускает
лишние distributions. Реальный dev tools dry-run под runtime constraints не
изменил ни одного runtime pin. Проверка выполнена повторно после усиления
metadata/marker guards; основное окружение/site-packages не менялись.
Сеть использовалась только для bounded package resolver/private verification;
broker/cloud/live E2E/deploy/production invocation отсутствовали.

**Уточнение scope Task 4:** hash-mode runtime и hashless dev экспорты разделены
здесь; public requirements-dev.txt сохраняет прежний набор tools/pins и не
включает hashed requirements.txt. Make setup до Task 5 остаётся существующим
raw pip bootstrap; verified helper installation, force-reinstall, receipts и
Make guards реализуются только в Task 5. Checker не выдаёт receipt и не
аттестует SDK source по одной версии. Новые SDK hashes допустимы только через
явное lock update после manifest approval точной пары; immutable bytes и
одинаковые SDK dependency metadata проверяются, approvals не создаются.
Начальный lock принимает только recovered hash; official independent hash
остаётся неизвестен. SDK bytes/manifest/LICENSE и deploy-prod.yml не менялись.
yc excluded-by-user; iprotopy notices ограничены последним решением выше,
без author contact/добавленного MIT текста и без ложного fulfilled=true.

### Task 5: Единая установка Make и CI

**Files:** изменить `Makefile`, `requirements.txt`, `requirements-dev.txt`,
`pyproject.toml`, `.github/workflows/pylint.yml`,
`.github/workflows/python-app.yml`, `.github/workflows/sandbox-e2e.yml`,
`scripts/make_environment.py`,
`test/test_make_environment.py`,
`test/test_make_commands.py`, `requirements-runtime.txt`,
`requirements-dev-tools.txt`; создать `scripts/install_dependencies.py`,
`scripts/package_wheels.py`, `test/test_package_wheels.py`,
`test/test_install_dependencies.py`; изменить `scripts/build_yandex_archive.py`
и `test/test_yandex_archive.py`
только для включения нового вложенного runtime requirements.

**Уточнение scope Task 5:** generated requirements и default builder уже
обновлены в Task 4; здесь они проверяются без повторного изменения. Добавлен
`test/test_autorepeater.py` только для изолированной archive fixture: копия
`requirements-runtime.txt` и ожидаемый состав default ZIP. Другие режимы
архива/деплоя не реализуются. Development helper получает точные проверенные
версии wheels из inventory seed (включая транзитивные tools), затем штатный
pip solver проверяет прежние tools requirements под runtime и seed constraints
без сети; raw public dev requirements сохраняют прежние ranges. Runtime pins
не меняются. PyPI acquisition использует уже закреплённые immutable artifact
URLs `files.pythonhosted.org`, без нового resolver/index-version выбора.
При HTTP404 отсутствует именно закреплённый artifact; другой release не
подбирается. Общий бюджет setup 300s включает запросы и pip; SDK получает
свои 60s только при достаточном остатке бюджета. Receipt принадлежит выбранному
интерпретатору (`sys.prefix`), создаётся после force-reinstall/graph checks.
Проверки текущей задачи используют внешний Python напрямую, без setup и
создания receipt в основной `.venv`; её environment/site-packages не меняются.

- [x] RED-тесты состава требований, interpreter выбора, argv/env/deadlines pip.
- [x] Разделить списки без изменения версий; исключить копипасту зависимостей;
  тестом контролировать совпадение SDK pin/lock/manifest и generated inputs.
- [x] Направить make setup в helper из .venv; сохранить VENV_PYTHON/PYTHON контракты.
- [x] Обновить совет для внешнего PYTHON в make_environment и добавить тест
  доступного bootstrap-helper без SDK/pytest/packaging и запуска из корня проекта.
- [x] Сохранить setup через Make в CI, checks, один coverage и лимит live setup.
- [x] Проверить network fallback SDK и каждого eligible Python wheel + WARNING;
  отсутствие reserve и solver/install/целостность ошибки остаются failure;
  extra-index из вложенных требований или pip окружения не просачивается.
- [x] Реализовать PyPI-first acquisition и per-package fallback до install;
  проверить доступный PyPI, status/timeout причины, missing/corrupted reserve,
  TLS/metadata/tag/hash failures, runtime lock и dev inventory version selection.
- [x] Проверить отсутствие silent downgrade/смены SDK source при ошибке другого
  package; dev reserve не вытесняет runtime, общие deadlines сохраняются.
- [x] Проверить locked runtime до и после dev install, installed receipt с
  lock digest/SDK source hash; E2E setup использует ту же процедуру.
- [x] Проверить SDK той же version с иными bytes: pip не должен пропустить
  reinstall. Проверить failure без success receipt и Make guard при missing/
  stale receipt/runtime drift; обновить изолированные guard/Make fixtures.
- [x] Прогнать новые/затронутые Make/archive/workflow тесты и typecheck до GREEN.

**Проверки Task 5 (2026-10-12):** RED — pytest collection exit 2 до создания
`scripts.package_wheels`/`scripts.install_dependencies`. Финальный полный
offline suite: 3687 passed, 18 warnings, exit 0, 293.31s. После последних
правок безопасной диагностики повторены 193 затронутых tests, exit 0,
91.22s; coverage append к полному suite, итоговое общее покрытие 99.70%.
Package helper 100%, installer 98.21%. Explicit diff-file включает tracked
PR diff от origin/master и все четыре новых Python files: 1471 исполняемых
строк, 8 пропусков, 99.46%, diff-cover exit 0. Новые файлы не пропущены как
untracked; пороги и `.coveragerc` не менялись.

Строгий lint payload через `scripts.make_offline ... lint` с внешним Python:
exit 0, Pylint 10.00/10, оба Flake8 этапа без замечаний, Mypy 22 files.
Типизация отдельно также GREEN; `scripts.runtime_lock` подтвердил прежний
digest `e7418637e952ec3101bdc666252f192a762bbcf31eefcfc712bbedf21e66a3a7`.
`git diff --check` GREEN. Make wrapper намеренно требует receipt; для этих
проверок не создавался искусственный receipt и не менялась основная `.venv`.
Make/guard/interpreter/CI contracts проверены offline tests.

Получение PyPI/SDK и установка проверены строгими autospec ports и DTO,
не внешней сетью. Hard timeout дополнительно подтверждён настоящим зависшим
offline subprocess с kill/wait и очисткой owned temporary output. Проверены
hash/metadata/tag/RECORD/TLS failures, причины WARNING, отсутствие reserve,
solver/install failures, force-reinstall, invalidated receipts и runtime drift.
Реальный сетевой package smoke остаётся Task 9. SDK bytes/manifest/пустой
approvals и original deploy-prod.yml не менялись; broker/cloud/live E2E,
production invocation и авторский contact не выполнялись. Task 6–10 не начаты.

### Task 6: Облачный архив с SDK wheel

**Files:** изменить `Makefile`, `scripts/build_yandex_archive.py`, `pyproject.toml`,
`test/test_yandex_archive.py`, `test/test_make_commands.py`.

- [x] RED-тесты default/vendored, аргументов, конфликтов флагов/неверных значений.
- [x] Добавить USE_VENDORED_SDK и CLI-флаг, сохранив цель и путь результата.
- [x] Формировать только архивные requirements; добавить wheel/LICENSE/manifest.
- [x] Проверить default без сетевых вызовов, исходники без изменений,
  неверный резерв до записи ZIP и сохранение предыдущего результата при ошибке.
- [x] Обновить все архивные fixture/source lists для requirements-runtime.txt;
  проверить прямой script/runpy/другой cwd и lazy helper imports.
- [x] Проверить отсутствие T-Bank во всей цепочке архивных требований и точные bytes wheel.
- [x] Прогнать archive/Make tests и typecheck до GREEN.

**Проверки Task 6 (2026-10-13):** RED до реализации — 9 targeted failures
с `unexpected keyword argument 'use_vendored_sdk'`; начальный полный
archive/Make прогон — 24 failed, 68 passed. GREEN новых/затронутых сценариев —
39 passed, 63 deselected; финальный полный offline suite — 3727 passed,
18 warnings, exit 0, 227.13s. После правок только test formatting/assertions
повторены соответствующие 7 tests, GREEN. Общее покрытие 99.69%; builder
98.84%; diff Task 6 — 59/60 исполняемых строк (98.33%), весь PR относительно
origin/master — 1522/1531 (99.41%). Все новые Python-строки находятся в
отслеживаемом builder; untracked Python-файлов задачи нет. Пороги и область
coverage не менялись. Единственная непокрытая строка builder добавляет
helper root в sys.path; её исполнение проверено отдельными изолированными
`-I -S` script/runpy subprocess без site-packages/PYTHONPATH.

Строгий lint через существующий `scripts.make_offline ... lint` с внешним
Python: exit 0, Pylint 10.00/10, оба Flake8 без замечаний, Mypy 23 source files.
Отдельный typecheck GREEN. Guard Make требует verified receipt, поэтому
проверки выполнены штатным offline helper без выдуманного receipt и без
изменения основной .venv/site-packages. Runtime lock checker подтвердил
прежний digest `e7418637e952ec3101bdc666252f192a762bbcf31eefcfc712bbedf21e66a3a7`;
`git diff --check` GREEN.

**Уточнение scope Task 6:** реализован только USE_VENDORED_SDK=0/1 и
`--use-vendored-sdk`. OFFLINE_ARCHIVE и матрица двух флагов принадлежат Task 7;
неподдерживаемый CLI-флаг offline сейчас отклоняется, не считается успешной
автономной сборкой. Default остаётся stdlib-only и сохраняет прежнюю запись
архива/requirements без probe. Существующие default fixture lists уже
обновлены в Task 4–5; новая vendored fixture дополнительно содержит lock,
inventory, SDK policy и полный комплект требуемых stdlib helpers.

Vendored builder до записи ZIP проверяет canonical lock, hash-mode runtime
input, forced SDK selection/approval и opaque dependency metadata. В ZIP
попадают owned immutable wheel/LICENSE bytes и проверенный manifest snapshot
в canonical JSON; исходные requirements/manifest не перезаписываются.
Archive requirements содержат только PyPI index, локальный SDK с hash и
вложенный locked runtime input. Staging ZIP рядом с результатом проверяется
на состав/дубликаты/CRC и точные подготовленные bytes, затем атомарно заменяет
результат; ошибки записи/проверки/replacement и KeyboardInterrupt очищают
staging и сохраняют прежний ZIP. Forced index-only probe проверен strict
autospec Transport: available и DNS fallback успешны, TLS и unapproved hash
mismatch фатальны; SDK download не выполняется. Тесты полностью offline.
Реальная сетевая установка распакованного ZIP остаётся обязательной Task 9,
не заявляется выполненной здесь. SDK bytes/manifest/approvals, исходные
requirements/runtime lock и deploy-prod.yml не менялись. Cloud/broker/live
E2E/deploy/production invocation отсутствовали; Task 7–10 не начаты.

### Task 7: Автономный runtime payload

**Files:** создать `scripts/cloud_dependencies.py`, `test/test_cloud_dependencies.py`;
изменить `Makefile`, `scripts/build_yandex_archive.py`, `scripts/package_wheels.py`,
`test/test_package_wheels.py`, `pyproject.toml`,
`test/test_yandex_archive.py`, `test/test_make_commands.py`.

- [x] RED-тесты platform gate, полного графа, download/install разделения и deadline.
- [x] Добавить OFFLINE_ARCHIVE, два bounded pip этапа и чистый staging.
- [x] Использовать canonical runtime lock и verify payload/receipt, не новый
  свободный resolve; включить lock digest и SDK artifact hash в manifest.
- [x] Переиспользовать per-package PyPI/reserve acquisition из задачи 5;
  проверить fallback eligible wheels и failure для GitHub-backed/no-reserve
  packages, без native source compilation или свободной смены версий.
- [x] Собирать root payload с dist-info/лицензиями и dependency manifest без installer requirements.
- [x] Реализовать isolated payload import smoke до atomic publication здесь;
  проверить missing/native import failure, неверный origin и timeout, без RPC.
- [x] Проверить все четыре сочетания флагов, malformed values, несовместимое окружение,
  unavailable PyPI/no binary, timeout, pip nonzero, path collision/symlink и cleanup.
- [x] Проверить отсутствие чужих site-packages и пакетов из списка dev requirements,
  не удаляя обязательные SDK dependencies; сохранить прежний ZIP при ошибке.
- [x] Проверить, что обязательные grpcio-tools/mypy-protobuf и их dependencies
  не вырезаны как dev-tools, а unsupported musl/PyPy отклоняются до сети.
- [x] Прогнать unit/archive/Make tests и typecheck до GREEN.

**Проверки Task 7 (2026-10-13):** исходный RED — collection exit 2:
`scripts.cloud_dependencies` отсутствовал. Новые/затронутые cloud/package/
archive/Make tests GREEN — 270 passed. При проверке реализации добавлена
регрессия вложенных setuptools vendored dist-info: RED — payload graph drift,
после исправления GREEN. Полный offline coverage suite — 3818 passed,
18 warnings, exit 0, 230.98s; финальный affected coverage append после этой
правки — 271 passed, exit 0. Общее покрытие 99.70%; cloud_dependencies и
package_wheels 100%, builder 97.96%; affected helpers вместе 99.58%.
Explicit diff-file включает новый untracked helper: Task 7 — 195/196
исполняемых строк (99.49%); весь PR от origin/master — 1714/1723 (99.48%).
Оба diff-cover exit 0; пороги и область coverage не менялись. Две строки
builder, добавляющие helper root в sys.path, проверены stdlib-only script/
runpy subprocess, но не измерены родительским coverage process.

Строгий lint через существующий scripts.make_offline с внешним Python —
exit 0, Pylint 10.00/10, оба Flake8 clean, Mypy 24 source files. Отдельный
typecheck, runtime lock checker и git diff --check GREEN. Canonical digest
остаётся `e7418637e952ec3101bdc666252f192a762bbcf31eefcfc712bbedf21e66a3a7`.
Проверки не создавали искусственный receipt и не изменяли основную .venv.

**Уточнение scope Task 7:** bounded SDK/per-package acquisition из Task 5
получает immutable verified wheels в отдельный wheelhouse. Один бюджет 300s
охватывает acquisition и pip download: последний работает локально с
--no-index/--no-deps/--require-hashes, CPython 3.12 и manylinux2014/2_17,
не выполняя второй сетевой resolver и не маскируя его ошибки резервом.
Далее независимый бюджет 300s ограничивает pip install в пустой target,
pip inspect и import smoke; все exact pins/hashes задаются из lock, pip
проверяет metadata constraints только по подготовленному wheelhouse.
--only-binary/--ignore-installed/--no-compile исключают source build и копию
пакетов текущей .venv. Native compilation/Docker не добавлены.

Root payload сохраняет все mandatory runtime packages, исходные METADATA,
лицензии/notices и data; вложенные vendored dist-info сохраняются как data,
верхнеуровневый installed graph обязан точно совпасть с lock. Сверяются
hashes wheels и сохранность файлов после pip; dependency-manifest.json
содержит receipt facts, lock digest, SDK source/hash, версии и immutable
artifact identities/лицензионные evidence. Manifest не публикует новый success
receipt в sys.prefix. Корневых installer requirements, wheelhouse и dev-only
distributions в offline ZIP нет. Конфликты корней приложения, unsafe paths,
symlinks, extra/missing/version/metadata/artifact drift фатальны.

Последнее notice-only решение пользователя для iprotopy применено строго
к 0.3.0/source commit 031ae0a6536f07964c9802dfe099efeb701e1c25: оригинальная
METADATA содержит upstream MIT declaration; THIRD_PARTY_NOTICES/iprotopy
содержит только автора Ruslan Sirazhetdinov, версию и exact immutable source
link. Дополнительный MIT текст/copyright/год не создаётся, контакта с автором
нет. Отсутствие standalone LICENSE и исходное fulfilled=false остаются
явными фактами manifest; chosen user_notice_only policy не является заявлением
о выполнении неподтверждённых upstream notice obligations. Это разрешённый
пользователем состав, а не автоматическое fulfilled=true. Для других пакетов
unknown/unfulfilled licence и отсутствующие материалы блокируют payload.
Certifi сохраняет исходную форму .py/cacert.pem, MPL licence и source-access notice.

До atomic ZIP publication выполняется реальный isolated -I/-S import process
по staging target: main/handler/SDK, grpc native cygrpc, grpc_tools native
protoc compiler, protobuf и обязательные runtime imports. Проверены namespace
origins, foreign origin/site-packages, missing/native loader failures,
ненулевой exit/таймаут/cleanup; клиенты и RPC не создаются. Python socket guard
не объявляется OS-level блокировкой native syscalls. Затем ZIP проверяется на
CRC/состав/точные bytes и атомарно заменяет прежний результат; отказ сохраняет
предыдущий ZIP. Все четыре режима и прямой script/runpy/other-cwd проверены.

PyPI/SDK source policy и eligible/no-reserve failures проверены offline strict
autospec ports и имеющимися acquisition tests; реальная сетевая сборка полного
locked graph/установка vendored ZIP остаются обязательной Task 9. Native loader
и isolation регрессии выполнены настоящими дочерними процессами без сети.
Это не проверка cold start в облаке. SDK bytes/manifest/пустые approvals,
generated runtime inputs, runtime lock и original deploy-prod.yml не менялись.
Новых dependencies, cloud/broker/live E2E/deploy/production вызовов нет.
Task 8–10 не начаты.

### Task 8: Ручной workflow с прежними production gates

**Files:** создать `.github/workflows/deploy-prod-reserve.yml`,
`test/test_deploy_reserve_workflow.py`; изменить `test/test_deploy_gate.py`
только при необходимости дополнительных проверок. Исходный deploy-prod.yml не менять.

- [x] RED-тесты dispatch-only, input allowlist, parity permissions/env/concurrency.
- [x] Добавить gate/deploy с выбором двух Make команд и точным SHA checkout.
- [x] Сохранить master restriction, три checks, повторный gate перед OIDC и текущие параметры.
- [x] Проверить, что PR/fork/неверный ref/устаревший SHA/неуспешный check не допускает OIDC/upload.
- [x] Проверить паритет ресурсов/Lockbox/SA/AUD и общий concurrency с обычным workflow.
- [x] Прогнать workflow/gate/deploy unit tests до GREEN без облачных запросов.

**Проверки Task 8 (2026-10-13):** исходный RED — 72 passed, 16 errors:
новый workflow отсутствовал. GREEN новых/затронутых workflow/gate/deploy/support
tests — 217 passed; после последних test-only правок повторены все 217 tests,
exit 0. Полный offline coverage — 3857 passed, 18 warnings, exit 0, 199.87s;
общее покрытие 99.70%, deploy_gate/yandex_deploy по 100%. Штатный diff-cover
для origin/master..HEAD с незакоммиченными tracked changes прошёл порог 90%
(вывод 99%, 9 missing lines). Новые Python-файлы Task 8 — только tests,
которые уже исключены действующим test/*; новых исполняемых production Python
строк нет. Пороги и область coverage не менялись. Строгий offline lint exit 0:
Pylint 10.00/10, оба Flake8 clean, Mypy 24 source files; отдельный typecheck
также GREEN. git diff --check GREEN.

**Уточнение scope Task 8:** новый dispatch-only workflow переиспользует
scripts.deploy_gate и scripts.yandex_deploy без изменений; оригинальный
deploy-prod.yml не изменён. Все cloud env скопированы из фактического файла:
256MB/60s, runtime SA ajelpbq7as5bpe499962, deployment SA, folder/cloud,
Lockbox, AUD и Object Storage совпадают. Concurrency deploy-prod/queue=max/
cancel-in-progress=false общая. Существующий официальный yc download остаётся
единственным способом получения yc, с отказом при недоступности; резерва нет.
Mode передаётся через env отдельного build-step, строгий shell case допускает
только vendored/offline. Build выбирает прежнюю Make-цель с SDK reserve и,
для offline, OFFLINE_ARCHIVE=1; точный SHA checkout и повторный gate перед
OIDC сохранены. Environment/альтернативная production auth не добавлены.

Тесты разбирают непосредственно оба workflow строгим stdlib-парсером их
ограниченного YAML-синтаксиса (неподдерживаемый синтаксис отклоняется), без
новой зависимости. Реальные shell case branches исполняются с recording
Make stub: неизвестные/пустые/injection значения не вызывают build. CLI gate
проверен для manual dispatch и обоих вызовов: unsafe repository/ref/event,
stale SHA, failure каждого required check, producer/SHA mismatch не выдают
ready=true; stale initial skip отличается от fatal check-once. Моки GitHub —
autospec(spec_set=True). Паритет jobs/permissions/env/order связывает эти
результаты с отсутствием доступа к OIDC/upload до успешного gate.
CodeGraph основного проекта использован до exploration; narrow snapshots
gate/deploy/support/tests/original workflow совпали с feature worktree.
Основная рабочая копия/.venv не менялись; cloud/broker/live E2E/deploy,
yc install и запуск Actions не выполнялись. Сетевой package smoke остаётся
Task 9; Task 9–10 не начаты.

### Task 9: Проверка установки и автономности готовых архивов

**Files:** создать `scripts/check_cloud_bundle.py`, `test/test_cloud_bundle.py`;
изменить `Makefile`, `pyproject.toml`, `test/test_yandex_archive.py`,
`test/test_make_commands.py`, `.github/workflows/python-app.yml`;
создать `.github/workflows/cloud-package-check.yml`,
`test/test_cloud_package_workflow.py`; изменить `scripts/cloud_dependencies.py`
для переиспользования payload guards и исправлений, обнаруженных real smoke;
создать `third_party/antlr4-python3-runtime-4.13.2/LICENSE.txt` и
`third_party/grpcio-tools-1.81.1/LICENSE` с неизменёнными upstream bytes;
создать `.gitattributes` с whitespace-атрибутом только для оригинального
grpcio-tools LICENSE (upstream trailing space сохраняется ради exact hash).
Обязательный этап и ручной workflow
используют одну цель; ручной job имеет отдельное имя Cloud package smoke,
не дублирует required Offline checks для того же SHA.

**Уточнение scope Task 9:** реальный smoke выявил отсутствие standalone
лицензий в locked wheels ANTLR 4.13.2 и grpcio-tools 1.81.1. Проверены все
24 runtime distributions в собственном verified feature окружении: у остальных
21 сохранены оригинальные licence files; iprotopy 0.3.0 остаётся исключением
по согласованной notice-only политике, без MIT текста/copyright и без изменения
fulfilled=false. Для двух wheel omissions полный исходный licence text из
точного upstream release добавлен в THIRD_PARTY_NOTICES соответствующего
пакета; METADATA, code/data и прочие licences не изменяются. Supplement
проверяет name/version, licence expression, repository/commit, exact inventory
evidence и SHA-256; неизвестный пакет или неразрешённые bytes остаются ошибкой.
ANTLR: commit cc82115a4e7f53d71d9d905caa2c2dfa4da58899, LICENSE.txt SHA-256
3db1fb3ee79a4b4f9918fc4d0f6133bf18a3cf787f126cd22f8aa9b862281c0c.
grpcio-tools: commit e84a8a2f04095f2772ba42a4abccde4f9243e75b, LICENSE SHA-256
58c86a3f2b5cca0ebdf9ed3e7f4e6dc38e3b38796d8c3dc92064c62a4e39d0dd;
сохранён весь upstream файл с его embedded licence text. Бинарники этих
GitHub-backed packages в Git не добавлены. Task 10 документирует эти факты
в пользовательских справочниках; другие изменения main worktree не перенесены.

**Границы package check:** required identity Offline checks сохраняется,
но после единственного make coverage выполняется отдельный сетевой шаг
make check-cloud-package. Ручной Cloud package smoke вызывает ту же Make-цель,
проверяет ожидаемый checkout HEAD и canonical lock digest, не подменяет
required job или Sandbox E2E. Vendored установка использует архивные
requirements с явным PyPI и локальным SDK, без T-Bank install source;
сборка сохраняет bounded official index-only probe и SDK mismatch approval.
Offline recheck не вызывает pip/acquisition. Общий isolated -I/-S import
helper проверяет main/handler/SDK, native extensions и namespace __path__
без вызова handler, создания клиентов или RPC. Python socket guard не
доказывает блокировку native syscalls; OS-level изоляция и cloud cold start
требуют отдельного механизма/приёмки и здесь не объявляются проверенными.

- [x] RED-тесты проверки bundle, без сети/торговли; определить пределы subprocess.
- [x] Реализовать isolated smoke на Python 3.12 (-I/-S): только stdlib и unpacked root;
  проверить imports main, handler, t_tech.invest, grpc, google.protobuf и origin
  файлов без вызова handler/создания клиентов; namespace packages проверять
  по __path__, не требовать от них несуществующий __file__.
- [x] Переиспользовать проверку payload из задачи 7; в checker добавить
  подготовку/установку vendored bundle и Make/workflow wiring без второй реализации imports.
- [x] Проверить обнаружение отсутствующей зависимости, неверного payload/requirements,
  утечки импортов из репозитория или .venv, timeout и ненулевого subprocess.
- [x] Добавить `make check-cloud-package` для реального bounded package smoke;
  в обоих workflows запускать именно Make, не повторять shell pipeline.
- [x] Собрать и проверить vendored и offline результаты в отдельной временной папке:
  vendored установить из архивных requirements с запрещённым T-Bank, offline
  импортировать без pip/загрузки зависимостей и без client/RPC calls.
  Не заявлять блокировку native syscalls без OS isolation. Проверка не использует токены.
- [x] Не подменять этим smoke live E2E, не менять смысл существующего Sandbox E2E статуса.
- [x] В Offline checks smoke обязателен без continue-on-error/skip-success;
  pytest suite запускается один раз через coverage. Отдельный ручной workflow
  без cloud/sandbox secrets, с проверкой конкретного checkout SHA/lock digest.
- [x] Сетевой download/install smoke запускать отдельным шагом после coverage,
  не из pytest и не внутри make coverage. Название Offline checks сохраняет
  required check identity, но документация явно указывает его сетевой этап.
- [x] Проверить parity обоих Make запусков, failure propagation, отсутствие
  подмены required check ручным job и соответствие обоих ZIP canonical lock.
- [x] Документировать границы сетевой проверки: socket mocks не доказывают
  блокировку native gRPC syscalls. Imports не должны создавать клиентов/делать
  RPC; более строгая OS-level изоляция — отдельный механизм с проверкой системных
  возможностей, без скрытого требования дополнительных прав к обычной сборке.
- [x] Прогнать новые unit tests, make lint, make coverage и git diff --check;
  отдельно проверить ещё не отслеживаемые Python-файлы в diff-покрытии;
  убедиться в >=90% общем/diff покрытии включая новые helpers.

**Проверки Task 9 (2026-10-13):** начальный RED предыдущего исполнителя —
collection exit 2, отсутствовал scripts.check_cloud_bundle. Диагностика
missing licence прошла RED 1 failure -> GREEN; ANTLR supplement — RED 5
failures -> GREEN; grpcio-tools — RED missing-text failure -> GREEN.
Финальные source/version/hash/expression/evidence/missing/symlink regressions
двух supplements — 18 passed. Полный make coverage в собственной verified
.venv — 3927 passed, 19 warnings, exit 0, 690.52s (Python tracer).
После последних source changes affected coverage — 267 passed, exit 0.
Финальный свежий full offline coverage существующим разрешённым внешним
интерпретатором с C tracer через scripts.make_offline — 3940 passed,
18 warnings, exit 0, 219.21s. Общее покрытие 99.55%;
cloud_dependencies 100%, check_cloud_bundle 93.46%. Explicit diff-file
включает новый untracked helper: Task 9 — 176/186 строк (94.62%),
весь PR origin/master — 1877/1896 (98.998%); оба diff-cover exit 0.
Пороги, suite и область coverage не менялись. Непокрытые строки checker
относятся к direct-script import fallback и __main__; их исполнение
подтверждено изолированными -I/-S script/runpy subprocess без site-packages.

Strict make lint exit 0: Pylint 10.00/10, оба Flake8 без замечаний,
Mypy 25 source files; отдельный make typecheck exit 0.
make check-runtime-lock и git diff --check GREEN. SDK wheel/LICENSE,
manifest/пустые approvals, generated requirements, canonical lock и
original deploy-prod.yml не изменены. Licence files проверены exact hashes;
оригинальный trailing space grpcio-tools сохраняется с Git-атрибутом только
этого файла. Code whitespace/lint/coverage gates не ослаблены.

Реальный approved make check-cloud-package завершился exit 0:
vendored build/install/report/isolated imports и offline build/ready-bundle
recheck/imports GREEN. JSON status=ok, checkout HEAD до коммита
331ee6d5fea6282f34c8941a8949b6fd6734edbc, lock digest
e7418637e952ec3101bdc666252f192a762bbcf31eefcfc712bbedf21e66a3a7.
Проверялись текущие task edits на этом HEAD; это не утверждение о проверке
ещё не созданного commit SHA. Sandbox блокировал PyPI; каждый необходимый
повтор выполнялся той же native Make-командой через require_escalated,
без альтернативных сетевых diagnostics. Два фактических licence failures
устранены оригинальными immutable upstream materials, без выдуманных
notices или blanket bypass. Все длительные native sessions завершены;
таймаут не засчитывался как успех. В собственной feature .venv использован
реальный verified receipt; основная .venv/site-packages не менялись.

Workflows проверены на shared Make, exact SHA, lock identity, dispatch-only
manual identity, failure propagation и отсутствие skip/continue-on-error.
Package networking вынесен после единственного coverage шага required
Offline checks. Pytest offline; broker/cloud/live E2E/deploy/production
invocation, author contact и yc reservation отсутствовали. Task 10 не начата.

### Task 10: Документация и подготовка PR

**Files:** изменить `README.md`, `vendor/README.md`,
`vendor/t-tech-investments/README.md` и этот план;
создать/дополнить `scripts/README.md` техническими командами только этой задачи;
создать `CHANGELOG.md`, если отсутствует, иначе дополнить раздел «Не выпущено».
Документацию из другой незавершённой ветки не переносить.

В разделе «Не выпущено» кратко записать резервную установку, новые режимы
облачного архива и переход Make на проверенное helper-окружение. Если файл
или раздел отсутствует, создать его только для изменений этого PR.

- [x] В основном README кратко описать SDK/резерв и дать ссылки на техническое описание
  и лицензию. Писать о текущем контракте без истории «раньше/теперь» и ссылок на автора текста.
- [x] Описать причины WARNING, accepted provenance caveat, лицензии, ограничения PyPI,
  различие reserve/offline и отсутствие автоматического reserve deploy.
- [x] Описать автоматический per-package резерв сохранённых Python wheels;
  yc исключён из хранения/резерва, только штатная загрузка с отказом при недоступности.
  Не обещать полный offline build без PyPI
  для GitHub-backed packages, чьи binaries в Git не хранятся.
- [x] Описать GitHub-source исключение: ready wheel не обязателен, автоматической
  сборки чужих source projects в этом PR нет; binary files generated cloud ZIP
  не означают хранение этих файлов в нашем Git.
- [x] Привести команды трёх режимов архива и make setup, требования Linux/Python,
  выбор режима ручного workflow и обязательные gates; описать внешний PYTHON
  и оба запуска реального package smoke, процедуру lock update и approvals.
- [x] Холодным чтением проверить команды/ссылки/оговорки и сохранность фактов;
  лицензии транзитивных пакетов и MPL-2.0 certifi проверять по фактически
  разрешённому составу автономного ZIP, сохраняя notices/исходную форму.
- [x] Проверить git diff --check и тесты документированных CLI/Make команд;
  отметить выполненные задачи и итоговые проверки в плане.
- [x] Подготовить один PR на русском; перечислить tests/ограничения/ручную приёмку.
  Коммит/push/создание PR выполняются на этапе реализации, не составления плана.

**Уточнение scope Task 10:** документация только этой feature-ветки, включая
vendor/README.md; другой незавершённый docs/readme-versioning diff не переносится.
Весь устаревший narrative плана о хранении/ручном резерве yc и добавленном
MIT/контакте iprotopy исправлен. Исходные inventory evidence не изменены:
yc storage=blocked/unknown описывает исходную проверку, хранение/резерв исключены;
iprotopy standalone LICENSE отсутствует, notice-only policy сохраняет fulfilled=false.
ANTLR/grpcio-tools supplements описаны как exact immutable upstream bytes
с source commits/hashes, не как новые licence texts. Код и бинарники не меняются.
Исполнитель проверяет команды/ссылки свежим чтением здесь; отдельное общее
specialist review выполняется оркестратором позднее, оно ещё не пройдено.
Draft PR создаётся после GREEN commit/push; merge/ready/E2E/deploy не выполняются.

**Проверки Task 10 (2026-10-13):** документальный RED: scripts/README.md и
CHANGELOG.md отсутствовали, vendor docs описывали fallback как будущий этап,
yc как ожидающий reserve/storage, iprotopy как требующий MIT текста/контакта.
После правок GREEN: 458 offline contract tests в Make/environment/archive/
lock/updater/installer/bundle/workflow/inventory suites, 16.91s, exit 0.
Нового исполняемого поведения нет; новые тесты-копии документации не добавлены.
Strict scripts.make_offline lint exit 0: Pylint 10.00/10, оба Flake8 clean,
Mypy 25 files; отдельный typecheck GREEN. Внешний разрешённый Python использован
без setup/receipt и без изменений основной .venv/site-packages.
CLI --help четырёх helpers и семь Make dry-run контрактов GREEN;
прямой default builder -I/-S из /tmp с абсолютным script/project-root/output
реально собрал ZIP без site-packages/сети, exit 0. make check-runtime-lock и
scripts.vendor_inventory GREEN; lock digest неизменён.
Проверены 34 локальные ссылки/якоря, immutable source links по inventory,
SDK hash/size/пустые approvals и supplemental licence hashes. git diff --check GREEN.
Холодное чтение исправило точный source iprotopy на irusland, различило
historical inventory storage и текущую yc exclusion policy, убрало обещание
сети у default builder и уточнило временный target checker вместо новой venv.
Полный suite/coverage и реальный package smoke здесь не повторяются: результаты
Task 9 выше относятся к её precommit tree, не финальному SHA ветки. Финализатор
должен повторить package smoke на итоговом committed SHA. Cloud cold start,
OS-level native syscall isolation и live E2E не проверены. Original deploy-prod.yml,
SDK/lock/licence bytes и checked scope/пороги сохранены.

**Публикация Task 10:** documentation commit `04b042f` успешно опубликован
в origin/ci/sdk-wheel-fallback. После проверки отсутствия существующего PR
для этого head создан один [draft PR #35](https://github.com/gorpsys/autorepeater/pull/35)
в gorpsys/autorepeater против master с русскими title/body, результатами
проверок, ограничениями и оставшейся ручной приёмкой. Review ещё не выполнено;
merge/ready/live E2E/deploy не запущены. Отметка фактического создания PR
фиксируется отдельно и публикуется в той же feature-ветке.

## Критерии приёмки

- SDK pin остаётся 1.51.0; официальный доступный источник выбирается раньше резерва.
- Все согласованные причины отказа дают исправный резерв и видимый WARNING.
- Для каждого сохранённого Python wheel PyPI-first fallback работает в setup
  и offline build; версия/bytes проверяются по lock/inventory. Без совместимого
  разрешённого reserve остаётся failure, SDK source не меняется побочно.
- Ошибки целостности, несовместимости и иных зависимостей не превращаются в success.
- make setup в CI и локально использует тот же контракт; основной индекс недоступен
  только для SDK, не блокирует PyPI-зависимости в reserve пути.
- Обычный облачный target/путь и исходный production workflow сохранены.
- Reserve ZIP устанавливается без T-Bank; offline ZIP работает без package installation.
- Неверная платформа отклоняется до сети; native wheels не зависят от более нового glibc хоста.
- Ручной workflow не ослабляет master/SHA/producer/checks/OIDC/concurrency gates.
- Wheel и license bytes сохранены; caveat о непроверенном официальном хеше явный.
- Unit tests не используют внешнюю сеть; real package smoke не обращается к брокеру.
- Lint строгий, coverage/diff >=90%, checks не отключены и suite не сокращена.
- Runtime versions/artifacts соответствуют canonical lock в setup/E2E и
  каждом ZIP; dev tools его не вытесняют, lock drift блокирует проверку.
- В Make/helper при доступном индексе и SDK hash divergence без одобрения нет
  install/package success; corrupted hashes не разрешаются одобрением.
  Default ZIP/runtime requirements.txt не выполняют helper probe, но не
  принимают SDK artifact вне утверждённых hashes lock. Raw hashless dev install
  не имеет этой гарантии и не подменяет проверенную установку Make/helper.
- Binary storage соответствует inventory: GitHub source нужной версии
  достаточно для исключения, единственное SDK-only исключение сохраняется.
  Licence obligations проверяются по фактическому ZIP; iprotopy notice-only
  сохраняет missing LICENSE и fulfilled=false, не объявляя их решёнными.
- Package smoke работает автоматически как часть required Offline checks
  и отдельно вручную; ручной результат не подменяет обязательный.

## Решения после проверки

### D1. Обязательность реального package smoke в PR

Пользователь выбрал оба варианта: обязательный этап existing Offline checks
и отдельный ручной workflow. Они вызывают одну `make check-cloud-package`
без токенов песочницы/облака. Отказ обязательного smoke блокирует existing
check; отдельный ручной прогон не подменяет его и не добавляет четвёртый
production gate. CI wiring находится в задаче 9.

### D2. Фиксация байтов SDK основного источника

Пользователь выбрал локальный hash для резерва, официальный — по индексу.
Первичный wheel с неверным индексным hash и резерв с неверным локальным hash
фатальны; одобрение не может отменить проверку целостности.
Отдельное состояние: оба hash корректны, но отличаются друг от друга при
доступном индексе. Тогда обязательно ручное одобрение, до него нет установки,
автоматического fallback или упаковки выбранного SDK. Это не обычная ошибка
целостности и не безусловный запрет легитимной upstream перепаковки.
Forced-vendored также делает bounded probe, не скачивая основной SDK.
Одобрение фиксируется в manifest один раз для точной пары hashes и версии;
повторные прогоны используют эту запись, изменённая пара требует новой.
Общий обход ALLOW_HASH_MISMATCH=1 не добавлять.

### D3. Версии транзитивных зависимостей и E2E

Пользователь выбрал полный runtime lock. Все обязательные runtime dependencies
фиксируются, включая транзитивный граф SDK, в общем lock для setup/E2E и
облачных режимов. Lock включает source identity/artifact checksums и проверку
совместимости с CPython 3.12/cloud ABI; отклонение установленного графа — ошибка.
SDK version остаётся 1.51.0, допустимые SDK artifacts согласуются с D2.
Повторная сборка не разрешает свободно диапазоны заново; изменение lock
выполняется явно через review и новый полный прогон проверок.
Development tools не могут обновить или вытеснить locked runtime packages.
Inventory выполняется в задаче 1, lock/генератор/fixtures — в задаче 4.
Формы SDK source/hash соответствуют D2, без неявного разрешения новых digests.

### D4. Применение резерва кроме SDK

Пользователь подтвердил рекомендованный автоматический fallback для всех
сохранённых Python wheels: WARNING, проверка hashes/metadata и runtime lock.
Остальные штатные packages по-прежнему поступают из PyPI в первую очередь;
при разрешённой недоступности выбирается проверенный reserve конкретного package.
Отсутствие подходящего reserve, corruption/TLS/solver/install errors остаются
failure. GitHub-source исключение и запрет автоматической компиляции сохранены.
Source selection находится в задаче 5, reuse для offline cloud download —
в задаче 7. yc исключён из хранения/резерва, стандартная загрузка обязательна
и завершается ошибкой при недоступности. Обычный deploy-prod.yml неизменён.

## Внешняя приёмка после реализации

В PR пользователь вручную запускает полный Sandbox E2E на конкретном SHA
после согласования. Он остаётся обязательным для merge, даже если offline smoke зелёный.
Новый workflow_dispatch доступен в штатном Actions UI после появления файла
в default branch; до merge проверяются wiring/gates и реальные архивы, но не
OIDC из feature branch. Не менять федерацию для обхода этого ограничения.

После merge ручная проверка резервного деплоя выполняется только при успешных
Lint/Offline checks/Sandbox E2E текущего master. Реальный деплой согласовать
отдельно: создание версии — production effect; результат подтверждать read-only
чтением версии/ACTIVE/параметров, без вызова рабочего handler и торгов.
Фактическую cloud-native import проверку можно провести отдельной временной
диагностической функцией с безопасным import-only handler, но создание/вызов
этого ресурса не разрешены текущим планом и требуют отдельного согласования.
До такой проверки не заявлять, что ACTIVE доказывает cold start offline payload.
Обычный автоматический деплой может уже опубликовать тот же SHA; повторная
ручная публикация нужна только для отдельной приёмки нового режима.

Когда основной индекс восстановится, независимо сверить официальный SHA-256
с восстановленным wheel. При совпадении обновить manifest/caveat проверяемым
источником; при несовпадении без уже записанного одобрения этой точной пары
приостановить установку/упаковку и разобрать происхождение/перепаковку.
Записанное одобрение совпадающей пары действует и после восстановления
индекса: повторного подтверждения не требуется. Поведение основного источника
определяется D2. Не переписывать резерв или подтверждение автоматически.
Обновления SDK требуют отдельного review версии, hash, metadata и лицензий.

## Исправления общего review, итерация 1 (2026-10-13)

Исходный review HEAD: `f41354cf684bf4ddef7d23ef9a4a36452fcb7dbe`.
Все 19 сообщений проверены, CONFIRMED; 14 отдельных исправлений, FALSEPOSITIVE
нет. Полная классификация с отдельными code/data/licence/rc/post контрпримерами
сохранена в [review record](review-20261012153543-sdk-wheel-fallback.md).
Исторические проверки Task 1–10 выше не переписаны.

- [x] Исправить все подтверждённые major и minor сообщения с отдельными
  regression cases, включая независимые успешные baselines и точные errors.
- [x] Проверить affected tests, полный offline suite, строгие lint/typecheck,
  общее и explicit tracked/untracked diff-покрытие без изменения gates/scope.
- [x] Выполнить настоящий verified setup собственного feature окружения и
  реальный Make package smoke обоих архивов; schema2 receipt не подделывать.

**Уточнение scope:** новый `runtime-file-lock.json` содержит только hashes/size
исходных файлов точных проверенных wheels, связан с canonical runtime lock
и artifact hashes. В Git не добавлены новые binaries. Генерация через
`scripts.install_dependencies --write-file-lock` использует bounded SDK/package
policy и проверку ZIP/RECORD, не устанавливает пакеты и не создаёт receipt.
Offline builder проверяет pip target, затем публикует точную исходную wheel
projection без pip-generated entrypoints/bookkeeping; checker сверяет весь
состав и bytes с независимым file lock до imports, даже если manifest переписан.
Schema2 installed receipt отдельно фиксирует actual installed file hashes и
membership после source/file-lock проверки. Старый schema1 недействителен.
Preflight штатным offline pip рассматривает retained packages и все proposed
runtime/dev wheels до эффектов и аннулирования прежнего receipt.

**RED:** исходный affected прогон 22 failed/464 passed: соседние releases
отклонялись, три подмены существующих code/data/SDK licence проходили, rewrite
cases ожидали file-facts API. Installer RED отдельно: proposed graph conflict
не обнаружен, installed file evidence API отсутствовал. Receipt контрпример
воспроизведён offline helper против исходного HEAD: full Make guard принимал
реально заменённый SDK source при прежней metadata; новый checker отвергает
старый receipt. Main worktree и его environment не менялись.

**GREEN:** 639 affected tests, затем 225 расширенных integrity/installer/bundle
tests; последний NUL/count fixture refinement отдельно 12 passed. Все path/count
fixtures проходят независимую complete RECORD проверку; errors точные.
Финальный полный offline coverage через feature `scripts.make_offline` с
разрешённым внешним C tracer: 4003 passed, 18 warnings, exit 0, 255.61s;
общее 99.58%. Installer 96.74%, package/SDK/cloud helpers 100%.
Explicit diff-файлы включают новый untracked Python: review 168/175 (96.00%),
полный PR 2012/2030 (99.11%), оба diff-cover exit 0. Пороги/scope не менялись.
Настоящий `make lint` собственного verified feature окружения exit 0:
Pylint 10.00/10, оба Flake8 clean, Mypy 25 files. `make typecheck` отдельно
exit 0. После последнего test-only refinement lint повторён, exit 0.
`git diff --check` и неизменность original deploy/SDK/lock/inventory/licences/
approvals/coverage/typecheck configuration подтверждены.

**Package evidence:** independent projection реально получена из 24 exact
artifacts/1226 файлов с прежним lock digest `e7418637e952ec3101bdc666252f192a762bbcf31eefcfc712bbedf21e66a3a7`.
Actual verified setup feature `.venv` завершился exit 0 и выдал schema2 receipt.
`make check-cloud-package PYTHON=.venv/bin/python CLOUD_PACKAGE_SHA=f41354cf684bf4ddef7d23ef9a4a36452fcb7dbe`
завершился exit 0: vendored install/report/imports и offline build/full file
recheck/imports, JSON status=ok. Sandbox отказ PyPI повторён той же native
командой require_escalated; все sessions завершены, timeout не считался pass.
Это smoke текущих precommit edits на указанном HEAD, не финального committed
SHA. Финализатор повторит smoke итогового SHA; live E2E/cloud cold start/deploy
остаются отдельно согласуемой приёмкой. Официальный SDK hash независимо не
подтверждён, approvals пусты; iprotopy notice-only/fulfilled=false и yc exclusion
сохранены. Draft PR #35 остаётся draft, ready/merge/PR creation не выполнялись.
Commit и immediate feature push фиксируются в progress после фактического успеха.

## Critical recheck, итерация 2: офлайн-checkpoint (2026-10-13)

Исходный HEAD: `f58326f31bb3b6b1b9464ee3022fc9f90ab232c4`.
Пять сообщений проверены и CONFIRMED, три отдельных исправления, FALSEPOSITIVE
нет. Подробные counterexamples и ограничения сохранены в
[review record](review-20261012153543-sdk-wheel-fallback.md).

- [x] Закрыть unchanged-RECORD additions в root namespace и nested module SDK,
  сохранить legitimate dev namespace branches, корректные caches и pip files;
  bounded nofollow enumeration получает actual installed distribution roots.
- [x] Проверить explicit file-lock generation для двух validated synthetic SDK
  artifacts с одинаковым filename и approved unequal hashes; хранить проекции
  раздельно по SHA-256, реальные approvals/SDK bytes не менять.
- [x] Отклонять NUL-containing orig_filename bundle ZIP до нормализации/чтения;
  regression подтверждает readable bytes и valid CRC повреждённых headers.
- [x] Завершить final offline coverage/diff и strict lint/typecheck
  validated checkpoint; фактические commit/push фиксировать в progress.
- [x] Подтвердить обязательный package result исправленного code SHA
  `2fa1e21d1944e5c0ad47ddb102689fa8c6c03b2a`: штатный CI run `38338087491`
  завершён SUCCESS, включая Network package smoke. Локальный smoke этого SHA
  не выполнен; remote CI proof не выдаётся за локальный запуск.

**Уточнение scope:** Task 1–10 и исторические результаты не переписываются.
Hash-separated staging применяется к explicit file-lock update; ordinary setup
оставляет единственный выбранный SDK. Полная проекция всех locked artifacts
либо явный отказ; unavailable required artifact не считается completed.
Тестовая approval pair создана только в DTO fixture, не в repo manifest.
Все прежние integrity/preflight/PEP440/full graph/resource checks сохраняются.
Новых binaries/dependencies/source build, main worktree/environment changes,
PR creation/ready/merge/rebase, live E2E/broker/cloud/deploy/secret calls нет.

**Порядок приёмки по последнему указанию пользователя:** parent interruption
отменил только ожидание local network authorization, не является user denial.
Sandbox pypi.org отказ записан; два interrupted require_escalated ожидания не
создали native child/session. После GREEN offline checks разрешён промежуточный
commit с immediate feature push. Текущий package smoke остаётся незавершённым;
штатный обязательный CI не отменён и не является обходом local permissions.
Review phase не объявляется завершённым до подтверждённого package result;
parent выполняет следующий critical recheck/finalizer для итогового SHA.

**GREEN offline checkpoint:** final full source suite 4030 passed, 18 warnings,
257.56s, exit 0. Security-boundary tests/affected coverage append 283 passed,
42.63s, exit 0; после full run source не менялся. Итоговое общее coverage 99.45%,
installer 95.58%; explicit review diff 126/137 (91.97%), весь PR 2136/2163
(98.75%), оба diff-cover exit 0. Untracked Python files нет. Strict make lint
окончательного дерева exit 0: Pylint 10.00, оба Flake8 clean, Mypy 25 files;
отдельный typecheck exit 0. Последняя test-only правка проверена 3 passed.
git diff --check/canonical lock checker GREEN; digest прежний e741...a3a7.
Разрешённый внешний C tracer использован feature helper, main environment
не менялся. Feature Make guard принял настоящий schema2 receipt; schema не
менялась, новый setup/receipt fabrication не выполнялись. Публикация после
этих checks разрешена как intermediate checkpoint, не completion review phase.

## Critical recheck, итерация 3: содержимое runtime caches (2026-10-13)

Исходный HEAD: `5f18439a27d4a09031a770e2c88460b9a0298f19`.
Оба MAJOR сообщения implementation/quality CONFIRMED, одно исправление;
counterexample и подробные ограничения сохранены в
[review record](review-20261012153543-sdk-wheel-fallback.md).

- [x] Воспроизвести real CPython 3.12 timestamp-preserving cache replacement
  при прежних source/RECORD/receipt; исправить guard и setup attestation.
- [x] Проверять caches только после независимой аутентификации source,
  без десериализации untrusted marshal/code и без исполнения cache validation.
  Сохранить native import-produced caches opt 0/1/2, cache invalidation modes,
  generic namespace/shadowing checks, bounded reads/compile и safe failures.
- [x] Проверить final stable offline suite, affected tests, strict lint/typecheck,
  total/review/PR coverage и whitespace; подготовить validated checkpoint.
- [x] Подтвердить required package smoke и CI исправленного code SHA
  `2fa1e21d1944e5c0ad47ddb102689fa8c6c03b2a`: Lint и Offline checks SUCCESS,
  Network package smoke в run `38338087491` SUCCESS. Локальный сетевой smoke
  не выполнен. CI последующего documentation-only HEAD проверяется отдельно
  перед финализацией, его точный SHA и результаты фиксируются в PR.

**Уточнение scope:** existing membership policy остаётся, но разрешённое cache
имя больше не подтверждает исполняемые bytes runtime. Для существующих caches
guard сравнивает непрозрачные bytes с сериализацией compilation уже проверенного
source: bytes read mode, absolute filename (включая nested code constants),
dont_inherit=True, optimize=0/1/2. Проверены magic и timestamp/size/source-hash
headers, точное payload и отсутствие хвостовых bytes. marshal.load/loads не
вызываются; сериализуется только trusted compiled object. Source hash/file lock
проверяется первым даже при forged receipt. Ограничения: source 8 MiB,
serialization 32 MiB, nofollow read ожидаемая длина+16, общий deadline 30s.
Компилируются только source с существующим cache; packages из source не строятся.
Существующий schema2 receipt и runtime file lock не переписываются.

**RED:** 1 failed/1 passed, native exit 1: isolated -S CPython 3.12.3 subprocess
реально исполнил harmless replacement fixture при прежнем timestamp/size header,
а исходный check_environment не вызвал InstallError. Source/RECORD/receipt
остались побайтно прежними. После fix и guard, и attestation отклоняют подмену.

**GREEN offline checkpoint:** fresh stable full suite 4070 passed, 18 warnings,
272.18s, native exit 0; affected installer/integrity 144 passed, 21.78s, exit 0.
Native cache/authentication cases 42 passed; последние exact import/replacement
integration cases 5 passed, 0.93s. Общее покрытие 99.45%, installer 95.95%;
explicit review diff 37/37 (100%), весь PR 2134/2160 (98.80%), оба diff-cover
exit 0. Strict actual make lint: Pylint 10.00, оба Flake8 clean, Mypy 25 files;
отдельные make typecheck/check-coverage guard exit 0, настоящий feature receipt.
Внешний main C tracer разрешён через feature helper; main environment не менялся.
885 standard feature caches дали ноль byte mismatches, compilation comparison
1.386s; native opt 0/1/2, encoding/newlines/future/nested code и hash modes
проверены отдельно. Scope/пороги/конфигурация проверок прежние.

Review git diff --check clean. Plain full PR check сообщает только CRLF
оригинального recovered SDK LICENSE; default whitespace rules с cr-at-eol дают
exit 0, точные upstream bytes и Git attributes/config не менялись. Canonical
lock checker exit 0, digest прежний e741...a3a7. SDK binary/licence, manifest/
actual approvals, inventory/file-lock, iprotopy notice-only, yc exclusion и
original deploy-prod.yml не изменены. Новых dependencies или app changes нет.

Публикация разрешена как промежуточный offline checkpoint. Commit/immediate
feature push фиксируются в progress только после фактического успеха. Required
network package result нового/final SHA остаётся незавершённым; предыдущий smoke
не подменяет новый. Existing mandatory PR CI действует самостоятельно; ad-hoc
dispatch, PR create/ready/merge, live/broker/cloud/deploy/credentials вызовов нет.
