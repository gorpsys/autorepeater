# Инвентарь резервных артефактов

`inventory.json` фиксирует проверку источников и лицензий 12 октября 2026.
Это inventory текущего seed CPython 3.12/Linux без extras: 47 Python
distributions, включая 24 runtime, и проектный deployment tool yc 1.40.0.
Числа описывают снимок, не будущие инварианты. Python/Git/Make/Actions
предоставляются окружением и не копируются в Git.

Каждая запись содержит точную версию, scopes и обязательные зависимости,
source URL/tag/immutable commit, evidence URL с digest прочитанных файлов,
лицензию и её obligations, candidate filename/size/SHA-256/tag/path,
решение о хранении и его причину. PyPI hashes кандидатов получены из version
JSON; это inventory, не runtime lock и не свидетельство установки.
Реальные source modules прочитаны на указанных commit, ссылка из metadata
сама по себе не считается доказательством matching source.

Все Python-проекты имеют matching GitHub source; готовый wheel на GitHub
для исключения не требуется. Их binaries не хранятся в этом репозитории.
Transient network error означает unverified, а не отсутствие исходников.
Автоматической source сборки и обновления версий нет.

## Сохранённый SDK

В Git хранится recovered `t-tech-investments==1.51.0` несмотря
на matching GitHub source. Исключение относится только к wheel размером
316190 байт, SHA-256
`f0fe22cbed727cecd9971200032d893c19702838b971c08abc2831fe25fa2856`.
Бинарник и оригинальный [LICENSE](t-tech-investments/LICENSE) сохранены.
Исключение не одобряет конкретную пару reserve/index hashes, approvals пуст.
Официальный hash независимо не проверен: совпадение кода не устраняет эту
оговорку о происхождении и не разрешает новый artifact hash.

Source: https://github.com/Invest-SDK/invest-python,
commit `0110e451cb0316e975ac5fc5ee027b95246f6396`.
Обе version declarations равны 1.51.0; все 125 файлов `t_tech/` recovered
wheel совпали с source Git blobs. ZIP CRC исправен; LICENSE Apache-2.0
содержит Copyright 2023 Tinkoff, отдельного NOTICE в wheel нет.
Backend `poetry.core.masonry.api` требует `poetry-core>=1.0.0`.
Автоматической source сборки нет.

## Лицензии и payload

Git binary eligibility (`storage`) и обязанности при распространении
payload (`license`) проверяются отдельно. GitHub source исключает binary
из Git даже при условной или неизвестной лицензии. Для разрешённого
резерва обязательны проверенные права и все licence/source/notice materials.
Состав распространяемого ZIP проверяется по фактическому runtime lock,
metadata и retained notices. Inventory не заменяет эту проверку.

GPL/LGPL/MPL не являются общим запретом. Например, certifi и pathspec имеют
MPL-2.0, pylint GPL-2.0-or-later, astroid LGPL-2.1-or-later. Их source form,
licence/copyright/NOTICE и сведения о corresponding source надо сохранять
по применимым условиям, не удаляя обязательные SDK dependencies.
`fulfilled` отражает результат проверки условий предложенного хранения,
а не заменяет последующую проверку состава распространяемого ZIP.

Для iprotopy 0.3.0 MIT объявлена в pyproject и METADATA, с
attribution Ruslan Sirazhetdinov и source reference. Это upstream
declaration, не только classifier. Текста отдельного licence/copyright
нет в полном repo tree, README, hash-verified wheel и sdist. Отсутствие
файла само по себе не означает отсутствия permission или запрета.
Все 20 файлов wheel совпали с tag v0.3.0, commit
`031ae0a6536f07964c9802dfe099efeb701e1c25`; pyproject version 0.1.0 устарел.
Repo binary исключён по matching source. Offline payload сохраняет исходную
METADATA; THIRD_PARTY_NOTICES/iprotopy содержит только автора, версию и
[точную source-ссылку](https://github.com/irusland/iprotopy/tree/031ae0a6536f07964c9802dfe099efeb701e1c25).
Отдельного upstream LICENSE нет. Дополнительный MIT текст, предполагаемые
copyright/год не добавляются, контакт с автором не требуется.
`conditional/fulfilled=false` сохранено: notice-only состав не объявляет
неподтверждённые upstream обязательства выполненными.

Certifi MPL-2.0 сохраняет исходную форму .py/cacert.pem, лицензию и notice
о доступе к исходникам. Для ANTLR 4.13.2 и grpcio-tools 1.81.1 locked wheels
не содержат standalone licence text. В payload добавляются точные upstream
bytes из [ANTLR LICENSE.txt](../third_party/antlr4-python3-runtime-4.13.2/LICENSE.txt)
и [grpcio-tools LICENSE](../third_party/grpcio-tools-1.81.1/LICENSE).
Источник и SHA-256 каждого supplement проверяются; это оригинальные материалы,
не составленные заново лицензии:

| Пакет | Неизменяемый источник | SHA-256 текста |
| --- | --- | --- |
| ANTLR 4.13.2 | [cc82115](https://github.com/antlr/antlr4/blob/cc82115a4e7f53d71d9d905caa2c2dfa4da58899/LICENSE.txt) | `3db1fb3ee79a4b4f9918fc4d0f6133bf18a3cf787f126cd22f8aa9b862281c0c` |
| grpcio-tools 1.81.1 | [e84a8a2](https://github.com/grpc/grpc/blob/e84a8a2f04095f2772ba42a4abccde4f9243e75b/LICENSE) | `58c86a3f2b5cca0ebdf9ed3e7f4e6dc38e3b38796d8c3dc92064c62a4e39d0dd` |

Бинарники этих пакетов в Git не хранятся. Другие неизвестные/unfulfilled
licence obligations или отсутствующие обязательные материалы блокируют payload.

yc 1.40.0 исключён из хранения и резерва (`excluded-by-user` в плане).
Inventory сохраняет исходные `storage=blocked` и unknown licence/source evidence;
хранение не ожидает их выяснения. Deployment workflow использует только
стандартную официальную загрузку с отказом при недоступности.
LFS/Release assets не используются.

## Проверка

Из корня проекта: `python -m scripts.vendor_inventory`.
Проверка локальная, без сети, SDK imports, установки, брокера и облака.
Она отклоняет malformed schema, identity/path/hash/tag нарушения,
неразрешённые overrides, symlinks и undeclared files во всём vendor tree,
включая untracked/ignored. Сохранённый binary требует всех заявленных
accompanying materials. Новые файлы не получают неограниченный allowlist
по расширению. Runtime lock, receipts, источники, per-package fallback и
команды реального smoke описаны в [техническом README](../scripts/README.md).
