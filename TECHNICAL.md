# Tradesoft KB — Техническая документация

> Дата документа: 2026-09-03
> Версия проекта: 5 коммитов (hybrid search, eval, self-learning ranking, fetcher/manifest)

---

## 1. Обзор архитектуры

Tradesoft KB — гибридная поисковая система по технической документации продуктов
экосистемы Tradesoft (Parts.Intellect, Parts.Resource, PartsInstance REST API,
service API, Диадок, Маркетплейсы, ТСД, Wazzup и др.).

### 1.1 Слой хранения

| Компонент | Технология | Назначение |
|-----------|-----------|------------|
| **FTS5-индекс** | SQLite FTS5 | Полнотекстовый поиск по заголовкам и содержимому страниц (`cache/kb_index.db`) |
| **Typesense** | Docker-контейнер (порт 8108) | Векторный индекс для семантического поиска (embedding-векторы документов) |
| **Ollama** | Docker-контейнер (порт 11434) | Генерация embeddings запросов/индексации (модель `qwen3-embedding:4b`) |
| **Логи доступа** | SQLite (`cache/kb_access.db`) | Асинхронное логирование поисковых запросов, кликов, открытий страниц |

### 1.2 Слой обработки

| Модуль | Файл | Назначение |
|--------|------|------------|
| `search.py` | `scripts/search.py` | Ядро поиска: нормализация, стемминг, FTS5-поиск, гибридный поиск (FTS+vector), определение продукта, product-context confinement |
| `fusion.py` | `scripts/fusion.py` | Ранжирование: Reciprocal Rank Fusion (RRF), линейная модель, feature extraction |
| `rank_model.py` | `scripts/rank_model.py` | Загрузка весов самообучающейся модели, `weight_adjustment()` |
| `rank_train.py` | `scripts/rank_train.py` | Обучение весов по неявным сигналам (клики), накопление пар, сохранение в `cache/rank_weights.json` |
| `stem.py` | `scripts/stem.py` | Морфологический стемминг (pymorphy3) для русского языка |
| `layout.py` | `scripts/layout.py` | Коррекция ошибок раскладки клавиатуры (ЙЦУКЕН→Cyrillic) |
| `embed.py` | `scripts/embed.py` | Генерация embeddings через Ollama API |
| `typesense_client.py` | `scripts/typesense_client.py` | Клиент Typesense для индексации и поиска |
| `vector_search.py` | `scripts/vector_search.py` | Векторный поиск через Typesense |
| `config.py` | `scripts/config.py` | 12-factor конфигурация из переменных окружения |
| `access_log.py` | `scripts/access_log.py` | Асинхронное логирование в SQLite (фоновый writer + очередь, thread-safe) |

### 1.3 Слой индексации (ETL)

| Модуль | Файл | Назначение |
|--------|------|------------|
| `fetch.sh` | `scripts/fetch.sh` | Загрузка файлов документации из remote-репозиториев по манифесту |
| `parse.py` | `scripts/parse.py` | Парсинг HTML→Markdown (`htm.md` файлы в `cache/<product>/parsed/`) |
| `build_index.py` | `scripts/build_index.py` | Инкрементальная индексация в FTS5 (только изменённые страницы) |
| `build_vector_index.py` | `scripts/build_vector_index.py` | Инкрементальная индексация embeddings в Typesense |
| `reembed_product.py` | `scripts/reembed_product.py` | Переиндексация embeddings для одного продукта |
| `manifest.json` | `manifest.json` (корень репозитория) | Манифест продуктов: source URL, версии, счётчики страниц/изображений, `alljs_hash` |

---

## 2. Конвейер обработки данных (ETL)

```
product-doc.tradesoft.ru (remote documentation)
     │  fetch.sh (curl, all.js + HTML-страницы, parallel x12)
     ▼
 cache/<product>/
   ├── all.js + toc.json          ← parse.py toc (дерево страниц)
   ├── html/*.htm                 ← сырые HTML-страницы
   └── parsed/*.htm.md (+.imgs)   ← parse.py html (Markdown, заголовки ##/###)
     │
     ├──→ build_index.py ──────→ cache/kb_index.db (FTS5)
     ├──→ build_vector_index.py ─→ Typesense (vector embeddings)
     └──→ products/<product>/{index.md, content.md, images/}
          (fetch.sh: build_index/build_content/download_images)
                                  └──→ manifest.json (update_manifest, в корне)
```

`fetch.sh` — оркестратор: скачивает `all.js`, извлекает `toc.json`, качает
HTML («-z» conditional-GET, только изменившиеся), парсит в Markdown, качает
изображения, собирает `index.md`/`content.md` и обновляет `manifest.json`.
Затем вызывает `scripts/build_index.py`. `service-api` страницы генерируются
отдельно из OpenAPI-спецификации (`convert_svc_spec.py`).

### 2.1 fetch.sh

Загружает файлы документации по URLs из `manifest.json` (в корне репозитория).
Манифест содержит 12 продуктов (на 2026-09-03): `delivery_schedule`, `diadok`,
`marketplace`, `parts-index-rest-api`, `parts-intellect-changes`,
`parts-intellect-guide` (v5.26), `parts-intellect-synch`, `parts-resource-changes`,
`parts-resource-guide` (v6.74), `parts-resource-rest-api`, `seo-guide`, `tsd`,
`wazzup`. Для каждого продукта хранится `source` (URL документации), `version`,
число `pages`/`images`, `images_bytes` и контрольная сумма `alljs_hash`
(используется для мониторинга обновлений источника).

> Примечание: `parts-index-rest-api` и `seo-guide` есть в манифесте/индексе,
> но не все из них входят в `PRODUCTS` (карту детекта в `search.py`) —
> каталог `PRODUCTS` см. в разделе 4.1.

### 2.2 parse.py

Конвертирует исходные файлы в Markdown-формат (`*.htm.md`). Каждый продукт хранится в `cache/<product>/parsed/`. Имя файла страницы = часть ключа документа (`product__page`).

### 2.3 build_index.py — FTS5

Инкрементальная индексация: сравнивает `mtime` файлов с хранимыми в БД. Страницы >15000 символов разбиваются на чанки по заголовкам (`##`, `###`).

**Схема FTS5** (`chunks_fts`):
- `title` — заголовок страницы (индексируется)
- `content` — содержимое чанка (индексируется)
- `product`, `page`, `path`, `chunk` — UNINDEXED (для идентификации)

FTS5 использует `unicode61` tokenizer (по умолчанию), который **не выполняет стемминг русских слов** — это ключевое ограничение системы.

### 2.4 build_vector_index.py — Typesense

Генерирует embeddings для каждого чанка через Ollama (модель `qwen3-embedding:4b`) и загружает в Typesense-коллекцию. Инкрементальная: пропускает чанки с существующими embeddings.

---

## 3. Нормализация запроса

Система использует две независимые «ноги» с разной нормализацией:

### 3.1 FTS-нога

```
raw query
  │
  ├─ terms(query) ──→ query.split()  (сырые токены, разделители — пробелы)
  │
  └─ search(terms, product, ...) ──→ обрабатываются внутри:
       normalize_terms(norm_terms):
         ├─ strip('"'), lower()
         ├─ пропуск стоп-слов (STOPWORDS) и токенов длиной < 2
         ├─ разбивка дефисных слов («прайс-листов» → «прайс» + «листов»)
         └─ морфологический корень через stem.stem_word()
             («выгрузку/выгрузки» → «выгрузк»)
       _correct_terms(norm_terms):
         └─ для стем с df=0 (не встречаются в индексе) и длиной ≥ 4 —
            замена на ближайший известный по Левенштейну (_closest_stem)
```

**Стоп-слова (STOPWORDS):** `в на для и по из от с со к у о а не то что как при но`
`чем чём же бы без до`.

### 3.2 Векторная нога — `_canonical_query()`

Запрос **для embedding** (`_canonical_query`, search.py:680). FTS-нога уже
стеммит и правит опечатки, а embed получал бы сырую строку — регистр и опечатки
искажали векторных соседей и гибридный порядок. Здесь:

- токены → lower;
- кириллические слова с df=0 и длиной ≥ 4 → замена на ближайший известный
  стем («автоимопрт» → «автоимпорт»);
- валидные и латинские токены (транслит) не трогаются.

Возвращает `(canonical_text, тип_коррекции)`.

### 3.3 Стемминг (`stem.py`)

Морфологический стемминг через `pymorphy3`. Стемы нормализуются через
`CharNormalize`: `ё`→`е`, убираются дефисы/подчёркивания/спецсимволы.

**Известная проблема**: `pymorphy3` иногда возвращает стемы с `ё`
(`подключённых` → `подключён`), а FTS5 tokenizer `unicode61` трактует
`ё`≡`е`. Это создаёт рассинхронизацию между FTS-токенами и стемами.

### 3.4 Исправление раскладки (`layout.py`) и fallback в гибриде

Коррекция раскладки ЙЦУКЕН→Cyrillic используется **как fallback** в
`search_hybrid` (см. раздел 5.6) и в eval-сервере `/api/hybrid`: только когда
основной результат слабый (пустой или низкое покрытие) и запрос похож на
кириллицу, набранную в латинской раскладке. `_layout_variant(query)` эмулирует
исправленную кириллицу; порог кириллических символов задаётся в `layout.py`.

Легитимный латинский транслит («nastrojka onlajn kassy») при этом не ломается —
исправленный вариант не превосходит основной по качеству.

---

## 4. Определение продукта (Product Detection)

Определение продукта построено из трёх независимых механизмов: **явные имена**,
**тематические маркеры** и **точечный паттерн web-payment**. Все они определены
в `search.py` (строки 24–161).

### 4.1 Каталог продуктов `PRODUCTS`

```python
PRODUCTS = {
    "parts-intellect-guide":      "Parts.Intellect",
    "parts-intellect-synch":      "Синхронизатор",
    "parts-resource-guide":       "Parts.Resource",
    "parts-resource-rest-api":    "Parts.Resource — REST API",
    "service-api":                "Tradesoft service API",
    "diadok":                     "Диадок",
    "delivery_schedule":          "График поставок",
    "wazzup":                     "Wazzup",
    "tsd":                        "ТСД",
    "marketplace":                "Маркетплейсы",
    "parts-resource-changes":     "Изменения Parts.Resource (версии)",
    "parts-intellect-changes":    "Изменения Parts.Intellect (версии)",
}
```

### 4.2 Явные имена — `PRODUCT_NAMES_SYSTEM` / `PRODUCT_NAMES_TOPIC`

`detect_product_name(query)` ищет **имя продукта в запросе** (по подстроке,
нормализованной `_name_key`: lower + точки/подчёркивания/дефисы → пробелы).

Блоки делятся на два класса приоритета:
- **`PRODUCT_NAMES_SYSTEM`** — явные имена систем. Проверяются первыми.
  Если в запросе названа система, она перекрывает любой тематический маркер.
  Примеры: `parts resource`, `parts.intellect`, `интеллект`, `service api`,
  `диадок`, `rest api ресурс`, `синхронизатор`.
- **`PRODUCT_NAMES_TOPIC`** — тематические/фичерные маркеры
  (`график поставок → delivery_schedule`, `тсд → tsd`,
  `wazzup/ozon/яндекс маркет/авито → marketplace`). Используются только если
  имя системы не найдено — так «настроить график поставок в Parts.Resource»
  уходит в `parts-resource-guide`, а «настроить график поставок» —
  в `delivery_schedule`.

Возвращается **один** продукт — первый найденный из более приоритетного блока.

### 4.3 Тематические маркеры — `PRODUCT_MARKERS` / `detect_products`

```python
PRODUCT_MARKERS = {
    "parts-intellect-synch":  ["синхронизац", "перенос", "передач", "синхрониз"],
    "parts-resource-rest-api":["api", "rest", "json", "метод", "endpoint",
                               "curl", "параметр запроса"],
    "parts-resource-guide":   ["интернет-магазин", "сайт", "корзина",
                               "прайс-лист", "прайс лист", "поставщик",
                               "клиентская часть", "каталог", "пополнение баланса"],
    "parts-intellect-guide":  ["наша фирма", "склад", "приходная", "расходная",
                               "торговая точка", "эквайринг", "интеллект", "касс"],
    "service-api":            ["api", "веб-поставщик", "веб поставщик", "service",
                               "tradesoft service", "endpoint", "getproviderlist",
                               "getpricelist", "поставщик по api", "подключ"],
}
```

`detect_products(query)` ранжирует продукты по числу найденных маркеров:
`score = число различных маркеров продукта, найденных в запросе`.

### 4.4 Объединение — `infer_product_context()`

Порядок приоритета:
1. **`forced_product`** (переданный явно) — возвращается как есть;
2. **явное имя** через `detect_product_name()` — наивысший приоритет;
3. **маркеры-контекст** через `detect_products()` — если уверенный лидер
   (счёт `top_score ≥ CONTEXT_PRODUCT_MIN_SCORE = 2` И нет близкого конкурента
   с таким же счётом — `len(det) == 1 or det[1][1] < top_score`);
4. иначе — `None` (поиск по всем продуктам).

Порог `CONTEXT_PRODUCT_MIN_SCORE = 2` — уже сильный сигнал (например
«поставщик» + «веб» → `parts-resource-guide`).

### 4.5 Точечный паттерн — `web_payment_product()`

Обрабатывает узкий класс запросов «оплата/эквайринг **на сайте**» для
интернет-магазина Parts.Resource. Причина: сам по себе «эквайринг» — индикатор
Parts.Intellect (POS), а «на сайте» переводит тему в способы оплаты
Parts.Resource.

```python
_WEBSITE_TERMS        = ("сайт", "интернет-магазин", "интернет магазин",
                         "онлайн-магазин", "веб-витрина", "интернет магазина")
_PAYMENT_TERMS        = ("эквайринг", "эквайринга", "оплат", "платеж", "платёж")
_POS_COUNTER_SIGNALS  = ("касс", "розничн", "торговой", "торговая точка",
                         "терминал", " pos", "офис")
```

Срабатывает **только** когда в запросе есть платёжный термин И веб-термин
И **нет** POS/офисных контрсигналов:
```python
if has_pay and has_web and not has_pos:
    return "parts-resource-guide"
```

Не затрагивает ни api/поставщик-запросы, ни «эквайринг на кассе».

### 4.6 Интерактивное уточнение — `ask_product()` (CLI)

Для консольного интерфейса: `--product` → сразу; `--auto` → `None` (все);
не tty → `None`. При 1 кандидате — подтверждение `[Y/n]`; при 2+ — меню;
при 0 — меню из всех продуктов; последняя опция — «Все продукты».

---

## 5. Поиск

### 5.1 FTS5-поиск (`search()`)

**Нормализация:** `_correct_terms(normalize_terms(terms))` (см. раздел 3.1).
Каждая стема → prefix-токен для FTS5: `fts_query(terms, mode)` строит
`"подключ"* AND "эквайр"*` (по одному `"..."*` на термин).

**Два яруса recall (search.py:427):**
1. **Tier AND** — строгий: все термины в чанке (`fts_query(terms, "AND")`);
2. **Tier OR** — если строгих результатов мало (`len(ranked) < RECALL_MIN`),
   дополняется частичным совпадением (`fts_query(terms, "OR")`, `require_all=False`).
   Повышает recall там, где ни одна страница не содержит все термины сразу
   (напр. «куда перечисляется выручка»).

**Сортировка кандидатов** — `_score_row()` возвращает кортеж `(-matched, -phrase_hits,
-title_hits, bm25_score, span)`. Чанки, не прошедшие условие (`matched < RECALL_MIN`
при частичном режиме, либо отсутствие всех терминов при строгом), отбрасываются.

**Фильтр по продукту:** `AND product=?` если `product` задан, иначе по всем
продуктам. FTS-результат выбирается через `ORDER BY rank LIMIT 500`.

**Сниппеты:** `snippet(chunks_fts, 1, '⟦', '⟧', '…', 24)` — служебные маркеры
`⟦⟧` вокруг совпадений; `clean_snippet()` затем превращает их в `**жирный**`
(или `<b>` при `html=True`) и чистит markdown-мусор. Для совпадений в середине
документа (обрыв контекста с `…`) используется `smart_snippet()` — строит
связный сниппет по абзацам с якорем на границу абзаца.

> Примечание: скоринг/бусты страниц и продуктов выполняются на уровне слияния
> (`fusion.py`), а не в самом FTS5-поиске — см. раздел 5.5.

### 5.2 Векторный поиск (`vector_main()` / `vector_search.py`)

1. Запрос → `_canonical_query()` → canonical-текст;
2. `embed.embed_text(canonical)` → embedding-вектор через Ollama;
   **модель:** `qwen3-embedding:4b`, **размерность:** `EMBEDDING_DIMS = 2560`
   (настраивается через env `EMBEDDING_MODEL`/`EMBEDDING_DIMS`);
3. Вектор → Typesense `vector_search(vec, k=...)` (cosine), с фильтром по
   продукту, если задан (`k = limit*10` в `vector_main`, `k=RRF_TOP=30` в гибриде);
4. Результаты приводятся к формату строки `search()`:
   `(product, page, title, path, snippet, score)`, где `score = _score` (cosine),
   `snippet` пустой (FTS-сниппет подставляется при слиянии).

При недоступности Ollama/typesense `vector_main()` возвращает пустой список.

### 5.3 Гибридный поиск (`search_hybrid()`)

**Архитектура (`search.py:856`):**

```
search_hybrid(query, product, limit, snippets, embed_host)
  │
  ├─ если product is None: product = detect_product_name(query)
  ├─ если product всё ещё None:
  │     p = web_payment_product(query)  →  product = p, web_payment = True
  │
  ├─ если web_payment:  → ВЕКТОРНЫЙ-ТОЛЬКО путь
  │     return vector_main(query, product, limit, embed_host)   # ч. 5.4
  │
  ├─ иначе: rows, elapsed = _hybrid_inner(query, product, ...)  # ч. 5.5
  │
  └─ Fallback по раскладке (ч. 5.6):
       если product is None и результат слабый (пустой или coverage < 0.34)
       и запрос похож на кириллицу в латинской раскладке —
       повторить _hybrid_inner по восстановленной кириллице и вернуть,
       если она заметно лучше (alt_cov ≥ prim_cov + 0.34).
```

### 5.4 Web-Payment Path (вектор-только)

Тема «оплата/эквайринг на сайте» семантически однозначна для эмбеддинга,
а FTS5 **без стемминга** для таких запросов даёт лишь шум — страницы-доноры
«сайт/подключ», не попавшие в векторный топ, получают RRF-буст и глушат верные
ответы оплаты. Поэтому здесь полагаемся на **чистый векторный поиск по продукту**
`parts-resource-guide`:

```python
rows, elapsed = vector_main(query, product, limit, embed_host)
```

Он возвращает именно «способы оплаты» Parts.Resource
(`nastrojka_sposobov_oplaty`, `priem_onlajn_platezhej`, ...).

Аналогичный путь реализован и в eval-сервере `run_compare()` (eval_server.py:419):
- `web_payment_product(query)` → `product = "parts-resource-guide"`, `web_payment=True`;
- векторный топ (`vector_search(k=30, product=product)`) берётся как `hyb_rows`
  напрямую, без FTS-слияния.

### 5.5 Слияние FTS + vector (`_hybrid_inner`) / `fusion.fuse()`

1. **FTS5 результаты** — `search(terms(query), product, limit=RRF_TOP)`.
2. **Векторные результаты** — по **canonical-запросу** (нижний регистр +
   исправление опечаток), т.к. иначе embedding видит регистр/опечатки и искажает
   соседей: `embed_text(canonical)` → `vector_search(vec, k=RRF_TOP, product)`.
   (При недоступности Ollama/typesense — откат к чистому FTS5-поиску.)
3. **Слияние** через `fusion.fuse(query, fts_rows, vec_rows, product)`
   (общий модуль, тот же и в eval-сервере): RRF + терм-буст + API-intent буст +
   развязка ничьих по векторной уверенности (`vec_score`).
4. **Топ-N** с дедупликацией кросс-продуктовых страниц при `product=None`
   (одна и та же `page` в разных продуктах схлопывается, продукт — через запятую).
5. **Map back** к полной строке результата `(product, page, title, path, snippet, score)`.

**RRF-слияние**:
```
RRF_score(doc) = Σ  1/(k + rank_i(doc))   +   линейная поправка модели
                 i∈sources
```
`RRF_K = 60` (константа, `search.py:20`). Детали признаков и весов модели —
в разделе 6.

**Признаки документа (FEAT_NAMES, в `fusion.py:28`):**
| Индекс | Признак | Описание |
|--------|---------|----------|
| 0 | `fts_rrf` | `1/(RRF_K+fts_rank+1)`, 0 если результат только из вектора |
| 1 | `vec_rrf` | `1/(RRF_K+vec_rank+1)`, 0 если результат только из FTS |
| 2 | `vec_score` | Косинусная уверенность вектора (0, если вектора нет) |
| 3 | `product_match` | 1 если продукт результата = детектированному продукту |
| 4 | `is_api` | 1 если продукт в `API_PRODUCTS` (`service-api`, `parts-resource-rest-api`) |
| 5 | `is_changelog` | 1 если продукт в `CHANGELOG_PRODUCTS` (`parts-resource-changes`, `parts-intellect-changes`) |

Порядок признаков обязан совпадать с `rank_train.py` и `rank_model.py`.

**Дополнительные коэффициенты RRF (`fusion.py`):**
- `RBUF_TOP_N = 30` — сколько векторных хитов участвует в слиянии;
- `RRF_K` — константа RRF (из `search.py`);
- `API_RRF_BONUS = 0.02` — буст для API-продуктов при API-intent;
- `CHANGELOG_RRF_PENALTY = 0.035` — понижение для changelog-продуктов
  (исторические записи по версиям шумят в общем поиске, но остаются находимыми
  при явном имени продукта); `boost = 12` при дискриминативных терминах —
  сдвиг рангов векторных хитов, содержащих эти термины, вверх (`i - boost`),
  остальных — вниз (`i + boost`).

**Развязка ничьих** — сортировка `(-rrf, -vec_score, order)`: при равенстве
RRF победитель определяется семантической уверенностью (vector _score), а не
порядком выдачи FTS. Страница, найденная только FTS и отсутствующая в векторном
top-N, получает `vec_score=0` и проигрывает ничью реально релевантному
векторному хиту (чинит топ-1 вида «подключить нового поставщика», где FTS-AND
ловил нерелевантную «выгрузку»).

**Отсечка по дискриминативным терминам** — если у запроса есть редкие термины
(`rare = _discriminators(...)`), показываются только страницы, содержащие хотя
бы один из них (иначе — запасной вариант: полный список).

### 5.6 Fallback по раскладке

Применяется в `search_hybrid` только когда `product is None` И основной результат
пустой или низкого качества (`_result_coverage < 0.34`). Ко всему эмулируется
`_layout_variant(query)` (кириллица из латинской раскладки) через `layout.py`;
возвращается альтернативный результат, если он заметно лучше
(`alt_cov ≥ prim_cov + 0.34`). Легитимный латинский транслит
(например «nastrojka onlajn kassy») при этом не ломается — кириллический вариант
не превосходит основной по качеству.

---

## 6. Самообучающееся ранжирование

Аддитивная поправка к RRF: набор линейных весов над 6 признаками результата
(`FEAT_NAMES`, см. раздел 5.5). Веса обучаются off-line по **неявным сигналам**
(клики/открытия из логов) и сохраняются в `cache/rank_weights.json`.

### 6.1 Обучение (`rank_train.py`)

**Входные данные** (из `cache/kb_access.db`, режим только-чтение):
- `search_events` с полем `top10_pp` (JSON-топ выдачи) — по каждому запросу;
- `events` типа `click`/`open` с полем `pp` (`product__page`) — что открыли.

**Построение пар (`build_dataset`):** для каждого поискового события:
1. Пересчёт результата через `fusion.fuse()` → `query_rankings` (порядок) +
   `features` (признаки 6-dim каждого результата);
2. Лейблы релевантности: открытые/кликнутые `product__page` → `label > 0`;
3. **Попарная маркировка:** для каждого релевантного результата `rk`, стоящего
   в выдаче ниже нерелевантного `nk` (выше по списку), создаётся пара
   `(features[rk], features[nk])` — «релевантный должен идти выше».

**Обучение (`train_pairs`):** **SGD на логистической парной потере**
(не sklearn, `pairs`, `epochs=12`, `lr=0.05`, `seed`):
```
для каждой пары (fp, fn):
    diff   = w·fp - w·fn
    sig    = 1/(1+exp(-diff))        # сигмоида
    w     += lr * sig * (fp - fn)    # градиент логистической потери
```
Веса стартуют с нуля. Если пар недостаточно — модель не пишется.

**Валидация (holdout):** запросы делятся на train/hold (1/5 hold), по запросам,
не на train. Метрика — **NDCG@5**:
- `ndcg_before` — NDCG исходного порядка выдач на hold;
- `ndcg_after` — NDCG после перестановки обученными весами.

**Запись:** веса пишутся только если `ndcg_after ≥ ndcg_before` (или `--force`).
Записывается статус `active: true/false`, `version`, `n_queries`, `n_pairs`,
`ndcg_before`, `ndcg_after`.

### 6.2 Применение (`rank_model.py`)

В `fusion.py` после подсчёта RRF-скоров каждого результата:
```python
features[k] = [fts_rrf, vec_rrf, vec_score, product_match, is_api, is_changelog]
rrf[k] += rank_model.weight_adjustment(features[k])   #  Σ wi·fi
```

**Гейт безопасности (`rank_model.py`):** веса применяются только если:
- файл `cache/rank_weights.json` существует и корректно парсится;
- `active = true` и есть `weights`;
- `len(weights) == FEAT_DIM (6)`;
- накоплено достаточно данных: `n_queries ≥ DEFAULT_MIN_QUERIES (50)`
  И `n_pairs ≥ DEFAULT_MIN_PAIRS (200)` (проверяется в rank_train при записи).

Если любое условие не выполняется → `weight_adjustment()` возвращает `0.0` →
ранжирование **ровно такое же, как до введения модели** (нулевая поправка).

`dump_status()` возвращает метастатус модели для диагностики/эндпоинтов
(`present`, `active`, `version`, `n_queries`, `n_pairs`, `ndcg_before/after`,
`weights`, пороги).

---

## 7. eval-сервер (eval_server.py)

### 7.1 Серверная архитектура

- `ThreadingHTTPServer` на порту 8055 (по умолчанию)
- Управляется через launchd (`com.tradesoft.kb-eval-server.plist`)
- Отдаёт страницу hybrid-поиска на корне; публичный доступ идёт через nginx reverse-proxy (HTTPS) — см. «Доступ по алиасу» ниже

### 7.2 API-эндпоинты

| Метод | Путь | Назначение |
|-------|------|------------|
| `GET` | `/`, `/index.html`, `/hybrid`, `/hybrid.html` | Страница hybrid-поиска |
| `GET` | `/api/hybrid?q=...&top=10` | Гибридный поиск для UI (через `run_compare()`) |
| `GET` | `/api/hybrid?q=...&top=10` | Гибридный поиск для UI (через `run_compare()`) |
| `GET` | `/api/v1/search?q=...&mode=hybrid&top=10&product=...` | Универсальный поиск API v1 |
| `GET` | `/api/v1/products` | Список продуктов |
| `GET` | `/api/v1/document?product=...&page=...&chars=30000` | Просмотр документа |
| `GET` | `/api/v1/health` | Статус сервисов (Typesense, Ollama) |
| `GET` | `/api/compare?q=...&top=5&product=...` | Сравнение FTS vs. vector vs. hybrid |
| `GET` | `/api/page?product=...&page=...&chars=30000` | Просмотр документа (legacy) |
| `GET` | `/api/image?product=...&rel=...` | Изображение из документации |
| `GET` | `/api/access?...` | Логирование кликов и открытий |

### 7.3 Поток поиска в `/api/hybrid`

```
Запрос → detect_product_name()
       → run_compare(query, top, product)
           │
           ├─── FTS5 search()
           ├─── Typesense vector search()
           └─── _hybrid_from_hits() → fusion.fuse()
       │
       ├─── web_payment_product() ──→ если True: чистый vector-only путь
       │
       └─── layout fallback: если основной результат слабый (cov < 0.34)
            и запрос похож на кириллицу в латинской раскладке → повторить
```

### 7.4 run_search_v1 (API v1)

Унифицированный поиск для интеграции в любые системы. Поддерживает 3 режима:
- `fts` — чистый FTS5
- `vector` — чистый векторный (через Typesense)
- `hybrid` — гибридный (через `search_hybrid()`)

---

## 8. Логирование доступа (access_log.py)

### 8.1 Схема БД (`cache/kb_access.db`; путь настраивается `ACCESS_DB_PATH`)

**access_log** — каждый HTTP-запрос:
- `ts` (TEXT, ISO-время) — временная метка
- `ip`, `session_id`, `user_agent`, `path`, `query_raw`
- `status` (int), `latency_ms` (REAL), `n_results` (int)

**search_events** — каждый поисковый запрос:
- `ts`, `session_id`, `ip`
- `q_raw` — исходный запрос
- `q_canonical` — нормализованный запрос
- `corrected_type` — тип коррекции: `none`, `typo`, `layout`, `product`
- `product_detected`, `product_filter`
- `n_results`, `top10_pp` — количество результатов и топ-10 (product__page)
- `n_fts`, `n_vec`, `fts_ms`, `vec_ms`, `embed_ms`, `total_ms` — тайминги
- `vec_source` — источник векторных результатов: `full`/`unavailable`/`fallback`
- `boost`, `rare`, `api_intent` — флаги

**events** — клики и открытия:
- `type` — `open` (открытие документа), `click` (клик по результату)
- `search_event_id` — FK на `search_events.id` (привязка к выдаче)
- `rank` — позиция в выдаче (0-based)
- `pp` — ключ `product__page`
- `product`, `page`, `q` — поля результата/запроса

**sessions** — агрегация по сессиям:
- `session_id`, `ip`, `user_agent`, `day`
- `first_ts`, `last_ts`, `n_searches`, `n_opens`

### 8.2 Асинхронное логирование

Единый **daemon-writer-поток** (`access-log-writer`) потребляет
`queue.Queue` с `timeout=1.0`:
- каждый вызов `log_access`/`log_search`/`log_event`/`touch_session` кладёт
  запись в очередь и немедленно возвращается (не блокирует HTTP-ответ);
- writer за один цикл достаёт **одну** запись и пишет/коммитит её
  (`INSERT`, затем `commit`);
- при пустой очереди поток ждёт (polling 1 c);
- при остановке (`_stopping`) дорабатывает оставшиеся записи.

Записи сериализуются через `threading.Lock`; SQLite в режиме **WAL**
(`PRAGMA journal_mode=WAL`). Кэш `_seid` (последний `search_event_id` по сессии)
обеспечивает привязку кликов/открытий к выдачам для самообучающегося
ранжирования. Старые записи удаляются при старте через `_prune_locked`
(с учётом `ACCESS_RETENTION_DAYS`).

---

## 9. Конфигурация (config.py)

12-factor конфигурация из переменных окружения — без захардкоженных адресов
и секретов в коде (`config.py`, 116 строк).

### 9.1 Службы (Typesense / Ollama)

Порядок: если конкретная `TYPESENSE_*`/`OLLAMA_*` переменная задана — берётся она;
иначе применяются дефолты окружения `TS_ENV`.

| Переменная | По умолчанию | Назначение |
|------------|--------------|------------|
| `TS_ENV` | `auto`→`local` | Окружение: `local`/`prod`/`auto`. Задаёт дефолты, где не указаны env |
| `TYPESENSE_HOST` | `http://localhost:8108` (local) / `http://sup5.tradesoft.corp:8108` (prod) | Адрес Typesense |
| `TYPESENSE_KEY` | `ts_local_dev_key` (local) / из env (prod) | API-ключ Typesense (секрет — из окружения) |
| `OLLAMA_HOST` | `http://localhost:11434` (local) / `http://sup5.tradesoft.corp:6791` (prod) | Адрес Ollama для индексации |
| `OLLAMA_EMBED_HOST` | = `OLLAMA_HOST` | Ollama для быстрой эмбеддинга запросов «на живую» — локальная, чтобы не грузить прод-Ollama |
| `EMBEDDING_MODEL` | `qwen3-embedding:4b` | Модель embedding |
| `EMBEDDING_DIMS` | `2560` | Размерность embedding-вектора |
| `COLLECTION_NAME` | `kb_chunks` | Имя коллекции Typesense |

`get_embed_host()` возвращает `OLLAMA_EMBED_HOST` или `OLLAMA_HOST`;
`LOCAL_EMBED_HOST` — совместимость (прежний экспорт для eval_server).

### 9.2 Логирование обращений (access_log)

| Переменная | По умолчанию | Назначение |
|------------|--------------|------------|
| `ACCESS_ENABLED` | `1` | Вкл/выкл логирование (`0`/`false`/`no` — выкл) |
| `ACCESS_DB_PATH` | `cache/kb_access.db` | Путь к SQLite-журналу; `none`/`off`/`0` — выключено |
| `ACCESS_TOKEN` | (пусто) | Опциональный токен для `/api/access` с не-loopback адресов; пусто = только loopback/внутр. IP |
| `ACCESS_RETENTION_DAYS` | `90` | Срок хранения записей (0 = без автоочистки) |
| `ACCESS_SESSION_DAYS` | `30` | Max-Age cookie-сессии |

### 9.3 Параметры сервера (не в config.py)

Порт сервера — аргумент CLI `eval_server.py --port` (по умолчанию **8055**),
`--host` по умолчанию `0.0.0.0`.

---

## 10. Структура файлов

```
tradesoft-kb/
├── manifest.json                # Манифест продуктов (источники, версии, hashes)
├── requirements.txt
├── cache/
│   ├── kb_index.db               # FTS5-индекс (SQLite)
│   ├── kb_access.db              # Логи доступа (SQLite)
│   ├── rank_weights.json         # Веса ранжирования (обучаемые)
│   ├── ollama_models.json        # Кэш моделей Ollama
│   └── <product>/parsed/*.htm.md # Страницы в Markdown
├── products/
│   └── <product>/{index.md, content.md, images/}  # Сборки fetch.sh + картинки
├── logs/
│   ├── eval.log                  # Лог eval-сервера
│   ├── eval_server.log           # Лог сервера (stdout)
│   └── rank_train.log            # Лог обучения ранжирования
├── scripts/
│   ├── search.py                 # Ядро поиска (1069 строк)
│   ├── eval_server.py            # eval-сервер (1103 строки)
│   ├── fusion.py                 # RRF-слияние + feature extraction
│   ├── rank_model.py             # Загрузка весов модели
│   ├── rank_train.py             # Обучение весов по неявным сигналам
│   ├── stem.py                   # Стемминг (pymorphy3)
│   ├── layout.py                 # Исправление раскладки
│   ├── embed.py                  # Генерация embeddings (Ollama)
│   ├── typesense_client.py       # Клиент Typesense
│   ├── vector_search.py          # Векторный поиск
│   ├── build_index.py            # Индексация FTS5
│   ├── build_vector_index.py     # Индексация Typesense
│   ├── reembed_product.py        # Переиндексация embeddings продукта
│   ├── access_log.py             # Логирование доступа
│   ├── config.py                 # 12-factor конфигурация
│   ├── evaluate.py               # Запуск eval-queries
│   ├── update_eval.py            # Обновление eval-набора
│   ├── eval_queries.json         # 40 query ground-truth
│   ├── convert_svc_spec.py       # OpenAPI → KB страницы
│   ├── fetch.sh                  # Загрузка документации
│   ├── eval_page.html            # Страница сравнения (не раздаётся; заменена hybrid на корне)
│   ├── hybrid_page.html          # Страница hybrid-поиска (раздаётся на корне /)
│   └── launchd/
│       ├── com.tradesoft.kb-eval-server.plist   # Сервер (port 8055)
│       └── com.tradesoft.vector-index.plist     # Переиндексация (RunAtLoad)
├── README.md
├── TECHNICAL.md                  # Этот документ
└── .gitignore
```

---

## 11. Запуск и управление

### 11.1 Зависимости

Единственная обязательная Python-зависимость — русская морфология:

```bash
pip install -r requirements.txt   # pymorphy3>=2.0
```

Без `pymorphy3` поиск работает **без морфологии** (fallback) и не падает.
Typesense и Ollama потребляются через HTTP-клиенты (встроенный `urllib`) —
внешних API-пакетов в `requirements.txt` нет. Прочие системные тулзы
(для fetch/parse) — стандартные скрипты (`wget`/`curl`, `pandoc`/кастомный
парсер по мере надобности).

### 11.2 Сервисы

**Typesense** (Docker):
```bash
docker run -d --name typesense \
  -p 8108:8108 \
  -v $(pwd)/cache/ts-data:/data \
  typesense/typesense:27.1 \
  --data-dir=/data --api-key=<TYPESENSE_API_KEY> --enable-cors
```

**Ollama** (Docker):
```bash
docker run -d --name ollama \
  -p 11434:11434 \
  -v $(pwd)/cache/ollama:/root/.ollama \
  ollama/ollama:latest
# Затем: ollama pull qwen3-embedding:4b
```

**launchd** (macOS): plists находятся в `scripts/launchd/` и копируются в
`~/Library/LaunchAgents/`:
```bash
launchctl load ~/Library/LaunchAgents/com.tradesoft.kb-eval-server.plist
launchctl load ~/Library/LaunchAgents/com.tradesoft.vector-index.plist
```

Оба plist — `RunAtLoad` (запускаются при загрузке), `WorkingDirectory` =
`scripts/`, логи в `/tmp/eval_server.log` и `/tmp/vector_index.log`. Сервер
запускает `eval_server.py --port 8055` интерпретатором
`/opt/homebrew/opt/python@3.14/bin/python3.14`; vector-index запускает
`build_vector_index.py --rebuild` через `/opt/homebrew/bin/python3` (никакого
расписания нет — только при загрузке).

> Docker-команды Typesense/Ollama выше — справочные: фактический адрес/ключ
> берите из `TYPESENSE_HOST`/`OLLAMA_HOST`/env (см. раздел 9).

### 11.3 Ручной запуск

```bash
# FTS5-индекс
python scripts/build_index.py --rebuild

# Typesense-индекс
python scripts/build_vector_index.py

# Веса ранжирования
python scripts/rank_train.py

# eval-сервер
python scripts/eval_server.py --port 8055 --log-dir logs
```

### 11.4 Доступ по алиасу (nginx + HTTPS)

Публичный доступ к eval-серверу идёт через nginx reverse-proxy по алиасу
`kb.tradesoft.corp` (HTTPS). Сервер слушает `0.0.0.0:8055`, nginx пробрасывает
`443` → `127.0.0.1:8055` и отвечает HTTP→HTTPS редиректом.

Компоненты:
- **nginx** — установлен через Homebrew (`/opt/homebrew/etc/nginx`), автозапуск
  через `brew services start nginx` (`~/Library/LaunchAgents/homebrew.mxcl.nginx.plist`).
- **server block** — `/opt/homebrew/etc/nginx/servers/kb.tradesoft.corp.conf`
  (проброс на `127.0.0.1:8055`, заголовки `X-Real-IP`/`X-Forwarded-For`,
  редирект http→https).
- **Самоподписанный сертификат** — `/opt/homebrew/etc/nginx/ssl/kb.tradesoft.corp.{crt,key}`,
  SAN: `DNS:kb.tradesoft.corp`, `IP:10.182.174.97`, `IP:127.0.0.1` (действует 825 дней).
- **Запись в `/etc/hosts`** — `127.0.0.1 kb.tradesoft.corp` (для доступа с этой машины;
  коллеги из подсети добавляют `10.182.174.97 kb.tradesoft.corp` в свой hosts
  или используют IP `http://10.182.174.97:8055/`).

Точки входа:
- `https://kb.tradesoft.corp/` — гибридный поиск (основной доступ)
- `http://10.182.174.97:8055/` — прямой доступ без TLS (внутри подсети)

> Сертификат самоподписанный — браузер показывает предупреждение. Для его
> устранения сертификат нужно добавить в доверенные корневые ЦС (macOS Keychain)
> на машинах клиентов.

---

## 12. Известные ограничения

1. **FTS5 не выполняет стемминг русских слов** — tokenizer `unicode61` разбивает
   по пробелам/знакам, но не стеммует. Prefix-поиск (`подключ*`) не матчит
   `подключаемого`/`подключения`. Это прямая причина web-payment-паттерна
   (вектор-только поиск, см. раздел 5.4).

2. **ё/е рассинхронизация** — `pymorphy3` иногда возвращает стемы с `ё`
   (`подключён`), тогда как FTS5/`unicode61` трактует `ё`≡`е`. Стемы
   нормализуются через `CharNormalize`, но не во всех контекстах.

3. **Точечные правила vs. общая морфология** — паттерн `web_payment_product`
   и маркеры `PRODUCT_MARKERS`/`PRODUCT_NAMES_*` покрывают известные случаи
   вручную. Новые классы запросов требуют добавления правил в `search.py`,
   а не автоматического обобщения.

4. **Продуктов в манифесте больше, чем в карте детекта** — `parts-index-rest-api`
   и `seo-guide` индексируются из манифеста, но их нет в `PRODUCTS`/маркерах
   `search.py` (детект по ним не срабатывает без явного имени).

5. **Ollama/embedding недоступны → откат к FTS** — если Ollama или Typesense
   недоступны, гибрид корректно откатывается к чистому FTS5-поиску
   (`_hybrid_inner`/`vector_main` возвращают FTS-строки), но семантическое
   качество при этом теряется.

6. **Векторные значения источникозависимы** — embedding-коллекция Typesense
   существует только в памяти/volume контейнера; без persistent-volume данные
   не переживают рестарт, требуется переиндексация
   (`build_vector_index.py`).

---

## 13. Метрики качества (eval)

Набор: 40 запросов в `eval_queries.json`. Метрики:

| Метрика | Формула | Описание |
|---------|---------|----------|
| `prec@1` | `hit[0] in expected` | Доля запросов с правильным первым результатом |
| `prec@3` | `hit[0..2] ∩ expected ≥ 1` | Доля запросов с хотя бы одним правильным результатом в топ-3 |
| `prec@5` | `hit[0..4] ∩ expected ≥ 1` | Доля запросов с хотя бы одним правильным результатом в топ-5 |
| `MRR` | `1/rank(first hit)` | Средний обратный ранг первого правильного результата |

**Текущие результаты** (после web-payment fix):
- prec@1 = 0.600
- prec@3 = 0.800
- prec@5 = 0.850
- MRR = 0.696

---

## 14. Ключевые файлы — референс

| Файл | Строк | Назначение |
|------|-------|------------|
| `search.py` | 1069 | Ядро поиска, гибрид, product detection, web_payment_product |
| `eval_server.py` | 1096 | HTTP-сервер, API-эндпоинты, run_compare/run_search_v1 (гибрид на корне) |
| `fusion.py` | 157 | RRF, FEAT_NAMES, fuse(), rank_model интеграция |
| `rank_model.py` | 78 | Загрузка весов, weight_adjustment(), dump_status() |
| `rank_train.py` | 300 | Обучение по неявным сигналам, pairwise ranking |
| `access_log.py` | 474 | SQLite логирование, асинхронный буфер, аналитика |
| `build_index.py` | 167 | FTS5 индексация (incremental) |
| `config.py` | ~80 | 12-factor env config |
| `stem.py` | ~60 | pymorphy3 стемминг + CharNormalize |
| `layout.py` | ~80 | ЙЦУКЕН→Cyrillic layout correction |
| `embed.py` | ~50 | Ollama embedding generation |
| `typesense_client.py` | ~100 | Typesense client (search, index, health) |
| `vector_search.py` | ~80 | Vector search orchestration |
| `build_vector_index.py` | ~120 | Typesense index building |
| `eval_queries.json` | 40 | Ground-truth eval set |
| `manifest.json` | ~200 | Product fetch manifest |
