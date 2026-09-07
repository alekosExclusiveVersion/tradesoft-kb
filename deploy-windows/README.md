# Развертывание KB Tradesoft на Windows (стабильная сеть)

Ставим базу знаний Tradesoft (гибридный поиск) на **Windows-машину**, которая
постоянно подключена к корпоративной сети, чтобы доступ не зависел от
переключения Wi-Fi/LAN на рабочем Mac.

Целевая машина в этой задаче: `192.168.128.160` (подсеть коллег).
После переноса коллеги ходят на `https://kb.tradesoft.corp` (или по IP).

---

## Архитектура

```
Windows (192.168.128.160)
├── nginx  (443/80)  ->  reverse-proxy  ->  127.0.0.1:8055 (eval_server)
├── eval_server.py   (Python 3.14, venv)
│     ├── FTS5-индекс  cache/kb_index.db
│     └── векторный    Typesense :8108  (Docker)
├── Typesense (Docker)  -- векторная индексация (embedding-векторы)
└── Ollama   (Docker)  -- генерация embeddings (qwen3-embedding:4b)
```

- Typesense + Ollama нужны **только** для семантического/гибридного поиска.
  Чистый FTS-поиск работает без них (hybrid автоматически откатится на FTS).
- Векторный индекс строится один раз (`build_vector_index.py --rebuild`).

---

## Шаг 1. Установка зависимостей (автоматически)

Единый установщик ставит **Python 3.14, Docker Desktop и nginx**:

```powershell
powershell -ExecutionPolicy Bypass -File install-deps.ps1
```

- Фаза 1: Python, WSL, скачивает/распаковывает nginx в `C:\nginx`, ставит Docker Desktop.
  В конце — предложит **перезагрузить** компьютер (требуется для Docker/WSL).
- Фаза 2 (после перезагрузки):
  ```powershell
  powershell -ExecutionPolicy Bypass -File install-deps.ps1 -Phase2
  ```
  запускает Docker Desktop и ждёт готовности движка.

> Если хочешь поставить компоненты вручную — см. следующий блок.

### Ручная установка (альтернатива)

1. [Python 3.14](https://www.python.org/downloads/) (Windows x64), при установке
   отметь **«Add python.exe to PATH»**.
2. [Docker Desktop for Windows](https://www.docker.com/products/docker-desktop/)
   (WSL2), включает перезагрузку.
3. [nginx for Windows](https://nginx.org/en/download.html) → распаковать в `C:\nginx`.

## Шаг 2. Перенос кода и данных

С macOS-машины скопируй на Windows (например в `D:\tradesoft-kb`):

```
tradesoft-kb/
├── scripts/            # код (НЕ переноси .venv, __pycache__)
│     ├── eval_server.py
│     ├── search.py, build_index.py, build_vector_index.py
│     ├── hybrid_page.html, demand_page.html
│     ├── config.py
│     └── ...           # остальные
├── cache/              # данные (137 MB)
│     ├── kb_index.db       # FTS5-индекс (готов)
│     └── <product>/parsed/ # markdown-страницы (для полных текстов)
├── requirements.txt
└── deploy-windows/     # этот пакет
```

> НЕ копируй: `scripts/.venv`, `scripts/__pycache__`, `cache/ts-data`, `cache/ollama`.
> Их пересоздаст Docker.

## Шаг 3. venv и зависимости

Из папки `deploy-windows` (или вручную):

```powershell
powershell -ExecutionPolicy Bypass -File setup.ps1
```

Скрипт:
- находит Python, создаёт `scripts/.venv`;
- ставит `pymorphy3` из `requirements.txt`;
- проверяет наличие `cache/kb_index.db`;
- проверяет наличие Docker.

## Шаг 4. Docker (Typesense + Ollama) — для гибридного поиска

1. Установи [Docker Desktop for Windows](https://www.docker.com/products/docker-desktop/)
   и запусти его (WSL2).
2. В `deploy-windows` создай `.env`:
   ```powershell
   Copy-Item .env.example .env
   # при желании задай TYPESENSE_KEY
   ```
3. Подними контейнеры:
   ```powershell
   docker compose up -d
   ```
4. Скачай модель эмбеддинга в Ollama:
   ```powershell
   docker exec kb-ollama ollama pull qwen3-embedding:4b
   ```

## Шаг 5. Сборка индексов

FTS5-индекс уже перенесён (`cache/kb_index.db`). Если его не было — пересобери:

```powershell
cd D:\tradesoft-kb\scripts
.venv\Scripts\python.exe build_index.py
```

Векторный индекс (после запуска Typesense+Ollama):

```powershell
.venv\Scripts\python.exe build_vector_index.py --rebuild
# параллельно, при желании: --workers 6
```

## Шаг 6. nginx (HTTPS reverse-proxy)

1. Скачай [nginx for Windows](https://nginx.org/en/download.html), распакуй в `C:\nginx`.
2. Положи `deploy-windows\nginx\nginx.conf` → `C:\nginx\conf\nginx.conf` (замени дефолтный).
3. Скопируй сертификаты (они уже есть на Mac, см. ниже):
   - `deploy-windows\nginx\certs\kb.tradesoft.corp.crt`
   - `deploy-windows\nginx\certs\kb.tradesoft.corp.key`
   в `C:\nginx\conf\certs\`.
4. Запуск:
   ```powershell
   cd C:\nginx
   .\nginx.exe
   # перезагрузка: .\nginx.exe -s reload
   # остановка:   .\nginx.exe -s stop
   ```

**Сертификаты.** На Mac они лежат в `/opt/homebrew/etc/nginx/ssl/`
(`kb.tradesoft.corp.crt` и `.key`). Скопируй их в пакет `deploy-windows/nginx/certs/`
перед переносом. При желании перегенерируй на Windows с нужными IP в SAN
(см. ТЕХНИЧЕСКАЯ информация ниже), либо добавь сертификат в доверенные на машинах коллег.

## Шаг 7. Запуск web-сервера

```powershell
cd D:\tradesoft-kb\scripts
.venv\Scripts\python.exe eval_server.py
# слушает 0.0.0.0:8055, доступен локально и по сети.
# Проверка: http://127.0.0.1:8055  → страница гибридного поиска
```

Через nginx: `https://127.0.0.1` или по имени/IP.

## Шаг 8. Автозапуск (Task Scheduler)

Чтобы eval_server, Docker и nginx стартовали при загрузке Windows:

1. **Docker Desktop** — в настройках поставь «Start Docker Desktop when you sign in».
2. **nginx** и **eval_server** — создай задачи в Task Scheduler.
   Пример — зарегистрировать через скрипт (доработай пути):
   ```powershell
   schtasks /Create /TN "kb-eval-server" /TR "D:\tradesoft-kb\scripts\.venv\Scripts\python.exe D:\tradesoft-kb\scripts\eval_server.py" /SC ONSTART /RU SYSTEM /RL HIGHEST /F
   schtasks /Create /TN "kb-nginx" /TR "C:\nginx\nginx.exe" /SC ONSTART /RU SYSTEM /RL HIGHEST /F
   ```
   > Обрати внимание: eval_server читает конфиг из env; задай нужные
   > переменные (TYPESENSE_KEY и т.п.) в свойствах задачи при необходимости.

---

## Доступ коллег

После переноса коллеги из `192.168.128.x` ходят на KB:

| Способ | Что настроить |
|---|---|
| По IP: `https://192.168.128.160` | сертификат должен включать этот IP в SAN (перегенерируй) |
| По имени: `https://kb.tradesoft.corp` | A-запись в корп-DNS (`kb` -> `192.168.128.160`) ИЛИ hosts на машинах коллег |

Раньше коллегам уже выкатывался `install_kb_corp.bat` (добавляет
`kb.tradesoft.corp` в hosts + импортирует сертификат в доверенные).
Обнови в нём IP на актуальный адрес Windows-машины и раздай снова.

## Доступ извне (филиалы)

Нужна одна из двух вещей (обычно через админа IT):
- проброс/порт-форвардинг внешнего порта на `192.168.128.160:443`, либо
- запись в корпоративном DNS + маршрутизация до этой машины.

Сам по себе L2TP-VPN на рабочем Mac **не даёт** коллегам доступа к KB
(VPN односторонний: Mac -> корп, но не корп -> Mac).

---

## Отладка

- `http://127.0.0.1:8108/health` — Typesense живой?
- `http://127.0.0.1:11434/` — Ollama живой?
- `http://127.0.0.1:8055/api/v1/health` — статус служб с точки зрения eval_server.
- Логи eval_server — в консоль (перенаправь в файл при запуске как службы).
