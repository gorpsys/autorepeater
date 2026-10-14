# Свой конфиг в песочнице

Песочница Т-Инвестиций использует виртуальные деньги и отдельные счета.
Реальные счета и их портфели автоматически сюда не копируются. Если нужно
проверить похожий состав, создайте виртуальные счета, пополните их и соберите
нужный портфель. У ACCOUNT источник тоже должен быть счётом песочницы.
Официальные правила и отличия от реальной торговли — в
[документации песочницы](https://developer.tbank.ru/invest/intro/developer/sandbox).

Этот способ запускает само приложение с вашим конфигом. `make e2e` запускает
другую проверку: готовые тестовые сценарии с автоматически создаваемыми
и удаляемыми счетами. Ручные счета из этой инструкции автоматически не удаляются.
Ни эта проверка, ни успешные E2E не гарантируют доходность или исполнение
по тем же ценам на реальном счёте.

Все команды выполняются из каталога проекта после
[локальной установки](../README.md#локальная-установка). В примерах используется
существующий Python-интерфейс `Runner`: у `main.py` нет флага `--sandbox`.
**Не запускайте обычный `main.py` в расчёте, что sandbox-токен сам переключит API.**
Ниже адрес песочницы выбирается явно; production-токен не используется.

## Токен

Выпустите sandbox-токен по
[официальной инструкции](https://developer.tbank.ru/invest/intro/intro/token).
Создайте в каталоге проекта файл `.sandbox_token`, содержащий только этот токен
в одной строке. Он исключён из Git; не добавляйте его принудительно в коммит.

```bash
chmod 600 .sandbox_token
```

Обёртка `scripts.sandbox_token` передаст значение дочерней команде через
`SANDBOX_TOKEN`, не напечатает его и не поместит в аргументы.
Непустой `SANDBOX_TOKEN` в окружении имеет приоритет над файлом; убедитесь,
что там не осталось другого токена. Если хотите использовать именно файл:

```bash
unset SANDBOX_TOKEN
```

Для примеров с блоками Python файл должен уже содержать токен: стандартный
ввод занят текстом программы, скрытый интерактивный запрос здесь недоступен.

## Создать и пополнить счёт

Команда создаёт новый счёт и пополняет его на 10 000 виртуальных рублей.
`units` задаёт целые рубли, `nano` — их дробную часть; здесь она равна нулю.
Каждый повтор команды создаёт ещё один счёт, не пополняет прежний.

```bash
.venv/bin/python -m scripts.sandbox_token -- .venv/bin/python - <<'PY'
from t_tech.invest import MoneyValue
from uuid import uuid4
from scripts.sandbox_lifecycle import sandbox_client, safe_call

name = "autorepeater-manual-dst-" + uuid4().hex
print("Имя создаваемого счёта:", name, flush=True)
with sandbox_client() as client:
    account = safe_call(
        client.sandbox.open_sandbox_account,
        name=name,
    )
    print("Создан account_id:", account.account_id, flush=True)
    safe_call(
        client.sandbox.sandbox_pay_in,
        account_id=account.account_id,
        amount=MoneyValue(currency="rub", units=10000, nano=0),
    )
    print("Счёт пополнен виртуальными рублями")
PY
```

Сохраните выданный `account_id`; обычно это UUID. Не заменяйте его номером
реального счёта. Если пополнение упало, ID уже выведен и новый счёт может
существовать. Если открытие или пополнение завершилось тайм-аутом,
сначала проверьте состояние счёта. Не повторяйте операцию,
пока не выясните её результат. Если неизвестно, создался ли счёт,
найдите его по выведенному уникальному имени. Список счетов можно получить так:

```bash
.venv/bin/python -m scripts.sandbox_token -- .venv/bin/python - <<'PY'
from scripts.sandbox_lifecycle import sandbox_client, safe_call

with sandbox_client() as client:
    for account in safe_call(client.users.get_accounts).accounts:
        print(account.name, account.id)
PY
```

По правилам песочницы пополнение доступно в рублях; вычесть деньги обратным
пополнением нельзя. Ограничения, срок хранения счетов и эмуляция сделок
описаны в официальной документации. Песочница не воспроизводит реальную
ликвидность и все расчёты брокера, а её доступность может меняться.

## Подготовить конфиг

Следуйте [инструкции своего конфига](../README.md#свой-конфиг).
Для первого примера можно скопировать `gold.json`, задать `name=MY_INDEX`
и сохранить его в `../autorepeater-configs/index/my-index.json`.
Это технический пример, не рекомендация инвестировать в GOLD.

```bash
export INDEX_CONFIG_DIR='../autorepeater-configs/index'
unset IMOEX_CONFIG_PATH
export SANDBOX_ACCOUNT_ID='<account_id из создания счёта>'
export SANDBOX_ALGORITHM='INDEX'
export SANDBOX_SOURCE='MY_INDEX'
export SANDBOX_DEBUG='1'
```

Замените значение `SANDBOX_ACCOUNT_ID` выданным идентификатором.
`SANDBOX_ALGORITHM`, `SANDBOX_SOURCE` и `SANDBOX_DEBUG` ниже читает пример
Python, а не `main.py`. Для COMPOSITE задайте `COMPOSITE_CONFIG_DIR`
и пути к конфигам дочерних стратегий. В `SANDBOX_ALGORITHM` укажите `COMPOSITE`,
а в `SANDBOX_SOURCE` — имя своего конфига.

## Проверить план

Запустите один разовый проход с `SANDBOX_DEBUG=1`:

```bash
.venv/bin/python -m scripts.sandbox_token -- .venv/bin/python - <<'PY'
import os
from autorepeater.runner import Runner, RunnerParams
from autorepeater.strategies import prepare_strategy

debug = os.environ["SANDBOX_DEBUG"]
if debug not in ("0", "1"):
    raise ValueError("SANDBOX_DEBUG must be 0 or 1")

Runner(
    token=os.environ["SANDBOX_TOKEN"],
    prepared_strategy=prepare_strategy(
        os.environ["SANDBOX_ALGORITHM"], os.environ["SANDBOX_SOURCE"],
    ),
    dst=os.environ["SANDBOX_ACCOUNT_ID"],
    params=RunnerParams(debug=debug == "1"),
    sandbox=True,
).run_sync()
PY
```

Приложение читает виртуальный счёт и котировки, строит и проверяет план,
показывает `Target`, `Policy`, `Intent`, `Execution: debug=True`, но не отправляет
заявки. Порядок чтения отчёта — в
[README](../README.md#проверка-перед-реальными-сделками).
`run_sync()` заканчивается после одного прохода; подписка на события не открывается.
Это не запуск облачной функции.

## Выполнить виртуальные сделки

После проверки плана разрешите исполнение только в песочнице:

```bash
export SANDBOX_DEBUG='0'
```

Повторите **тот же блок Python** из предыдущего раздела: `sandbox=True`
должен остаться. Приложение отправит разрешённые виртуальные заявки
и проверит ответы брокера. После пополнения или изменения конфига повторите
сначала проверку с `SANDBOX_DEBUG=1`, затем при необходимости исполнение.
Цены живые: второй проход не обязан иметь тот же план, что первый.
Закрытые торги, ограничения и ошибки не следует считать успешной проверкой.
Не запускайте одновременно несколько торговых проходов по одному счёту.

Для просмотра результата можно запросить виртуальный портфель:

```bash
.venv/bin/python -m scripts.sandbox_token -- .venv/bin/python - <<'PY'
import os
from decimal import Decimal
from scripts.sandbox_lifecycle import sandbox_client, safe_call

with sandbox_client() as client:
    portfolio = safe_call(
        client.sandbox.get_sandbox_portfolio,
        account_id=os.environ["SANDBOX_ACCOUNT_ID"],
    )
    for position in portfolio.positions:
        quantity = Decimal(position.quantity.units) + (
            Decimal(position.quantity.nano) / Decimal(1000000000)
        )
        print(position.instrument_uid, quantity)
PY
```

Вывод содержит UID бумаги и количество штук, не лотов. Сопоставляйте UID
с отчётом приложения; для валютной позиции количество означает сумму валюты.

## Источник для ACCOUNT

Для ACCOUNT нужны **два отдельных виртуальных счёта**:

1. Создайте источник первой командой, заменив префикс имени на `autorepeater-manual-src-`,
   и запишите его ID. Пополните его виртуальными деньгами.
2. Соберите нужный исходный портфель. Например, запустите приведённый выше
   INDEX или COMPOSITE на ID источника: сначала debug, затем виртуальные сделки.
   Для подобия реального портфеля состав, бюджет и параметры задайте самостоятельно;
   точной автоматической копии реального счёта этот пример не создаёт.
3. Создайте и пополните второй счёт с префиксом имени `autorepeater-manual-dst-`.
   Укажите его ID в `SANDBOX_ACCOUNT_ID`.
4. Скопируйте ACCOUNT-конфиг в отдельный файл и задайте `name=MY_ACCOUNT`,
   а в `source_account_id` — UUID виртуального источника, не реальный ID.
   Остальные поля должны соответствовать вашей проверяемой настройке.

```bash
export ACCOUNT_CONFIG_PATH='../autorepeater-configs/account/my-account.json'
unset ACCOUNT_CONFIG_DIR
export SANDBOX_ALGORITHM='ACCOUNT'
export SANDBOX_SOURCE='MY_ACCOUNT'
export SANDBOX_DEBUG='1'
```

Повторите блок `Runner(..., sandbox=True).run_sync()` сначала с debug,
затем, если план понятен, с `SANDBOX_DEBUG=0`. Источник должен уже содержать
бумаги. Если на нём есть только деньги, ACCOUNT не сможет построить цель
с положительным количеством бумаг.
Все счета ACCOUNT, в том числе вложенного в COMPOSITE, здесь относятся к песочнице.

## Удалить ручные счета

После проверки закройте каждый созданный вами счёт по точному ID.
Следующий пример удаляет только указанный `SANDBOX_ACCOUNT_ID`:

```bash
.venv/bin/python -m scripts.sandbox_token -- .venv/bin/python - <<'PY'
import os
from scripts.sandbox_lifecycle import sandbox_client, safe_call

with sandbox_client() as client:
    safe_call(
        client.sandbox.close_sandbox_account,
        account_id=os.environ["SANDBOX_ACCOUNT_ID"],
    )
    print("Указанный виртуальный счёт закрыт")
PY
```

Для ACCOUNT повторите закрытие для источника, подставив его ID.
Не удаляйте чужие тестовые счета по имени или сразу весь список песочницы.
После закрытия старые IDs в конфиге больше не подходят: новая проверка
требует новых счетов и обновления `source_account_id`/`SANDBOX_ACCOUNT_ID`.
