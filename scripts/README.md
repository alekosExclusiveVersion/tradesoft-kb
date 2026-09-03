# Скрипты базы знаний Tradesoft

Конвейер: `fetch.sh` (скачивание) → `parse.py` (HTML→Markdown) → `build_index.py` (FTS5-индекс) → `search.py` / `ask.py` (поиск).

## Быстрый старт

```bash
# 1. Обновить базу знаний: скачать изменённые страницы,
#    спарсить их в cache/<product>/parsed/*.htm.md и переиндексировать
./fetch.sh

# 2. Поиск
python3 search.py "НДС эквайринг"
python3 ask.py "передача НДС в эквайринг" | pbcopy
```

`fetch.sh` делает весь конвейер целиком (скачивание → `parse.py` → `build_index.py`).
По отдельности шаги можно запускать вручную, см. ниже.

## Продукты

| Ключ (`--product`) | Название |
|---|---|
| `parts-intellect-guide` | Parts.Intellect |
| `parts-intellect-synch` | Синхронизатор |
| `parts-resource-guide` | Parts.Resource |
| `parts-resource-rest-api` | Parts.Resource — REST API |

## Уточнение продукта

Если продукт не задан флагом, `search.py` и `ask.py` определяют его по маркерам
запроса (см. `PRODUCT_MARKERS` в `search.py`) и уточняют у пользователя — но
**только** при запуске из терминала:

- **1 кандидат** → подтверждение: «Запрос относится к продукту *X*? [Y/n]». При «n» — меню из всех продуктов.
- **2+ кандидата** → меню только из кандидатов, ранжированное по числу совпадений маркеров, плюс «Все продукты».
- **0 кандидатов** → меню из всех продуктов.
- Неверный ввод → повтор (до 3 попыток), затем «Все продукты».

Маркеры — лишь подсказка для уточнения: без подтверждения пользователя они
никогда не ограничивают поиск.

**Без вопросов** — в одном из случаев:

- задан `--product <ключ>`;
- задан `--auto` (поиск по всем продуктам);
- stdin не является терминалом (например, `search.py "..." | grep` или использование в скрипте).

## search.py

Поиск со сниппетами. Поддерживает три режима (`--mode`):

- `fts` (классический) — точный поиск по FTS5-индексу;
- `vector` — семантический поиск по Typesense (запрос → эмбеддинг через Ollama);
- `hybrid` (по умолчанию) — объединение FTS5 + vector через Reciprocal Rank Fusion (RRF).

```bash
python3 scripts/search.py "НДС эквайринг"
python3 scripts/search.py "налоговая система" --product parts-resource-guide
python3 scripts/search.py "nastrojka onlajn kassy" --top 3
python3 scripts/search.py "прайс-лист" --auto
python3 scripts/search.py "что-то" --no-snippet
python3 scripts/search.py "оплата картой" --mode vector
python3 scripts/search.py "приём возврата товара" --mode hybrid
```

Опции: `--product`, `--auto`, `--top N` (по умолчанию 5), `--no-snippet`,
`--mode fts|vector|hybrid` (по умолчанию hybrid).

В режиме `hybrid` при недоступности Typesense/Ollama автоматически
происходит откат на чистый FTS5.

Поиск точный, по ключевым словам контекста:
- стоп-слова («в», «на», «для», …) отбрасываются, дефисные слова разбиваются
  («прайс-листов» → «прайс» + «листов»);
- `AND` по всем терминам: каждый терм обязан встречаться в результате;
- термины должны находиться рядом (окно близости ~120 слов) — страницы, где
  слово упомянуто вскользь, не попадают в выдачу;
- в топ выходят страницы, где термины идут подряд, как в запросе
  («загрузка прайс-листов»), затем — где термины есть в заголовке;
- если результатов нет — `LIKE` по именам/заголовкам страниц (работает и с
  латинской транслитерацией, например `nastrojka`); иначе — «нет результатов».

Вывод: заголовки страниц берутся из `###`-заголовков Markdown; сниппеты
очищаются от картинок и markdown-разметки, найденные термины выделяются `**жирным**`.

## ask.py

Выводит полный текст найденных страниц — готовый контекст для LLM.
Поддерживает те же режимы `--mode`, что и search.py.

```bash
python3 scripts/ask.py "передача НДС в эквайринг"
python3 scripts/ask.py "ставка НДС онлайн касса" --product parts-resource-guide
python3 scripts/ask.py "интернет-магазин оплата картой" --top 3 --max-chars 12000
python3 scripts/ask.py "вопрос" --auto | pbcopy
python3 scripts/ask.py "как оформить возврат" --mode hybrid
```

Опции: `--product`, `--auto`, `--top N` (по умолчанию 4), `--max-chars N`
(максимум символов на страницу, по умолчанию 20000), `--no-sources`,
`--mode fts|vector|hybrid` (по умолчанию hybrid).

## Семантический поиск (Typesense + Ollama)

Для семантического/гибридного поиска нужны две службы и построенный
векторный индекс.

### 1. Локальный запуск служб

Typesense в Docker:

```bash
docker run -d --name typesense \
  -p 8108:8108 -p 8109:8109 \
  -v tradesoft-typesense-data:/data \
  typesense/typesense:27.1 \
  --api-key=ts_local_dev_key --data-dir=/data --enable-cors
```

Ollama (десктоп-приложение или `brew install ollama`), затем:

```bash
ollama pull qwen3-embedding:4b
```

`config.py` автоматически определяет окружение (см. `get_config()`):

- `TS_ENV=local` — локальные службы (`localhost:8108`, `localhost:11434`)
- `TS_ENV=prod` — продакшен (`sup5.tradesoft.corp:8108`, `:6791`)
- без `TS_ENV` (по умолчанию) — автоопределение: если `http://localhost:8108`
  отвечает, берутся локальные службы, иначе продакшен.

Например, поиск по прод-индексу: `TS_ENV=prod python3 scripts/search.py "..." --mode hybrid`.

### 2. Построение векторного индекса

```bash
python3 scripts/build_vector_index.py          # инкрементально
python3 scripts/build_vector_index.py --rebuild  # полная переиндексация
python3 scripts/build_vector_index.py --workers 6  # параллельных запросов к Ollama
python3 scripts/build_vector_index.py --dry-run   # показать план, ничего не менять
```

Для индексации прод-сервера (эмбеддинг через прод-Ollama, запись в
прод-Typesense): `TS_ENV=prod python3 scripts/build_vector_index.py --rebuild`.

Индексатор читает чанки из SQLite FTS-индекса (`build_index.py`),
векторизует их через Ollama (`qwen3-embedding:4b`, 2560 измерений) и
записывает в Typesense-коллекцию `kb_chunks`. Чанки до 8000 символов
индексируются целиком; более крупные усекаются до безопасного для
контекста модели размера.

### 3. Отдельный векторный поиск

```bash
python3 scripts/vector_search.py "приём возврата товара"
python3 scripts/vector_search.py "оплата картой" --top 10
python3 scripts/vector_search.py --product parts-resource-guide "интернет-магазин"
```

### 4. Визуальное сравнение выдачи

Веб-страница для сравнения FTS / Vector / Hybrid по одному вопросу: три колонки
с таймингами, в Hybrid помечается происхождение каждой страницы
(«семантика» — нашёл только vector, «FTS» — только FTS-индекс). Клик по
результату открывает полный текст страницы с подсветкой терминов запроса.

```bash
python3 scripts/eval_server.py            # → http://127.0.0.1:8055 (по умолчанию локальный индекс)
TS_ENV=prod python3 scripts/eval_server.py   # страница против прод-индекса
```

Сервер слушает `0.0.0.0` и доступен из локальной сети — адрес печатается
при старте. Если другие машины не открывают, разрешите входящие подключения
для Python в Системных настройках → Сеть → Межсетевой экран.

API (для скриптов): `GET /api/compare?q=...&top=5&product=...` — JSON со всеми
тремя режимами за один вызов; `GET /api/page?product=...&page=...` — полный
текст страницы.

## build_index.py

Строит/обновляет индекс `cache/kb_index.db`. Инкрементальный: переиндексируются
только страницы с изменённым mtime. Крупные страницы режутся на секции по
заголовкам `##`/`###` (чанк до 15000 символов).

## fetch.sh

Скачивает документацию (только изменённые страницы; `--force` — всё заново).

```bash
./fetch.sh
./fetch.sh --force
./fetch.sh --products parts-resource-guide,parts-intellect-guide
```

## parse.py

Конвертация HTML (Drexplain) в Markdown и извлечение оглавления.
Используется внутри `fetch.sh`; вручную обычно не запускается.

```bash
python3 parse.py toc <alljs>                 # дерево страниц в JSON
python3 parse.py html <htmlfile> <outfile>   # HTML -> Markdown
```

## telegram_bot.py

Telegram-бот для поиска по базе знаний (библиотека `python-telegram-bot`, установлена в `scripts/.venv`).

```bash
cd scripts
export TELEGRAM_BOT_TOKEN="токен от @BotFather"
export TELEGRAM_API_BASE="https://api.telegram.org/bot"   # при необходимости — зеркало/локальный Bot API
.venv/bin/python telegram_bot.py
```

Команды бота:

- `/start` — приветствие;
- `/help` — справка;
- `/search <запрос>` — поиск (результаты со сниппетами);
- любой текст без команды — то же, что `/search`.

Под результатами — кнопки:
- `1`–`5` — полный текст страницы результата (без картинок), далее
  навигация `◀ Пред · Список · След ▶` (текст длиннее ~12 000 символов обрезается);
- выбор продукта сужает поиск, «Все продукты» — возвращает поиск по всем.

## Как это устроено

- `cache/<product>/raw/` — скачанные HTML;
- `cache/<product>/parsed/*.htm.md` — Markdown-страницы (индексируются);
- `cache/kb_index.db` — SQLite: таблица `pages` (страницы по чанкам) + FTS5-таблица `chunks_fts`.
