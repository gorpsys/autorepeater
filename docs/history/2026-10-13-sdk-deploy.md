# Проверка SDK и облачного деплоя, 2026-10-13

Исторические свидетельства по [PR #35](https://github.com/gorpsys/autorepeater/pull/35).
Действующие команды — в [техническом README](../../scripts/README.md),
состав выпуска — в [истории версий](versioning.md#production-деплой-через-oidc).
Время ниже указано в UTC.

## Проверенные исходники и проверки

PR #35 влит в `master` на SHA `f430b0fa6921ec56b8181a5084c9ee0297c57a98`.
Изменения относятся к установке зависимостей, CI, сборке и диагностике пакетов;
код приложения `autorepeater/`, `main.py` и `handler.py` не менялся.

| Проверка | Результат | Запуск |
| --- | --- | --- |
| Lint master | Успех | [38378872042](https://github.com/gorpsys/autorepeater/actions/runs/38378872042) |
| Offline checks master, включая package smoke | Успех | [38378871961](https://github.com/gorpsys/autorepeater/actions/runs/38378871961) |
| Sandbox E2E master, согласованный полный повтор | Все 17 сценариев прошли, очистка подтверждена | [38378871897, attempt 2](https://github.com/gorpsys/autorepeater/actions/runs/38378871897/attempts/2) |

Первый master E2E получил транспортный DEADLINE_EXCEEDED и ошибку no-op
проверки после пополнения при меняющихся котировках. Автоматический retry не
скрыл нетранспортную ошибку. Один полный повтор отдельно согласован;
торговый код и тесты перед ним не менялись. Счета всех сценариев очищены.

## Обычный деплой

[Запуск 38384713014](https://github.com/gorpsys/autorepeater/actions/runs/38384713014)
прошёл OIDC/IAM и загрузку ZIP. Создание версии 10:38–10:40 вернуло
InvalidArgument. Объект:
`autorepeater-deploy/functions/f430b0fa6921ec56b8181a5084c9ee0297c57a98/38384713014-1.zip`.
SHA-256: `720f8a07be71b6fa020f772cfa3d4d69a2a3da5c09af820c0fb1d0b804844dec`.
Request ID: `aa82d52a-9b2d-4562-871d-c2c8e4823dce`;
client trace ID: `8f947e3f-4378-4968-8d0f-be1dc88332a6`.

CLI-обвязка сохранила только категорию и корреляционные ID, не сырой ответ.
При ручном создании версии из загруженного артефакта build log показал:

```text
will start 'pip3.12 install' in /function/code
Connection to opensource.tbank.ru timed out. (connect timeout=15)
ERROR: Could not find a version that satisfies the requirement t-tech-investments==1.51.0 (from versions: none)
ERROR: No matching distribution found for t-tech-investments==1.51.0
execution failed: exit status 1
```

Повтор подключения не восстановил доступ. `from versions: none` здесь является
следствием недоступного индекса, а не доказательством отсутствия SDK.
Сбой установки в облаке подтверждён ручным build log; внутренний лог первого
запроса по request/trace ID недоступен. Default-архив не использует резерв
автоматически, поэтому само наличие wheel в Git эту сборку не исправляет.

## Автономный деплой

После отдельного согласования выполнен
[deploy-prod-reserve, запуск 38393064309](https://github.com/gorpsys/autorepeater/actions/runs/38393064309)
на том же SHA `master`, `mode=offline`. Все обязательные gates прошли;
сборка runtime, OIDC/IAM, Object Storage и создание версии успешны.

- 11:51:59: публикация пакета
  `functions/f430b0fa6921ec56b8181a5084c9ee0297c57a98/38393064309-1.zip`.
- SHA-256: `5e80346a05edc0b3f9c6672d1cfe42e89b7ac7059fde7c322d9f86cf6bb89ab4`.
- 11:52:24: версия `d4ebln458hf8gvsr8ln7` проверена как ACTIVE.
- Подтверждены `python312`, `handler.handler`, 256 МБ, 60 секунд,
  runtime SA, окружение и ссылки Lockbox.

Production handler не вызывался. Успех подтверждает публикацию и сохранение
конфигурации, но не облачный Python cold start или совершение сделок.
При сборке требовался PyPI; готовый offline ZIP не требовал установки
зависимостей в облаке. Обычный индексный workflow не менялся.
Условие GitHub/OIDC-проверки связанного PATCH-блока выполнено.
Тег `v5.1.2` опубликован 2026-10-14 на SHA мержа PR #35.

## Официальный wheel на GitHub

Сообщённый официальный источник:
[t_tech_investments-1.51.0-py3-none-any.whl](https://github.com/Invest-SDK/invest-python/raw/refs/heads/master/t_tech_investments-1.51.0-py3-none-any.whl).
Это URL wheel, не PyPI Simple Index; передавать его как `--index-url` неверно.
Успешное скачивание с GitHub не доказывает восстановление основного T-Bank индекса.

| Артефакт | Размер, байт | SHA-256 |
| --- | --- | --- |
| Резерв в Git | 316190 | `f0fe22cbed727cecd9971200032d893c19702838b971c08abc2831fe25fa2856` |
| Официальный GitHub wheel на момент сверки | 316897 | `74b7a95474a238a982863740a2299145fd0a2d5d1f6e9cb09b717d6f87dd1d4b` |

Имена файлов, Python-код, Name/Version и Requires-Dist совпали. Содержимое
отличается в `t_tech/invest/certs/RussianTrustedRootCA.pem` и связанном RECORD:
в резерве один сертификат, в официальном wheel два. Общий сертификат имеет
DER SHA-256 `d26d2d0231b7c39f92cc738512ba54103519e4405d68b5bd703e9788ca8ecf31`;
добавленный — `e46888b8e0f8fd3e6fc4428fc6d8dcebfb99b76d41c88c978373e6932df9a5e4`.
Это отличие доверенных сертификатов, не только ZIP-упаковки.

Новая пара hashes не одобрена. GitHub URL не добавлен в installer как источник,
manifest и lock не менялись; `approvals=[]`, `official_hash_verified=false`
сохраняют статус сверки с основным индексом. Замена SDK требует отдельного
одобрения пары и проверки lock/состава пакета.
