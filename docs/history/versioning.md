# Ретроспективное версионирование

История назначения тегов, состав выпусков и описания коммитов.
Краткие записи выпусков — в [CHANGELOG](../../CHANGELOG.md).
Действующие правила выпуска — в [техническом README](../../scripts/README.md#версии-и-выпуск).
Результаты калибровки вынесены в [исторический отчёт](../reports/2026-10-06-calibration.md).

## v5.2.1: резервный offline-деплой, 2026-10-16

[PR #39](https://github.com/gorpsys/autorepeater/pull/39) влит в 09:52:57 UTC:
`0492181575861adffc0c830c5f6f5e437b235db2`; аннотированный тег `v5.2.1` создан автоматически.
PATCH обвязки: оба деплоя выбирают функцию по постоянному ID `d4e6d0l53hkivgrmfano`,
gate учитывает динамическое имя master E2E по пути workflow. Переименование каталога
`tinkoff` → `my-catalog` не влияет на выбор; функция не переименована.
Lint/offline зелёные; PR E2E `38879802838` — все 17 сценариев, master E2E
`38883490401` — SUCCESS; полное завершение и cleanup обоих прогонов подтверждены.
Обычный деплой `38885601300` после OIDC, preflight ID функции и загрузки ZIP упал при сборке (exit 1); `invalid_argument` — CI-обёртка ошибки build.
Источник: build log из облачной консоли, предоставленный пользователем 16.10.2026: SDK-индекс `opensource.tbank.ru` недоступен из cloud builder (тайм-аут соединения 15 секунд, все 5 retry).
`pip3.12 install` завершился `No matching distribution found` для `t-tech-investments==1.51.0`; отсутствие версии или несовместимость с Python этим не доказаны.
По отдельному согласованию выполнен [резервный offline-деплой](https://github.com/gorpsys/autorepeater/actions/runs/38888894486)
того же `0492181`: SUCCESS, обновлён в 10:43:03 UTC.
Read-only проверка YC в 10:50 UTC подтвердила новую latest-версию
`d4ep3iiual5v3b312vpk` — ACTIVE, создана в `2026-10-16T10:42:42.510Z`:
`python312`, `handler.handler`, 268435456 байт (256 МиБ), тайм-аут 60 секунд.
Это подтверждение публикации, не Python cold start: production handler не вызывался.
Live handler/FX25-приёмка ещё не выполнена; этапы 7/8 валютной фичи не завершены.

## Production-деплой через OIDC

[PR #29](https://github.com/gorpsys/autorepeater/pull/29),
[PR #30](https://github.com/gorpsys/autorepeater/pull/30),
[PR #31](https://github.com/gorpsys/autorepeater/pull/31),
[PR #32](https://github.com/gorpsys/autorepeater/pull/32),
[PR #33](https://github.com/gorpsys/autorepeater/pull/33) и
[PR #35](https://github.com/gorpsys/autorepeater/pull/35) образуют один связанный
PATCH-блок. PR #35 добавил резерв SDK и автономную сборку, необходимые для
проверки деплоя при недоступном индексе T-Bank; отдельного изменения
функциональности приложения в нём нет.
Условие завершения выполнено 2026-10-13: согласованный offline production-деплой
из `master` через GitHub Actions прошёл с обменом OIDC → IAM. Это не локальный
деплой и не успешный повтор обычной индексной сборки.
Выпуск — `5.1.2`; тег `v5.1.2` опубликован 2026-10-14. SHA завершения блока:
`f430b0fa6921ec56b8181a5084c9ee0297c57a98`, merge PR #35.
[Свидетельства проверки](2026-10-13-sdk-deploy.md) содержат ссылки на Actions,
ID ACTIVE-версии и границы подтверждённого результата.

- Деплой проверенного master через OIDC и Object Storage с сохранением
  настроек версии функции.
- Исправление идентификации запуска deploy-gate и прогресс live E2E.
- Раздельные audience GitHub OIDC и Yandex IAM exchange.
- Изолированный временный профиль yc с endpoint, без credentials в профиле.
- IAM через `YC_IAM_TOKEN`, диагностика и проверка runtime SA.
- Резерв SDK 1.51.0, полный runtime lock и проверенная установка окружения.
- Vendored/offline архивы, обязательный package smoke и ручной резервный workflow.

Тег указывает только на последний из шести коммитов блока;
остальные перечислены в общей истории ниже без отдельных тегов.

## Выбор функции по ID (PR #39)

В [PR #39](https://github.com/gorpsys/autorepeater/pull/39) переход основного
и резервного деплоя на `FUNCTION_ID` входит в тот же PATCH `5.2.1`,
что и изменения проверки Sandbox E2E. Пользовательский CLI и торговая логика
не меняются; технический интерфейс `scripts.yandex_deploy` требует ID
существующей функции вместо `FUNCTION_NAME`.

2026-10-16 в 08:45 UTC read-only запрос YC подтвердил три поля:

| Поле | Значение |
| --- | --- |
| `id` | `d4e6d0l53hkivgrmfano` |
| `name` на момент проверки | `python-function` |
| `folder_id` | `b1gdgrp5bnth11phova8` |

Команда завершилась с кодом 0; вывод ограничен этими полями:

```bash
yc --endpoint api.cloud.yandex.net:443 serverless function get \
  --name python-function --folder-id b1gdgrp5bnth11phova8 --format json \
  | jq '{id,name,folder_id}'
```

Источник ID — метаданные существующего облачного ресурса, не тестовая fixture
и не ID версии из прежнего отчёта. Проверка не создавала версий,
не переименовывала ресурс и не вызывала handler. Production-деплой этого
изменения не выполнялся. Имя функции и имя каталога не используются
для выбора цели в обновлённых workflow и helper.

## Область ретроспективного версионирования

Первоначальные версии назначены 2026-10-10 по 97 коммитам, достижимым из master
на SHA `ba3668702bf2d5f46f6445826c5398615a5e05f1`.
Включены коммиты влитых веток и merge-коммиты; невлитые ветки не включены.
92 коммита образуют 31 завершённый блок с тегами от `v0.1.0` до `v5.1.1`.
Оставшиеся пять на дату этого снимка относились к незавершённому деплою.
После мержа PR #35 история дополнена до 98 коммитов на SHA `f430b0f`.
Шесть коммитов завершённого OIDC/SDK-блока объединены выпуском `v5.1.2`.
Все 32 аннотированных тега опубликованы 2026-10-14; SHA первоначального
31 тега при публикации не изменены.
В тот же день по запросу владельца шаблонные аннотации заменены описаниями
изменений и инструкциями перехода из CHANGELOG. Объекты тегов обновлены;
коммиты выпусков, автор и дата создания каждого тега сохранены.
История Git не переписана.

Все изменения одного PR относятся к одному выпуску. Уровень определяется
наиболее значимым изменением приложения, тег указывает на SHA мержа.
Несколько PR могут составлять один явно связанный незавершённый блок,
как в случае production-деплоя выше.

В истории без PR последовательные PATCH-правки объединены до паузы
строго больше восьми часов между коммитами. Смена календарной даты
сама по себе блок не завершает. Явно связанные незавершённые изменения
объединены и через более длинную паузу; причины приведены в таблице блоков.
Времена параллельных веток не трактуются как последовательные паузы master.

Коммит `581fc59` с промежуточным удалением CLI не имеет собственного тега;
он входит в блок `2.1.0`. CLI восстановлен в `ff54fb8` с теми же параметрами
`--debug`, `-s`, `-d`, `-t`, `-r`; этот рефакторинг не требует MAJOR.

Таблицы описывают версии исходников, а не существовавшие в те даты релизы,
успешные проверки или production-деплои. Теги назначены ретроспективно.
Даты взяты из committer date последнего коммита блока; порядок обхода —
`git log --reverse --topo-order` на указанном SHA. Промежуточные снимки
могут быть неработоспособны и не получают отдельных тегов.
Группировка выпусков не означает squash или переписывание истории Git.

## Несовместимые переходы

MAJOR определяется совместимостью пользовательского запуска и конфигурации.
Изменение существующих параметров запуска или введение обязательного параметра
повышает MAJOR. Добавление необязательного параметра или новой возможности приложения
повышает MINOR. Несовместимые схемы конфигов также требуют MAJOR.

Переименование или перенос исполняемого файла, смена импортов, SDK
и зависимостей сами по себе не требуют MAJOR, если сохраняются параметры
и поведение запуска. Внутренние сигнатуры Runner/движка не являются
самостоятельным основанием для MAJOR приложения. Исправление синтаксиса
необязательного `--debug` в `e7bb0a1` отнесено к PATCH.
Корректировки резерва, порога и облачного источника по умолчанию в `821b9e2`
и `ad0394b` являются настройкой для устранения проблем, а не изменением
экономического контракта. Вторая входит в MAJOR-блок PR #3 только из-за
изменений обязательных аргументов CLI в том же PR.

Workflow, CI/CD, Make, тестовая, сборочная и деплойная обвязка без изменения
функциональности приложения получают PATCH, включая новые технические
Python-скрипты. PR #27 получает MINOR за поддержку строковых ID API
в ACCOUNT-конфиге, включая UUID, а не за добавление Sandbox E2E.

| Версия | Коммит завершения | Что изменить при переходе |
| --- | --- | --- |
| 1.0.0 | [0ba1fc9](https://github.com/gorpsys/autorepeater/commit/0ba1fc92fac89a667ab08f8c6434da93b6537327) | Передавать `src dst` позиционными аргументами CLI; запуска без них недостаточно. |
| 2.0.0 | [c721c67](https://github.com/gorpsys/autorepeater/commit/c721c67f1045b682252f72fe7b28a9d98e59ee2e) | Заменить позиционные аргументы на `-s <src> -d <dst>`. |
| 3.0.0 | [27d27dc](https://github.com/gorpsys/autorepeater/commit/27d27dc6b8a408b3f52a2ba98335352f1a02e142) | Явно задавать источник в CLI и облаке через `src`/`SRC_ACCOUNT`; пустое значение недопустимо. |
| 4.0.0 | [999f407](https://github.com/gorpsys/autorepeater/commit/999f407ce83ed9e4faa23d648867161537cf5c3a) | Задать `--algoritm ACCOUNT|INDEX` и `--src`; подготовить `PreparedStrategy` перед созданием Runner. |
| 5.0.0 | [e3987ca](https://github.com/gorpsys/autorepeater/commit/e3987cac247f4bc413ba9495bea5aa925bd69f7c) | Перенести `--reserve` и `--threshold` в конфиги; ACCOUNT выбирать по `name` с `source_account_id`; дополнить INDEX таблицей и COMPOSITE `component_drift_limit`; реализовать пяти-методный Strategy. При необходимости переопределить облачные COMPOSITE/BALANCED. |

## Блоки выпусков

От нового к старому. Тег ставится только на последний коммит блока.
Диапазон в таблице обозначает границы блока; точный состав, включая
веточные и merge-коммиты, перечислен в истории ниже.

| Версия / тег | Дата | Последний коммит | Уровень выпуска | Состав | Основание группировки |
| --- | --- | --- | --- | --- | --- |
| `v5.1.2` | 2026-10-13 | [f430b0f](https://github.com/gorpsys/autorepeater/commit/f430b0fa6921ec56b8181a5084c9ee0297c57a98) | PATCH | PR #29–#33 и #35 | Связанный блок OIDC-деплоя завершён успешным offline-деплоем из GitHub; SDK-резерв и сборочная обвязка не меняют приложение. |
| `v5.1.1` | 2026-10-08 | [1c665ce](https://github.com/gorpsys/autorepeater/commit/1c665ced41ba40d9ca40de3edcb9b061ef7bb401) | PATCH | [PR #28](https://github.com/gorpsys/autorepeater/pull/28) | Один законченный PR #28; уровень по наиболее значимому изменению. |
| `v5.1.0` | 2026-10-08 | [75944ad](https://github.com/gorpsys/autorepeater/commit/75944ad6361779f6b075a197e0876fa8fe3f33fe) | MINOR | [PR #27](https://github.com/gorpsys/autorepeater/pull/27) | Один законченный PR #27; уровень по наиболее значимому изменению. |
| `v5.0.1` | 2026-10-07 | [1adb172](https://github.com/gorpsys/autorepeater/commit/1adb17250a49c890fb65242879ae99b962a45210) | PATCH | [PR #26](https://github.com/gorpsys/autorepeater/pull/26) | Один законченный PR #26; уровень по наиболее значимому изменению. |
| `v5.0.0` | 2026-10-06 | [e3987ca](https://github.com/gorpsys/autorepeater/commit/e3987cac247f4bc413ba9495bea5aa925bd69f7c) | MAJOR | [PR #4](https://github.com/gorpsys/autorepeater/pull/4) | Один законченный PR #4; уровень по наиболее значимому изменению. |
| `v4.0.0` | 2026-09-30 | [999f407](https://github.com/gorpsys/autorepeater/commit/999f407ce83ed9e4faa23d648867161537cf5c3a) | MAJOR | [PR #3](https://github.com/gorpsys/autorepeater/pull/3) | Один законченный PR #3; уровень по наиболее значимому изменению. |
| `v3.0.0` | 2026-09-29 | [27d27dc](https://github.com/gorpsys/autorepeater/commit/27d27dc6b8a408b3f52a2ba98335352f1a02e142) | MAJOR | [PR #2](https://github.com/gorpsys/autorepeater/pull/2) | Один законченный PR #2; уровень по наиболее значимому изменению. |
| `v2.2.1` | 2026-09-29 | [15a5e1f](https://github.com/gorpsys/autorepeater/commit/15a5e1f7bf762217d977d8a146de09071c39d999) | PATCH | [PR #1](https://github.com/gorpsys/autorepeater/pull/1) | Один законченный PR #1; уровень по наиболее значимому изменению. |
| `v2.2.0` | 2026-09-29 | [1f60ffb](https://github.com/gorpsys/autorepeater/commit/1f60ffb217a54f729337e650af1e6cab83a5e4fd) | MINOR | 1 комм.: 1f60ffb | Облачный handler.handler с разовым run_sync. |
| `v2.1.2` | 2026-09-29 | [3513aa6](https://github.com/gorpsys/autorepeater/commit/3513aa6e78e1d93c7661961ec65ade05e9d45f04) | PATCH | 4 комм.: 19b4e57–3513aa6 | Инструкции, миграция SDK, индекс пакетов и разделение модулей; паузы до 8 часов. |
| `v2.1.1` | 2025-05-12 | [821b9e2](https://github.com/gorpsys/autorepeater/commit/821b9e2d5bdceccfe3bfae23747c59de65b39e57) | PATCH | 5 комм.: ca80501–821b9e2 | Decimal, исправления стиля, make format и корректировка констант; паузы до 8 часов. |
| `v2.1.0` | 2025-03-11 | [9875d9d](https://github.com/gorpsys/autorepeater/commit/9875d9d3b6b212d734daf41a4d275f6a5bd6e50e) | MINOR | 7 комм.: 581fc59–9875d9d | Выделение Runner/CLI и подготовка облачного запуска: после паузы 15 ч 19 мин завершены перенос пакета и исправление логирования. Промежуточное удаление CLI не получает отдельной версии. |
| `v2.0.6` | 2025-02-18 | [22fc9b6](https://github.com/gorpsys/autorepeater/commit/22fc9b69890bd135414f751832dea7bcd4e4771f) | PATCH | 7 комм.: 0f5efb0–22fc9b6 | Настройка Python и последовательные исправления Pylint workflow; паузы до 8 часов. |
| `v2.0.5` | 2025-01-17 | [5ed8056](https://github.com/gorpsys/autorepeater/commit/5ed80565dce3893ceb08ce9a6cdfee267109282e) | PATCH | 4 комм.: 270eceb–5ed8056 | Исправление тестов и линтеров; паузы до 8 часов. |
| `v2.0.4` | 2024-11-16 | [67309dc](https://github.com/gorpsys/autorepeater/commit/67309dcff91eae30d59b8bfc9be3d01c7212565a) | PATCH | 1 комм.: 67309dc | Отдельный этап расширения регрессионных тестов, не продолжение незавершённого workflow. |
| `v2.0.3` | 2024-11-14 | [6149163](https://github.com/gorpsys/autorepeater/commit/61491639580d69db1df9f41725cb18180c54e226) | PATCH | 1 комм.: 6149163 | Отдельный этап тестирования расчёта стоимости намерений. |
| `v2.0.2` | 2024-11-10 | [f7b6905](https://github.com/gorpsys/autorepeater/commit/f7b69057b888455ec6b1b7fd4c66c309cebea336) | PATCH | 2 комм.: 8877bef–f7b6905 | Дополнение тестов денежных помощников и триггеров; пауза до 8 часов. |
| `v2.0.1` | 2024-11-10 | [3628c20](https://github.com/gorpsys/autorepeater/commit/3628c20d7b0d2ada899d59f774623e7198bd9015) | PATCH | 11 комм.: 8a1b6f8–3628c20 | CLI/requirements и первая инфраструктура тестов: после паузы 24 ч 21 мин placeholder test_q заменён настоящими тестами, исправлены package layout и workflow. Единый незавершённый блок. |
| `v2.0.0` | 2024-11-08 | [c721c67](https://github.com/gorpsys/autorepeater/commit/c721c67f1045b682252f72fe7b28a9d98e59ee2e) | MAJOR | 1 комм.: c721c67 | Замена позиционных аргументов флагами CLI. |
| `v1.2.0` | 2024-11-07 | [f65f3a2](https://github.com/gorpsys/autorepeater/commit/f65f3a252a54646e38ce04052faab33d86d1b160) | MINOR | 1 комм.: f65f3a2 | Добавление общего порога синхронизации. |
| `v1.1.2` | 2024-11-07 | [972c089](https://github.com/gorpsys/autorepeater/commit/972c089747dafa84968ce263b258b1f29f75991e) | PATCH | 3 комм.: 997c06e–972c089 | Обработка исключений и интеграция параллельной документации; блок завершается merge, время боковой ветки не задаёт новую паузу. |
| `v1.1.1` | 2024-11-06 | [39912b1](https://github.com/gorpsys/autorepeater/commit/39912b168dc6402bcad4403d2e8b903abd2a7250) | PATCH | 1 комм.: 39912b1 | Выделение помощников и настройка debug. |
| `v1.1.0` | 2024-11-05 | [a11beec](https://github.com/gorpsys/autorepeater/commit/a11beecda1704ebe82561ba3e70d8b728d596e2e) | MINOR | 1 комм.: a11beec | Добавление debug-режима. |
| `v1.0.2` | 2024-11-04 | [e3a56dd](https://github.com/gorpsys/autorepeater/commit/e3a56dd33c725de401fc980644ea9a92c5119621) | PATCH | 2 комм.: 41d60a3–e3a56dd | Вывод портфелей перед проверкой аргументов и документация; пауза до 8 часов. |
| `v1.0.1` | 2024-11-02 | [86fc130](https://github.com/gorpsys/autorepeater/commit/86fc130a52929d36cbc55a52456a441362375a71) | PATCH | 3 комм.: bf7150b–86fc130 | Уточнение документации и исправление триггера; паузы до 8 часов. |
| `v1.0.0` | 2024-11-02 | [0ba1fc9](https://github.com/gorpsys/autorepeater/commit/0ba1fc92fac89a667ab08f8c6434da93b6537327) | MAJOR | 1 комм.: 0ba1fc9 | Обязательные позиционные параметры CLI. |
| `v0.4.0` | 2024-11-02 | [7ab688a](https://github.com/gorpsys/autorepeater/commit/7ab688abf2be6dc131afac6970f55ec10ef500d2) | MINOR | 1 комм.: 7ab688a | Денежный резерв назначения. |
| `v0.3.1` | 2024-11-02 | [485e18d](https://github.com/gorpsys/autorepeater/commit/485e18d8457315df99b8fb9871806032f033d8f0) | PATCH | 1 комм.: 485e18d | Документация принципа повторения счёта. |
| `v0.3.0` | 2024-11-02 | [79f92fe](https://github.com/gorpsys/autorepeater/commit/79f92fe6e0a9ddcbf7da34f6118b75c96e3b13ac) | MINOR | 1 комм.: 79f92fe | Отправка торговых заявок. |
| `v0.2.1` | 2024-10-31 | [09bf6ae](https://github.com/gorpsys/autorepeater/commit/09bf6aec2a8635f13f941770effccf1a53e941a5) | PATCH | 1 комм.: 09bf6ae | Исправление разницы позиций и триггеров. |
| `v0.2.0` | 2024-10-29 | [9b12708](https://github.com/gorpsys/autorepeater/commit/9b12708083f342f2fde14c007e1af0cbcfed3e86) | MINOR | 1 комм.: 9b12708 | Чтение портфелей и подписка на события. |
| `v0.1.0` | 2024-10-24 | [fb50345](https://github.com/gorpsys/autorepeater/commit/fb503454b4a8d8d940e9ccd8946397510b7defee) | Начало | 1 комм.: fb50345 | Начало репозитория. |

## История коммитов

От нового к старому. Каждый из 98 коммитов перечислен ровно один раз.
Уровень в этой таблице относится к самому изменению; итоговый уровень
выпуска указан в таблице блоков. Промежуточные коммиты входят в версию
своего блока, но не имеют собственного тега. Сообщения описаны по
изменениям файлов, а не только по исходным заголовкам коммитов.

| Версия / тег | Дата | Коммит | Уровень изменения | Изменения |
| --- | --- | --- | --- | --- |
| `v5.1.2` | 2026-10-13 | [f430b0f](https://github.com/gorpsys/autorepeater/commit/f430b0fa6921ec56b8181a5084c9ee0297c57a98) | PATCH | Влит PR #35: резерв SDK 1.51.0, runtime/file locks, verified setup, vendored/offline архивы, package smoke и ручной резервный деплой. Торговый код не менялся; завершение блока подтверждено offline-деплоем. |
| В составе 5.1.2, без тега | 2026-10-10 | [ba36687](https://github.com/gorpsys/autorepeater/commit/ba3668702bf2d5f46f6445826c5398615a5e05f1) | PATCH | IAM передаётся yc через YC_IAM_TOKEN; уточнены диагностика и проверка runtime SA (PR #33). |
| В составе 5.1.2, без тега | 2026-10-09 | [e1351a8](https://github.com/gorpsys/autorepeater/commit/e1351a8d91b5840f1ff7089a4d4852ae3c794f03) | PATCH | Для yc создаётся изолированный временный профиль с endpoint без хранения credentials (PR #32). |
| В составе 5.1.2, без тега | 2026-10-09 | [07c3ec4](https://github.com/gorpsys/autorepeater/commit/07c3ec48de820e39bca0003569ce411008826a06) | PATCH | Разделены audience GitHub OIDC и Yandex IAM exchange (PR #31). |
| В составе 5.1.2, без тега | 2026-10-09 | [9b3a1f1](https://github.com/gorpsys/autorepeater/commit/9b3a1f144f52887b708b4f75aca511afc65d9343) | PATCH | Исправлена идентификация запуска deploy-gate; добавлен безопасный прогресс live E2E (PR #30). |
| В составе 5.1.2, без тега | 2026-10-09 | [0d80656](https://github.com/gorpsys/autorepeater/commit/0d80656c6b1a72b186345beea70276a697200f2c) | PATCH | Добавлен production-деплой проверенного master через OIDC, Object Storage и проверку настроек версии функции (PR #29). |
| `v5.1.1` | 2026-10-08 | [1c665ce](https://github.com/gorpsys/autorepeater/commit/1c665ced41ba40d9ca40de3edcb9b061ef7bb401) | PATCH | Усилены E2E-гейты и очистка; добавлены до трёх дополнительных транспортных повторов только упавших сценариев. |
| `v5.1.0` | 2026-10-08 | [75944ad](https://github.com/gorpsys/autorepeater/commit/75944ad6361779f6b075a197e0876fa8fe3f33fe) | PATCH | Влит PR #27 с локальными проверками и инфраструктурой Sandbox E2E. |
| В составе 5.1.0, без тега | 2026-10-08 | [2dc5f65](https://github.com/gorpsys/autorepeater/commit/2dc5f658685b6bf0efa28778ef3cdb3f287aee74) | PATCH | Исправлены проверки отмены в контекстах GitHub workflow. |
| В составе 5.1.0, без тега | 2026-10-08 | [82363f3](https://github.com/gorpsys/autorepeater/commit/82363f3fdb4235d56f72e059b5e3752e9a9e58ee) | PATCH | Добавлены очередь Sandbox E2E, ручной запуск для PR и проверенный статус точного SHA. |
| В составе 5.1.0, без тега | 2026-10-08 | [e250760](https://github.com/gorpsys/autorepeater/commit/e250760aced43e247f696aaac9d84702c36d8c25) | PATCH | Добавлены общие цели Make setup/lint/test/coverage/typecheck/e2e и проверка Mypy; CI использует Make. |
| В составе 5.1.0, без тега | 2026-10-08 | [1caaa28](https://github.com/gorpsys/autorepeater/commit/1caaa28ea37aa5dcbe131e796ff00b195812cd71) | MINOR | Добавлены live Sandbox E2E и безопасная диагностика; ACCOUNT допускает строковый ID API, включая UUID. |
| В составе 5.1.0, без тега | 2026-10-08 | [8b9dbe0](https://github.com/gorpsys/autorepeater/commit/8b9dbe06f3c2eb7939f2e1266e8217d4f7b925ea) | PATCH | Устранены замечания строгого линтера без изменения торговых расчётов. |
| `v5.0.1` | 2026-10-07 | [1adb172](https://github.com/gorpsys/autorepeater/commit/1adb17250a49c890fb65242879ae99b962a45210) | PATCH | Влит PR #26 с проверками брокерских ответов. |
| В составе 5.0.1, без тега | 2026-10-07 | [403044a](https://github.com/gorpsys/autorepeater/commit/403044a9ca5638ee3311ecc2cc6047ca65ab6102) | PATCH | Усилены проверки фактов брокерских квитанций и сохранение request ID/транспортного контекста. |
| `v5.0.0` | 2026-10-06 | [e3987ca](https://github.com/gorpsys/autorepeater/commit/e3987cac247f4bc413ba9495bea5aa925bd69f7c) | PATCH | Влит PR #4 с COMPOSITE, новой политикой ребалансировки и исполнением. |
| В составе 5.0.0, без тега | 2026-10-06 | [cc838e8](https://github.com/gorpsys/autorepeater/commit/cc838e876da0c7765b2a8beef4b0b58758720180) | PATCH | Добавлены аннотации типов, внедряемые часы и фабрика request ID. |
| В составе 5.0.0, без тега | 2026-10-06 | [f02aefa](https://github.com/gorpsys/autorepeater/commit/f02aefabdd75441b8d333f68ef38ddb919fa8776) | PATCH | Добавлены повторы при квоте API, ожидание SELL FILL и обновления доступности перед BUY. |
| В составе 5.0.0, без тега | 2026-10-06 | [296452e](https://github.com/gorpsys/autorepeater/commit/296452eb3174a7166e23c4330f0bfde690635a12) | MAJOR | Введены StrategyPlan, денежные области и индивидуальные критерии ребалансировки; удалён --threshold, изменены схемы ACCOUNT/INDEX/COMPOSITE. |
| В составе 5.0.0, без тега | 2026-10-06 | [5528821](https://github.com/gorpsys/autorepeater/commit/5528821af680ef94349a2f4ec5b880c94b91b203) | PATCH | Восстановлена торговля фондами; дополнена диагностика торговых решений. |
| В составе 5.0.0, без тега | 2026-10-06 | [69036ef](https://github.com/gorpsys/autorepeater/commit/69036efa9f47f481cfb3afae0cddac2e060c0063) | MAJOR | Добавлен COMPOSITE и конфигурируемые резервы листьев; удалён --reserve, облачный default изменён на COMPOSITE/BALANCED. |
| `v4.0.0` | 2026-09-30 | [999f407](https://github.com/gorpsys/autorepeater/commit/999f407ce83ed9e4faa23d648867161537cf5c3a) | PATCH | Влит PR #3 с INDEX и подготовкой стратегий. |
| В составе 4.0.0, без тега | 2026-09-30 | [3dd7de5](https://github.com/gorpsys/autorepeater/commit/3dd7de56b48595e5be7a545d1ac13f97d1c9e76f) | MAJOR | CLI требует --algoritm и --src; введены prepare_strategy, PreparedStrategy и read-only порт StrategyData. |
| В составе 4.0.0, без тега | 2026-09-30 | [ad0394b](https://github.com/gorpsys/autorepeater/commit/ad0394bef25da3bc26c48a334f3832d9f537f5b5) | MINOR | Добавлен INDEX, составы IMOEX/TMON и API-калибровка; облачный источник по умолчанию изменён на TMON. |
| `v3.0.0` | 2026-09-29 | [27d27dc](https://github.com/gorpsys/autorepeater/commit/27d27dc6b8a408b3f52a2ba98335352f1a02e142) | PATCH | Влит PR #2 с контрактом цели и стратегиями источника. |
| В составе 3.0.0, без тега | 2026-09-29 | [eb3a7cb](https://github.com/gorpsys/autorepeater/commit/eb3a7cbdbd1a8c2a5556f0c497e90b672246a801) | PATCH | Зафиксированы результаты проверки рефакторинга. |
| В составе 3.0.0, без тега | 2026-09-29 | [321f3cd](https://github.com/gorpsys/autorepeater/commit/321f3cde0d259f72cd1620148e8abb5675ee4c50) | PATCH | Описаны границы стратегий; добавлена проверка содержимого и запуска облачного архива. |
| В составе 3.0.0, без тега | 2026-09-29 | [e509d71](https://github.com/gorpsys/autorepeater/commit/e509d715b739ba8f90f66dfa41f1d1ef8bd64521) | MAJOR | Источник проверяется до Client; удалён неявный источник serverless, отсутствующий/неверный src фатален. |
| В составе 3.0.0, без тега | 2026-09-29 | [2f9a9c1](https://github.com/gorpsys/autorepeater/commit/2f9a9c1e90938b441c0feec091a680896ddccde9) | PATCH | Движок принимает стратегию источника; mainflow/sync_accounts получают только счёт назначения. |
| В составе 3.0.0, без тега | 2026-09-29 | [de93dee](https://github.com/gorpsys/autorepeater/commit/de93deef9d80968ed1558dafa5e597541eebf741) | MINOR | Добавлены TargetPortfolio, ACCOUNT и регистрация стратегий источника. |
| В составе 3.0.0, без тега | 2026-09-29 | [3718ada](https://github.com/gorpsys/autorepeater/commit/3718ada54b2a3aee96fb8bbce485e40c89c039c1) | PATCH | Вывод портфелей выделен из синхронизации. |
| В составе 3.0.0, без тега | 2026-09-29 | [ee5c2a5](https://github.com/gorpsys/autorepeater/commit/ee5c2a53d038363ae87828acdef85d62a9cb747f) | PATCH | Добавлены регрессионные тесты синхронизации счетов. |
| `v2.2.1` | 2026-09-29 | [15a5e1f](https://github.com/gorpsys/autorepeater/commit/15a5e1f7bf762217d977d8a146de09071c39d999) | PATCH | Влит PR #1 с миграцией SDK, разделением модулей и проверками. |
| В составе 2.2.1, без тега | 2026-09-29 | [2c4721e](https://github.com/gorpsys/autorepeater/commit/2c4721eb7bd1aa498ccf4a8c11b9da380a67bfe5) | PATCH | Настроены Python 3.12 и пороги общего/diff-покрытия в CI. |
| В составе 2.2.1, без тега | 2026-09-29 | [d930ef7](https://github.com/gorpsys/autorepeater/commit/d930ef7c5092adf09de6890b1b818a5338ac33fe) | PATCH | Восстановлен запуск тестов на pull_request. |
| В составе 2.2.1, без тега | 2026-09-29 | [2c7f691](https://github.com/gorpsys/autorepeater/commit/2c7f69182d27cbe0fef98ed5edce9b2306d4c7f7) | PATCH | SDK-моки строятся по спецификациям сервисов. |
| `v2.2.0` | 2026-09-29 | [1f60ffb](https://github.com/gorpsys/autorepeater/commit/1f60ffb217a54f729337e650af1e6cab83a5e4fd) | MINOR | Добавлены handler.handler и разовый serverless run_sync; удалён модуль совместимых экспортов autorepeater.autorepeater. |
| `v2.1.2` | 2026-09-29 | [3513aa6](https://github.com/gorpsys/autorepeater/commit/3513aa6e78e1d93c7661961ec65ade05e9d45f04) | PATCH | Монолит разделён на расчёты, оркестрацию, вывод и Runner; сохранены совместимые экспорты. |
| В составе 2.1.2, без тега | 2026-09-29 | [2235bd2](https://github.com/gorpsys/autorepeater/commit/2235bd229fb17815457b003c9f1874fe41589e3d) | PATCH | Индекс пакетов Т-Банка добавлен в trusted hosts. |
| В составе 2.1.2, без тега | 2026-09-29 | [38fa592](https://github.com/gorpsys/autorepeater/commit/38fa5920bf4f64d59e3e954b41d01949b3058359) | PATCH | SDK tinkoff-investments/tinkoff.invest заменён t-tech-investments/t_tech.invest. |
| В составе 2.1.2, без тега | 2026-09-29 | [19b4e57](https://github.com/gorpsys/autorepeater/commit/19b4e5753c3d2fe6eece1d7f6734c401049f0cbc) | PATCH | Добавлены инструкции для работы с репозиторием. |
| `v2.1.1` | 2025-05-12 | [821b9e2](https://github.com/gorpsys/autorepeater/commit/821b9e2d5bdceccfe3bfae23747c59de65b39e57) | PATCH | Значения по умолчанию изменены: reserve с 0.005 на 0.01, общий threshold с 0.001 на 0.004. |
| В составе 2.1.1, без тега | 2025-05-12 | [fbff67a](https://github.com/gorpsys/autorepeater/commit/fbff67a175aa5b9772e18fb508caa2957a53134f) | PATCH | Исправлены замечания линтера. |
| В составе 2.1.1, без тега | 2025-05-12 | [74507d7](https://github.com/gorpsys/autorepeater/commit/74507d7a3697469f7e1de732350c4faf0b70f001) | PATCH | Добавлена цель make format; упорядочено форматирование. |
| В составе 2.1.1, без тега | 2025-05-12 | [6ce421c](https://github.com/gorpsys/autorepeater/commit/6ce421c20234aff973e093444855c6d66b7477c5) | PATCH | Исправлены замечания линтеров для Decimal-расчётов. |
| В составе 2.1.1, без тега | 2025-05-12 | [ca80501](https://github.com/gorpsys/autorepeater/commit/ca8050184facff4a2f2915c07a4504a7ede773aa) | PATCH | Денежные расчёты переведены с float на Decimal; currency_to_float* заменены currency_to_decimal*, расширены тесты. |
| `v2.1.0` | 2025-03-11 | [9875d9d](https://github.com/gorpsys/autorepeater/commit/9875d9d3b6b212d734daf41a4d275f6a5bd6e50e) | PATCH | Исправлены настройки логирования для Yandex Cloud. |
| В составе 2.1.0, без тега | 2025-03-11 | [51e122f](https://github.com/gorpsys/autorepeater/commit/51e122ff5befb60c7b6aeb29fd943efde19c5160) | PATCH | Пакет app перенесён в autorepeater; CLI app/app.py перенесён в main.py. |
| В составе 2.1.0, без тега | 2025-03-10 | [9c84aaf](https://github.com/gorpsys/autorepeater/commit/9c84aaf966dd00debf0d51563b7ac90582ea94ee) | PATCH | Исправлен стиль отдельной точки входа. |
| В составе 2.1.0, без тега | 2025-03-10 | [ff54fb8](https://github.com/gorpsys/autorepeater/commit/ff54fb82990eada748c4559df1d265373348d7ef) | MINOR | Добавлена отдельная CLI-точка app/app.py. |
| В составе 2.1.0, без тега | 2025-03-10 | [5069a0b](https://github.com/gorpsys/autorepeater/commit/5069a0b2f107082914a9328eac68c62173733ec8) | PATCH | Дополнены исправления стиля Runner. |
| В составе 2.1.0, без тега | 2025-03-10 | [cec8d0e](https://github.com/gorpsys/autorepeater/commit/cec8d0e7be8f8ba53113021a7861ce4ce31b5d95) | PATCH | Исправлены замечания линтера после введения Runner. |
| В составе 2.1.0, без тега | 2025-03-10 | [581fc59](https://github.com/gorpsys/autorepeater/commit/581fc59a15845bacc214c1bd816ac382d7b95584) | Промежуточный | Добавлен Runner и разовая синхронизация для serverless; удалён CLI из app/autorepeater.py, вывод переведён на logging. |
| `v2.0.6` | 2025-02-18 | [22fc9b6](https://github.com/gorpsys/autorepeater/commit/22fc9b69890bd135414f751832dea7bcd4e4771f) | PATCH | Завершены исправления workflow Pylint. |
| В составе 2.0.6, без тега | 2025-02-18 | [00ae7c8](https://github.com/gorpsys/autorepeater/commit/00ae7c80ac0cac5023c211cc8e23271c6e072f23) | PATCH | Исправлена конфигурация workflow Pylint. |
| В составе 2.0.6, без тега | 2025-02-18 | [8d20e4e](https://github.com/gorpsys/autorepeater/commit/8d20e4eb59839ed87822d1f218a6fe8e5e0a91f7) | PATCH | Исправлен запуск Pylint в CI. |
| В составе 2.0.6, без тега | 2025-02-18 | [95cc3a2](https://github.com/gorpsys/autorepeater/commit/95cc3a2d11b1145dfde0fb28eb852274c9dd4e9c) | PATCH | Уточнены команды workflow Pylint. |
| В составе 2.0.6, без тега | 2025-02-18 | [0e31e13](https://github.com/gorpsys/autorepeater/commit/0e31e13efa3bb73445bb163bdcde26e205b67354) | PATCH | Исправлен workflow Pylint. |
| В составе 2.0.6, без тега | 2025-02-18 | [54c1eac](https://github.com/gorpsys/autorepeater/commit/54c1eac2ad0caad51c2bc1c4cf9bf577c350f6e2) | PATCH | Согласованы настройки линтеров. |
| В составе 2.0.6, без тега | 2025-02-18 | [0f5efb0](https://github.com/gorpsys/autorepeater/commit/0f5efb026e8999d0dafb869d7cf4ebbdf197ec24) | PATCH | Уточнена версия Python в workflow Pylint. |
| `v2.0.5` | 2025-01-17 | [5ed8056](https://github.com/gorpsys/autorepeater/commit/5ed80565dce3893ceb08ce9a6cdfee267109282e) | PATCH | Исправлена конфигурация линтера. |
| В составе 2.0.5, без тега | 2025-01-17 | [95879f2](https://github.com/gorpsys/autorepeater/commit/95879f2ed8bad73ddc752a0e51c270a3f1862a31) | PATCH | Уточнены настройки проверок стиля. |
| В составе 2.0.5, без тега | 2025-01-17 | [8fe2f8f](https://github.com/gorpsys/autorepeater/commit/8fe2f8ff9ca8886d1672a45929b553a456679e21) | PATCH | Исправлены замечания линтеров. |
| В составе 2.0.5, без тега | 2025-01-17 | [270eceb](https://github.com/gorpsys/autorepeater/commit/270ecebb4cb99f595bf4deef7bd3df4e84bee184) | PATCH | Исправлены тесты. |
| `v2.0.4` | 2024-11-16 | [67309dc](https://github.com/gorpsys/autorepeater/commit/67309dcff91eae30d59b8bfc9be3d01c7212565a) | PATCH | Добавлены тесты вспомогательных функций и поведения приложения. |
| `v2.0.3` | 2024-11-14 | [6149163](https://github.com/gorpsys/autorepeater/commit/61491639580d69db1df9f41725cb18180c54e226) | PATCH | Расширена регрессионная свита. |
| `v2.0.2` | 2024-11-10 | [f7b6905](https://github.com/gorpsys/autorepeater/commit/f7b69057b888455ec6b1b7fd4c66c309cebea336) | PATCH | Дополнены тестовые сценарии. |
| В составе 2.0.2, без тега | 2024-11-10 | [8877bef](https://github.com/gorpsys/autorepeater/commit/8877bef79d0d50d582fb602f4993ba55cd6fd6c8) | PATCH | Расширены тесты расчётов и синхронизации. |
| `v2.0.1` | 2024-11-10 | [3628c20](https://github.com/gorpsys/autorepeater/commit/3628c20d7b0d2ada899d59f774623e7198bd9015) | PATCH | Исправлены команды тестового workflow. |
| В составе 2.0.1, без тега | 2024-11-10 | [60ae8e1](https://github.com/gorpsys/autorepeater/commit/60ae8e1f857fb05880ab5b4d7e174c9057a2e70b) | PATCH | CLI autorepeater.py перенесён в app/autorepeater.py; добавлены тесты приложения. |
| В составе 2.0.1, без тега | 2024-11-08 | [2ae66b7](https://github.com/gorpsys/autorepeater/commit/2ae66b7c6b300620e48bf73998f0f406310ce204) | PATCH | Исправлено форматирование кода. |
| В составе 2.0.1, без тега | 2024-11-08 | [1af6bef](https://github.com/gorpsys/autorepeater/commit/1af6beff28175a48a17ad717dff7c50db5d0cff1) | PATCH | Добавлен начальный тест для настройки тестового workflow. |
| В составе 2.0.1, без тега | 2024-11-08 | [3d465da](https://github.com/gorpsys/autorepeater/commit/3d465da83cf74da66fb408e72db332e19e688ccd) | PATCH | Добавлен workflow Python-тестов. |
| В составе 2.0.1, без тега | 2024-11-08 | [6fdec2f](https://github.com/gorpsys/autorepeater/commit/6fdec2fac28671b062a382ffe07882e17d53b9e7) | PATCH | Исправлены замечания по стилю кода. |
| В составе 2.0.1, без тега | 2024-11-08 | [6f3f9b1](https://github.com/gorpsys/autorepeater/commit/6f3f9b1c99985eb4a9f451919fb7ae4fa8481f11) | PATCH | Исправлено имя файла зависимостей в workflow Pylint. |
| В составе 2.0.1, без тега | 2024-11-08 | [4723830](https://github.com/gorpsys/autorepeater/commit/47238301afd27a5b2b4881b3363a02e36e9352c1) | PATCH | Добавлена установка зависимостей для Pylint. |
| В составе 2.0.1, без тега | 2024-11-08 | [4488812](https://github.com/gorpsys/autorepeater/commit/4488812e5eb73929ce1ec00ccd6aaefd50281b17) | PATCH | Добавлен workflow Pylint. |
| В составе 2.0.1, без тега | 2024-11-08 | [e7bb0a1](https://github.com/gorpsys/autorepeater/commit/e7bb0a1a285e97a47177c4d6cbcdbdc6daed7ee5) | PATCH | --debug принимает флаг без значения вместо bool-аргумента. |
| В составе 2.0.1, без тега | 2024-11-08 | [8a1b6f8](https://github.com/gorpsys/autorepeater/commit/8a1b6f81a327d664010005eab6333dccdeaa58d6) | PATCH | Точка запуска client.py переименована в autorepeater.py; добавлен requirements.txt. |
| `v2.0.0` | 2024-11-08 | [c721c67](https://github.com/gorpsys/autorepeater/commit/c721c67f1045b682252f72fe7b28a9d98e59ee2e) | MAJOR | Позиционные src/dst заменены флагами -s/-d; добавлены -t/--threshold и -r/--reserve. |
| `v1.2.0` | 2024-11-07 | [f65f3a2](https://github.com/gorpsys/autorepeater/commit/f65f3a252a54646e38ce04052faab33d86d1b160) | MINOR | Добавлен общий порог стоимости сделок для запуска синхронизации. |
| `v1.1.2` | 2024-11-07 | [972c089](https://github.com/gorpsys/autorepeater/commit/972c089747dafa84968ce263b258b1f29f75991e) | PATCH | Объединены изменения master и локальной ветки; отдельная возможность не добавлена. |
| В составе 1.1.2, без тега | 2024-11-06 | [fbb0f8d](https://github.com/gorpsys/autorepeater/commit/fbb0f8db8793fe26316a5fecf966d21515692c57) | PATCH | Обновлена документация запуска. |
| В составе 1.1.2, без тега | 2024-11-07 | [997c06e](https://github.com/gorpsys/autorepeater/commit/997c06e199a296de24a7e54fc5adc7e8ced11a9d) | PATCH | Добавлена обработка исключений в событийном цикле. |
| `v1.1.1` | 2024-11-06 | [39912b1](https://github.com/gorpsys/autorepeater/commit/39912b168dc6402bcad4403d2e8b903abd2a7250) | PATCH | Упорядочены методы синхронизации и настройка debug. |
| `v1.1.0` | 2024-11-05 | [a11beec](https://github.com/gorpsys/autorepeater/commit/a11beecda1704ebe82561ba3e70d8b728d596e2e) | MINOR | Добавлен debug-режим без отправки заявок; выделены вспомогательные функции. |
| `v1.0.2` | 2024-11-04 | [e3a56dd](https://github.com/gorpsys/autorepeater/commit/e3a56dd33c725de401fc980644ea9a92c5119621) | PATCH | Уточнены инструкции README. |
| В составе 1.0.2, без тега | 2024-11-04 | [41d60a3](https://github.com/gorpsys/autorepeater/commit/41d60a362b7e570b5146dcc696f501686bdf918b) | PATCH | Вывод портфелей перенесён перед проверкой аргументов запуска. |
| `v1.0.1` | 2024-11-02 | [86fc130](https://github.com/gorpsys/autorepeater/commit/86fc130a52929d36cbc55a52456a441362375a71) | PATCH | Исправлен триггер счёта назначения; упорядочен стиль кода. |
| В составе 1.0.1, без тега | 2024-11-02 | [c353301](https://github.com/gorpsys/autorepeater/commit/c35330152445eafd76784064c487ec8c9d2b6ecb) | PATCH | Дополнена документация приложения. |
| В составе 1.0.1, без тега | 2024-11-02 | [bf7150b](https://github.com/gorpsys/autorepeater/commit/bf7150b51cabbd6936f442eb03f3aafa3db8e060) | PATCH | Уточнено описание запуска в README. |
| `v1.0.0` | 2024-11-02 | [0ba1fc9](https://github.com/gorpsys/autorepeater/commit/0ba1fc92fac89a667ab08f8c6434da93b6537327) | MAJOR | Источник и назначение задаются обязательными позиционными аргументами CLI вместо констант. |
| `v0.4.0` | 2024-11-02 | [7ab688a](https://github.com/gorpsys/autorepeater/commit/7ab688abf2be6dc131afac6970f55ec10ef500d2) | MINOR | Добавлен денежный резерв счёта назначения. |
| `v0.3.1` | 2024-11-02 | [485e18d](https://github.com/gorpsys/autorepeater/commit/485e18d8457315df99b8fb9871806032f033d8f0) | PATCH | Описаны повторение счёта, ограничения валют и торговых инструментов. |
| `v0.3.0` | 2024-11-02 | [79f92fe](https://github.com/gorpsys/autorepeater/commit/79f92fe6e0a9ddcbf7da34f6118b75c96e3b13ac) | MINOR | Добавлена отправка заявок PostOrder при синхронизации. |
| `v0.2.1` | 2024-10-31 | [09bf6ae](https://github.com/gorpsys/autorepeater/commit/09bf6aec2a8635f13f941770effccf1a53e941a5) | PATCH | Исправлены расчёт разницы позиций и обработка триггеров. |
| `v0.2.0` | 2024-10-29 | [9b12708](https://github.com/gorpsys/autorepeater/commit/9b12708083f342f2fde14c007e1af0cbcfed3e86) | MINOR | Добавлены чтение и вывод портфелей, расчёт разницы позиций и подписка на события. |
| `v0.1.0` | 2024-10-24 | [fb50345](https://github.com/gorpsys/autorepeater/commit/fb503454b4a8d8d940e9ccd8946397510b7defee) | Начало | Создан репозиторий и первоначальный README. |

## Bootstrap Sandbox E2E, 2026-10-08

При подготовке [PR #28](https://github.com/gorpsys/autorepeater/pull/28)
использовался временный запуск через событие PR с ручным одобрением
окружения `sandbox-e2e-bootstrap`. Его назначение — установить workflow,
который затем выполняет ручные PR-прогоны и автоматические master-прогоны.
Bootstrap-процедура не является текущей инструкцией запуска E2E.
