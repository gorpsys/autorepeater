# Проверка плана резервного SDK

Дата: 12 октября 2026. Проверен
[план](../plans/20261012153543-sdk-wheel-fallback-implementation-plan.md)
против постановки задачи, согласованных ответов и кода master
`ba3668702bf2d5f46f6445826c5398615a5e05f1`.
Реализация/торговля/деплой/E2E не запускались; изменены только документы.

## Результат

Обновление после ответов пользователя: D1 принят в обоих вариантах (обязательный
CI этап плюс ручной workflow), D2 — отдельные локальный/официальный хеши с
ручным одобрением расхождения, D3 — полный runtime lock. Затем расширен scope
на хранение всех нужных разрешённых к распространению бинарных артефактов.
Последующая поправка исключает проекты с исходниками нужной версии на GitHub:
готовые binary assets не обязательны. Автоматическая source сборка не входит
в текущий PR; разработчик допускает ручное восстановление с дополнительными усилиями.
Одобрение подтверждено один раз для точной пары hashes и версии SDK,
с записью в manifest после отдельного явного подтверждения конкретных файлов.
Таблица содержит находки предыдущих и повторной проверок; технические
исправления внесены в план. Q1 закрыт подтверждением рекомендации: все
сохранённые Python wheels получают автоматический per-package fallback,
с WARNING/проверками; yc остаётся ручным, обычный deploy workflow не меняется.

Первый вариант не был непротиворечивым. Технические исправления внесены
непосредственно в план. D1–D3 подтверждены ответами пользователя; предположения
не переименованы в согласованные требования. Внешняя приёмка cloud-native
исполнения остаётся отдельной и не выдается за проведённую.

## Замечания

| ID | Критичность | Что обнаружено и почему | Источник | Статус |
| --- | --- | --- | --- | --- |
| P01 | Высокая | Сетевой package smoke молча добавлялся в required Offline checks. Это новый blocking network stage, хотя пользователь согласовал прежние gates | План, прежняя задача 7; [workflow](../../.github/workflows/python-app.yml) | D1 подтверждён: обязательный этап и ручной workflow |
| P02 | Высокая | Основной SDK принудительно сравнивался с хешем восстановленного wheel, хотя подтверждено только доверие резерву. Валидная upstream перепаковка той же версии стала бы ошибкой | План, раздел установки | D2 подтверждён: отдельные hashes и ручное одобрение расхождения |
| P03 | Высокая | E2E для SHA не подтверждает exact dependency graph автономного ZIP: повторный resolver и manylinux filter могут выбрать другие версии | [requirements](../../requirements.txt), SDK METADATA с диапазонами | D3 подтверждён: полный runtime lock |
| P04 | Высокая | Import smoke до публикации требовался и от vendored ZIP, хотя тот содержит только SDK wheel и требует последующей установки остальных пакетов. Требования несовместимы | План, режимы ZIP и прежняя задача 7 | Исправлено: разная приёмка двух режимов |
| P05 | Высокая | Общий запрет dev-tools мог удалить grpcio-tools/mypy-protobuf/setuptools, обязательные через iprotopy. ZIP перестал бы содержать полный declared runtime graph | METADATA установленного SDK/iprotopy/grpcio-tools | Исправлено: исключать dev requirements, не обязательные dependencies |
| P06 | Высокая | Guard советует выбранному внешнему PYTHON старую raw pip команду. Этот путь не использует новый fallback, поэтому после ошибки пользователь снова обращается к T-Bank | [make_environment.py](../../scripts/make_environment.py), строки 29–33; соответствующий test | Исправлено: helper для выбранного Python и тест инструкции |
| P07 | Средняя | Builder вызывается как прямой script. Без дополнительного решения `from scripts...` ищется не из project root; fixtures копируют только Makefile и builder. Новые imports/requirements ломали бы default CLI | [Makefile](../../Makefile), archive target; [tests](../../test/test_yandex_archive.py), test_make_archive_without_python_alias | Исправлено: lazy imports, direct/runpy/cwd tests, обновление fixtures |
| P08 | Средняя | Socket timeout и проверка monotonic между вызовами не ограничивают заблокированный DNS/slow streaming. Обещанные 60/300 секунд не гарантировались | План, SDK acquisition | Исправлено: supervised SDK worker, end-to-end timeout и cleanup |
| P09 | Средняя | Linux x86_64 + Python 3.12 не означает CPython/glibc. Musl/PyPy могут удовлетворять неполному guard, но не принимать manylinux CPython wheels | План, platform guard; PyPI wheel tags | Исправлено: implementation/glibc checks до сети |
| P10 | Средняя | Валидация path с последующим повторным чтением могла упаковать уже другие bytes. Период владения downloaded temp wheel не был определён | План, SDK validation/packaging | Исправлено: один validated byte snapshot, lifecycle temp и symlink checks |
| P11 | Средняя | Общая оговорка о лицензиях не раскрывала MPL-2.0 certifi; первоначальное правило остановки при любых дополнительных обязательствах фактически блокировало допустимую redistribution | METADATA certifi 2026.7.22 и PyPI version JSON | Исправлено: отдельный inventory, retention/source obligations; при неясности не распространять |
| P12 | Средняя | Read-only ACTIVE проверка доказывает публикацию версии, но не её Python imports/cold start; isolated Linux smoke не является реальным cloud execution | [yandex_deploy.py](../../scripts/yandex_deploy.py), workflow; официальная документация runtime | Ограничение закреплено; внешняя приёмка отдельно |
| P13 | Средняя | HTMLParser не отклоняет автоматически malformed/login HTML или JSON как текст. Пустой результат мог ошибочно превратиться в «версия отсутствует» и резерв | План, index parser | Исправлено: endpoint/media-type/формат, ambiguity/yanked и отрицательные тесты |
| P14 | Средняя | Python socket mocks не блокируют native gRPC syscalls; формулировка «сеть запрещена» давала более сильную гарантию, чем выбранный механизм | План, import smoke | Исправлено: границы доказательства и отсутствие client/RPC calls; OS isolation не скрытое local build требование |
| P15 | Высокая | Расширение binary storage осталось преимущественно inventory: не было явной задачи добавления остальных eligible artifacts. Одновременно PyPI failure объявлен фатальным и допускается repo reserve для других packages | План, границы набора, installer, задачи 1–2/5/7 | Добавление файлов включено в задачу 2; Q1 подтверждён как D4: автоматический fallback Python wheels в задачах 5/7, yc вручную |
| P16 | Высокая | Pip может пропустить установленный SDK той же version; receipt выбранного wheel тогда не доказывает, что установлены его bytes. Старый receipt мог пережить частичный install | [Makefile](../../Makefile), setup; план, install helper | Добавлены force-reinstall/no-deps полного locked closure, аннулирование receipt перед изменением и запись после проверок |
| P17 | Высокая | Одобрение нового index hash не определяло его отношение к полному lock. Возможны установка незакреплённого SDK или ошибочное объявление одобрения достаточным | План, «Одобрение SDK» и D3 | Approval отдельно от явного lock update; metadata/closure/checks обязательны; generator не создаёт approvals |
| P18 | Средняя | Задача 4 генерировала requirements-dev.txt с файлом, создаваемым только в задаче 5. Задача 7 требовала smoke до публикации, но checker появлялся в задаче 9 | План, задачи 4/5/7/9; [archive fixtures](../../test/test_yandex_archive.py:117) | dev-tools создаётся в задаче 4; payload smoke в задаче 7, checker задачи 9 его переиспользует |
| P19 | Средняя | Лимит 2 MiB downloaded bytes не ограничивает ZIP expansion, повреждённый RECORD или небезопасные entries | План, wheel validation | Добавлены uncompressed/count limits, CRC/RECORD/path/symlink проверки до pip и отрицательные тесты |
| P20 | Средняя | «При любом mismatch нет установки» шире реального контракта: неизменный основной workflow устанавливает requirements, не вызывает approval helper. Нынешний trusted-host также отключает TLS validation | [requirements](../../requirements.txt), [deploy](../../.github/workflows/deploy-prod.yml), план | Уточнены Make/helper/default/raw pip границы; runtime export ограничен approved lock hashes, raw hashless dev install не выдаёт receipt; trusted-host не генерируется |
| P21 | Средняя | Обязательный сетевой smoke можно было включить внутрь offline pytest/coverage и нарушить обещание тестов без сети | [workflow](../../.github/workflows/python-app.yml), план, задача 9 | Отдельный обязательный Make step после coverage; pytest остаётся без внешней сети, required job name сохраняется |
| P22 | Средняя | Make guard проверяет только наличие модулей: локальный e2e/test мог запуститься в устаревшей .venv без полного runtime lock | [guard](../../scripts/make_environment.py:23), план | Добавлены версии/closure/receipt проверки и tests stale/missing state в задачу 5; прямой боевой CLI не меняется |

## Подтверждённые факты

- Make setup использует VENV_PYTHON для создания .venv и затем её Python;
  PYTHON для остальных целей не является bootstrap override. План уточнён
  без изменения этого поведения.
- В основной и новой рабочих копиях совпадают bytes Makefile, builder,
  make_environment, deploy_gate и yandex_deploy. Доказательства кода относятся
  к тому же master, от которого создана ветка.
- Guard проверяет `t_tech`, а не legacy namespace tinkoff. Smoke уточнён:
  main, handler, t_tech.invest, grpc, google.protobuf.
- Установленный runtime graph включает 24 distributions; число описывает
  только изученное окружение, не является будущим hardcoded invariant.
  SDK требует iprotopy, который требует grpcio-tools/mypy-protobuf/ANTLR;
  обязательные dependencies нужно сохранять.
- PyPI version JSON подтвердил wheels iprotopy 0.3.0, antlr4-python3-runtime
  4.13.2, grpcio 1.84.0, grpcio-tools 1.81.1 и protobuf 6.33.6.
  Для native пакетов есть CPython 3.12/abi3 manylinux2014 x86_64 варианты.
  Гипотеза об отсутствии подходящего wheel среди этих пакетов не подтвердилась.
  Это не полный resolve/download/install будущего графа.
- certifi 2026.7.22 имеет MPL-2.0; SDK — Apache-2.0. Сохранение одной Apache
  licence не закрывает остальные пакеты. Полная юридическая приёмка всего
  разрешённого графа не выполнена.
- [Документация зависимостей Yandex](https://yandex.cloud/ru/docs/functions/lang/python/dependencies)
  прямо говорит о pip install в корне проекта: relative wheel path обоснован.
- [Документация runtime Yandex](https://yandex.cloud/ru/docs/functions/concepts/runtime/index)
  указывает Ubuntu 22.04 LTS/Linux 5.15 и поддерживаемый python312.
  Cloud native import и поведение installer при отсутствии requirements.txt
  не проверялись реальным созданием/вызовом функции.

## Вопросы пользователю

Вопросы D1–D3 ниже относятся к первоначальной проверке и уже отвечены.
Процедура одобрения SDK тоже подтверждена: один раз для точной пары/версии.
Q1 тоже отвечен: автоматический fallback для сохранённых Python wheels при
разрешённой недоступности PyPI (с WARNING/целостностью/lock), D4 в плане.
Применение yc остаётся ручным; основной production workflow не изменяется.
Открытых вопросов по выбранной политике нет. Фактические inventory/licence/
lock результаты ещё предстоит получить по задачам, не считать их проверенными.

1. **D1:** реальный download/install smoke делать отдельным ручным workflow
   через make check-cloud-package (рекомендация) или обязательным этапом
   existing Offline checks, способным блокировать PR из-за дополнительного
   сетевого отказа? Три нынешних production gates в ручном варианте сохраняются.
2. **D2:** фиксировать hash обоих источников на те же bytes (рекомендация для
   reproducibility) или проверять этот hash только у резерва, а официальный
   wheel — по hash его индекса/metadata? Первая политика намеренно отвергает
   перепакованный upstream 1.51.0 даже при сохранении API/version.
3. **D3:** оставить нынешние ranges и явный остаточный риск отличия ZIP от
   E2E-окружения (рекомендация для узкого PR) или сейчас расширить PR полным
   runtime lock для CPython 3.12, общим для setup/E2E/новых архивов?

Ответы D1–D3 уже внесены в план до реализации. Разрешение на создание/вызов
временной import-only cloud function запрашивается отдельно при внешней
приёмке; рабочий handler не вызывается для такой проверки.

## Принятое независимое cold-reader review

12 октября 2026 пользователь принял три замечания независимого холодного
чтения. В плане исправлены: P2 — одобрена только процедура SDK approval,
конкретные пары не одобрены и начальный approvals пуст; P2 — изменение
test/test_make_commands.py и прогон затронутых tests включены в задачу 4
одновременно с разделением requirements; P3 — в задаче 10 предусмотрены
создание CHANGELOG.md при отсутствии и запись в разделе «Не выпущено»
только о текущем PR, без переноса чужой документации.

Десять заголовков приведены к формату `### Task N: <title>` для последовательной
реализации. Порядок задач и состояния чекбоксов сохранены. Это подготовка
документации: реализация задач, включая Task 1, ещё не начата.
