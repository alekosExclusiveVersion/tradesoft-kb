# Tradesoft KB — правила работы

## Python-интерпретатор для эвалов и пересборки индекса

Все скрипты поиска зависят от `pymorphy3` (морфология в `scripts/stem.py`).
Системный `python3` (Homebrew/приложенный) pymorphy3 **не имеет** — при запуске
поиск работает в пассивном fallback-режиме (возвращает слово в нижнем регистре,
без морфологических корней). Из-за этого результаты поиска и эвалов **искажаются**:

- запрос «как выгрузить товары на авито» при системном python даёт топ-1
  «О маркетплейсе» (стем «авить» не находит «avito» в индексе);
- тот же запрос через `.venv` даёт корректные «Добавление товаров на маркетплейс»
  и «Выгрузка каталога товаров на сайт Avito».

Правила:

- **Эвалы (`scripts/evaluate_cross.py`) запускать `.venv/bin/python`**:
  ```bash
  .venv/bin/python scripts/evaluate_cross.py
  .venv/bin/python scripts/evaluate_cross.py --file scripts/eval_queries.json
  ```
  `python3 scripts/evaluate_cross.py` даёт недостоверные метрики (заниженные
  prec@1/mrr, ложные fails) — так делать нельзя.
- **Пересборку индекса (`scripts/freshness.py --fix`) запускать тем же
  `.venv/bin/python`**, что и API-сервер: fingerprint, записываемый в
  `cache/build_meta.json`, должен совпадать с fingerprint-ом рабочего индекса
  (иначе health/API видят `stale` и пересобирают при старте).

Живой API (`com.tradesoft.cross-search-api.plist`) уже работает на
`.venv/bin/python` — пересборку/эвалы нужно выполнять в том же окружении.