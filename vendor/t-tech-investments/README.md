# Резервный t-tech-investments 1.51.0

Здесь хранится неизменённый восстановленный wheel SDK и точная копия его
Apache-2.0 LICENSE с Copyright 2023 Tinkoff. Отдельного NOTICE в wheel нет.
Лицензия SDK не определяет лицензию приложения; одобрение T-Bank не подразумевается.

Именно эта версия и эти байты хранятся в Git как исключение
для SDK с доступными исходниками GitHub. Исключение не применяется к другим
версиям SDK или пакетам. Решение и проверенные факты находятся в
[inventory](../inventory.json).

Все 125 файлов `t_tech/` совпали с исходниками
[Invest-SDK/invest-python](https://github.com/Invest-SDK/invest-python/tree/0110e451cb0316e975ac5fc5ee027b95246f6396).
Это подтверждает сопоставимость кода, но не независимо проверенный официальный
hash wheel. В manifest сохранено `official_hash_verified: false`, `approvals: []`.
Ни одна конкретная пара reserve/index hashes не одобрена.

Локальная проверка `scripts.sdk_wheel.load_vendored_wheel` проверяет manifest,
точные SHA-256/size wheel и LICENSE, ZIP limits/CRC/RECORD, metadata и пути.
Результат содержит неизменяемые проверенные байты. Она не обращается к сети
и не устанавливает SDK. Общий helper установки и сборки выбирает основной
официальный SDK либо этот резерв при разрешённой недоступности. WARNING
содержит безопасную причину, version и путь. TLS/hash/metadata/ZIP ошибки
и неодобренное расхождение index/reserve hashes фатальны.
Forced vendored выполняет bounded index-only probe, без official SDK download.
Команды и правила источников: [технический README](../../scripts/README.md).

Wheel: `t_tech_investments-1.51.0-py3-none-any.whl`, 316190 байт,
SHA-256 `f0fe22cbed727cecd9971200032d893c19702838b971c08abc2831fe25fa2856`.
Manifest и [runtime lock](../../runtime-lock.json) фиксируют допустимые bytes;
установка той же версии из непроверенного artifact не считается подтверждённой.

Обновление версии или байтов требует отдельного review: источник, hash,
metadata, лицензии, inventory, manifest и тесты. После восстановления
официального индекса необходимо независимо сверить его hash. При несовпадении
нужно явное одобрение точной версии и пары hashes с автором, датой UTC и
ссылкой на обсуждение; автоматическая запись одобрения запрещена.
Изменение hash или версии требует нового одобрения. До изменения схемы
подтверждённого происхождения валидатор принимает только текущий caveat.
