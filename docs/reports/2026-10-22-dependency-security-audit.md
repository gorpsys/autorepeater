# Проверка Python-зависимостей, 22.10.2026

Проверен состав security-ветки выпуска 5.3.1: 48 Python-пакетов,
из них 24 входят в runtime, остальные 24 нужны для разработки или установки.
Версии взяты из `runtime-lock.json`, `vendor/inventory.json` и фактически
установленного окружения после `scripts.install_dependencies --development`.
Также проверены 31 встроенный компонент: 18 в pip, 12 в setuptools
и один в isort. Совпадающие пары имя/версия объединены; вместе с двумя
прежними версиями проверены 78 уникальных пар. OSV обработал все 78,
PyPI вернул данные для 77; исключение SDK разобрано ниже.

## Источники и метод

- [GitHub Dependabot](https://github.com/gorpsys/autorepeater/security/dependabot):
  открытые предупреждения основной ветки, включая все страницы ответа API.
- [OSV querybatch](https://google.github.io/osv.dev/api/#tag/vulnerabilities/operation/OSV_QueryAffectedBatch):
  точные имя, версия и ecosystem `PyPI`; затем полные сведения найденных advisory.
- [PyPI JSON API](https://docs.pypi.org/api/json/):
  `https://pypi.org/pypi/<package>/<version>/json`, поле `vulnerabilities`.

Проверены также прежние `certifi 2026.7.22` и `pip 24.0`, чтобы сохранить
результат до исправления. Отозванные advisory исключены; GHSA и PYSEC
объединены по aliases, а не посчитаны отдельными уязвимостями.
PyPI использует данные OSV: это два способа проверки, не две независимые базы.

## Найдено и исправлено

| Пакет до исправления | Advisory | Уровень | Исправленная версия |
| --- | --- | --- | --- |
| certifi 2026.7.22 | [GHSA-6q48-6c7w-9gw2](https://github.com/advisories/GHSA-6q48-6c7w-9gw2) | critical | 2026.10.19 |
| pip 24.0 | [GHSA-4xh5-x5gv-qwph](https://github.com/advisories/GHSA-4xh5-x5gv-qwph) | moderate | 25.3 |
| pip 24.0 | [GHSA-6vgw-5pg2-w6jp](https://github.com/advisories/GHSA-6vgw-5pg2-w6jp) | low | 26.0 |
| pip 24.0 | [GHSA-58qw-9mgm-455v](https://github.com/advisories/GHSA-58qw-9mgm-455v) | moderate | 26.1 |
| pip 24.0 | [GHSA-jp4c-xjxw-mgf9](https://github.com/advisories/GHSA-jp4c-xjxw-mgf9) | moderate | 26.1 |
| pip 24.0 | [GHSA-wf93-45jw-7689](https://github.com/advisories/GHSA-wf93-45jw-7689) | moderate | 26.1.2 |
| pip 24.0 | [GHSA-qwm4-qh6w-59xr](https://github.com/advisories/GHSA-qwm4-qh6w-59xr) | moderate | 26.2 |

`certifi` обновлён до 2026.10.19 с пересозданием обоих runtime lock-файлов
и requirements. Сохранены MPL-2.0 и ссылка на точный исходный код.
В Dependabot одна эта уязвимость отображалась шесть раз по разным requirements.
Предупреждения основной ветки не закрываются до мержа исправления.

`pip` закреплён на 26.2.2 в dev inventory и сгенерированных dev requirements.
Штатный установщик скачивает его с PyPI, проверяет SHA-256 и устанавливает
при `make setup`. Колесо в Git не хранится; остальные runtime-пакеты не менялись.
Регрессионный тест проверяет наличие безопасного pip среди dev-артефактов,
точный хеш, requirements и отсутствие pip в облачном runtime lock.

## Итоговый состав

Повторная проверка в 14:37 UTC: OSV обработал все 48 текущих версий,
PyPI вернул сведения для 47; исключение SDK описано ниже. Для этих
установленных distributions advisory не найдено. Это не включает встроенные
библиотеки pip: их результат приведён отдельно ниже. Старые версии проверены
отдельно и в эту цифру не включены. Отсутствие записей не означает отсутствия
уязвимостей.

| Пакет | Версия | Область |
| --- | --- | --- |
| antlr4-python3-runtime | 4.13.2 | runtime |
| astor | 0.8.1 | runtime |
| astroid | 4.3.3 | dev |
| cachetools | 5.5.2 | runtime |
| certifi | 2026.10.19 | runtime |
| chardet | 7.6.0 | dev |
| charset-normalizer | 3.5.2 | runtime |
| coverage | 7.16.2 | dev |
| deprecation | 2.1.0 | runtime |
| diff-cover | 10.6.0 | dev |
| dill | 0.4.1 | dev |
| flake8 | 7.4.1 | dev |
| grpcio | 1.84.0 | runtime |
| grpcio-tools | 1.81.1 | runtime |
| idna | 3.20 | runtime |
| iniconfig | 2.3.1 | dev |
| iprotopy | 0.3.0 | runtime |
| isort | 9.0.2 | dev |
| jinja2 | 3.1.6 | dev |
| markupsafe | 3.0.4 | dev |
| mccabe | 0.7.0 | dev |
| mypy | 1.18.2 | dev |
| mypy-extensions | 1.1.0 | dev |
| mypy-protobuf | 5.1.0 | runtime |
| packaging | 26.3 | runtime, dev |
| pathspec | 1.1.1 | dev |
| pip | 26.2.2 | dev, bootstrap |
| platformdirs | 4.12.3 | dev |
| pluggy | 1.6.0 | dev |
| proto-schema-parser | 2.1.0 | runtime |
| protobuf | 6.33.6 | runtime |
| pycodestyle | 2.15.0 | dev |
| pyflakes | 4.0.2 | dev |
| pygments | 2.21.0 | dev |
| pylint | 4.1.2 | dev |
| pytest | 9.1.1 | dev |
| pytest-cov | 7.1.0 | dev |
| python-dateutil | 2.9.0.post0 | runtime |
| python-json-logger | 3.3.0 | runtime |
| requests | 2.34.2 | runtime |
| sentry-sdk | 2.71.0 | runtime |
| setuptools | 84.0.0 | runtime |
| six | 1.17.0 | runtime |
| t-tech-investments | 1.51.0 | runtime, ограничение ниже |
| tomlkit | 0.15.1 | dev |
| types-protobuf | 7.35.1.20260906 | runtime |
| typing-extensions | 4.16.0 | runtime, dev |
| urllib3 | 2.8.0 | runtime |

## Встроенные библиотеки pip

Дополнительно проверены 18 компонентов из `pip/_vendor/vendor.txt`,
сверенных с CycloneDX SBOM `pip/_vendor/bom.cdx.json`. Они не видны в
`pip list`; одноимённая установленная отдельно библиотека не заменяет
встроенную копию. После объединения одинаковых пар имя/версия проверено
66 пар до добавления встроенных зависимостей setuptools/isort:
текущие distributions, встроенные компоненты pip и две прежние версии.

| Компонент в pip 26.2.2 | Запись | Применимость / решение |
| --- | --- | --- |
| msgpack 1.1.2 | [GHSA-6v7p-g79w-8964](https://github.com/advisories/GHSA-6v7p-g79w-8964), high, исправлено в 1.2.1 | Сбой при повторном использовании Unpacker после ошибки. В wheel pip нет нативного расширения msgpack; cachecontrol использует `loads`, не потоковый Unpacker. Уязвимый сценарий не подтверждён. |
| setuptools 70.3.0 | [GHSA-5rjg-fvgr-3xxf](https://github.com/advisories/GHSA-5rjg-fvgr-3xxf), high, исправлено в 78.1.1 | В pip включён только pkg_resources; `setuptools.package_index.PackageIndex` отсутствует. |
| setuptools 70.3.0 | [GHSA-h35f-9h28-mq5c](https://github.com/advisories/GHSA-h35f-9h28-mq5c), moderate, исправлено в 83.0.0 | Уязвимая сборка sdist и FileList отсутствуют в vendored pkg_resources. Установленный отдельно runtime setuptools уже 84.0.0. |
| urllib3 2.7.0 | [GHSA-8988-9cw3-xx77](https://github.com/advisories/GHSA-8988-9cw3-xx77), high | Ошибки разделения TLS-настроек HTTPS-прокси и сервера. Исправлено в 2.8.0. |
| urllib3 2.7.0 | [GHSA-gh4c-6fx4-qh6g](https://github.com/advisories/GHSA-gh4c-6fx4-qh6g), moderate | Зацикливание при обработке chunked Deflate. Исправлено в 2.8.0. |
| urllib3 2.7.0 | [GHSA-vxq7-64xx-v4gw](https://github.com/advisories/GHSA-vxq7-64xx-v4gw), high | Неограниченная буферизация поля размера HTTP chunk. Исправлено в 2.8.0. |

**Остаточный риск:** последняя стабильная версия pip на PyPI при проверке,
26.2.2, всё ещё содержит urllib3 2.7.0. Установить отдельно urllib3 2.8.0
недостаточно: это не меняет `pip._vendor.urllib3`. В приложении используется
отдельная закреплённая urllib3 2.8.0; pip и его vendored-библиотеки не входят
в облачный runtime payload. Это не закрывает риск сетевых команд самого pip
при разработке, разрешении зависимостей и проверке vendored-архива.

Самодельный wheel pip, подмена встроенных файлов и prerelease-версия не
добавлялись. Для полного устранения нужен официальный pip с обновлённой
встроенной urllib3 либо отдельно согласованная замена установщика. До этого
используются только закреплённые официальные HTTPS-источники и хеши;
управляемая установка после скачивания wheel-файлов работает с `--no-index`.
Эти ограничения уменьшают поверхность атаки, но не объявляются исправлением
advisory встроенной urllib3.

## Встроенные библиотеки setuptools и isort

Проверка `RECORD` всех установленных distributions выявила встроенный код
ещё в setuptools и isort. Версии setuptools получены из вложенных
`*.dist-info/METADATA`, версия tomli в isort сверена по `__version__`.
В итоговой проверке 14:54 UTC для этих компонентов advisory не найдено:

| Владелец | Компонент | Версия |
| --- | --- | --- |
| setuptools | autocommand | 2.2.2 |
| setuptools | backports.tarfile | 1.2.0 |
| setuptools | importlib-metadata | 8.7.1 |
| setuptools | jaraco.context | 6.1.0 |
| setuptools | jaraco.functools | 4.4.0 |
| setuptools | jaraco.text | 4.0.0 |
| setuptools | more-itertools | 10.8.0 |
| setuptools | packaging | 26.0 |
| setuptools | platformdirs | 4.4.0 |
| setuptools | tomli | 2.4.0 |
| setuptools | wheel | 0.46.3 |
| setuptools | zipp | 3.23.0 |
| isort | tomli | 2.4.1 |

## Проверки изменений

Управляемая установка обновлённого dev-набора прошла. Новый регрессионный
тест прошёл цикл RED -> GREEN; профильный набор: 314 passed.
`make lint`, `make check-runtime-lock`, `git diff --check` прошли.
Полный `make coverage`: 5450 passed, общее покрытие 99.45%, diff 100%.
Live E2E и production handler при этой проверке не запускались.

## Границы проверки

У SDK `t-tech-investments 1.51.0` нет страницы этой версии на PyPI;
версионный API вернул HTTP 404. OSV не содержит записей для этой пары
имени и версии. SDK не объявляется проверенным или безопасным только на этом
основании; его закреплённые транзитивные зависимости проверены отдельно.
Это поиск известных advisory, не аудит исходников и не гарантия безопасности.

Проверка не охватывает системный Python, ОС, нативные библиотеки внутри wheel,
Git, Make, GitHub Actions и бинарный `yc`. Их нельзя считать проверенными
по результату запросов для пакетов PyPI.

Первую установку выполняет pip, который поставляется с `venv`;
управляемая dev-версия заменяет его после проверки скачанных wheel-файлов.
Существующие окружения вне этой рабочей копии автоматически не обновлялись.
`pip` штатной сборки Yandex Functions принадлежит облачному провайдеру;
в автономном архиве установщика и установки зависимостей в облаке нет.
