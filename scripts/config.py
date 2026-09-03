#!/usr/bin/env python3
"""Unified configuration for Typesense + Ollama services.

Everything is driven by environment variables (12-factor), so the same code
runs locally, in Docker, or against prod — no hardcoded addresses/secrets.

  TYPESENSE_HOST      e.g. http://localhost:8108 | http://typesense:8108
  TYPESENSE_KEY       API-ключ Typesense (секрет — из env/.env, не из кода)
  OLLAMA_HOST         Ollama для эмбеддингов индексации/поиска
  OLLAMA_EMBED_HOST   Ollama для быстрой эмбеддинга запросов живои страницы
                      (локальный, чтобы не грузить прод-Ollama); по умолч. = OLLAMA_HOST
  TS_ENV               local|prod|auto — устарело, оставлено для совместимости:
                      задаёт дефолты там, где env-переменные не указаны.

Порядок: если конкретная TYPESENSE_*/OLLAMA_* переменная задана — берётся она;
иначе применяются дефолты окружения TS_ENV (local/prod/auto).
"""
import os
import urllib.request

EMBEDDING_MODEL = os.environ.get(
    "EMBEDDING_MODEL", "qwen3-embedding:4b")
EMBEDDING_DIMS = int(os.environ.get("EMBEDDING_DIMS", "2560"))
COLLECTION_NAME = os.environ.get("COLLECTION_NAME", "kb_chunks")

# Дефолты для совместимости с прежним поведением (TS_ENV).
_DEFAULTS = {
    "local": {
        "typesense_host": "http://localhost:8108",
        "typesense_key": "ts_local_dev_key",
        "ollama_host": "http://localhost:11434",
    },
    "prod": {
        "typesense_host": "http://sup5.tradesoft.corp:8108",
        # Ключ прода теперь ожидается из окружения TYPESENSE_KEY, а не из кода.
        "typesense_key": os.environ.get("TYPESENSE_KEY", ""),
        "ollama_host": "http://sup5.tradesoft.corp:6791",
    },
}


def _default_env() -> str:
    env = (os.environ.get("TS_ENV") or "auto").strip().lower()
    if env in _DEFAULTS:
        return env
    return "local"


def _probe_typesense(host: str, timeout: float = 2.0) -> bool:
    try:
        req = urllib.request.Request(host + "/health")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _base() -> dict:
    """Базовые настройки: явные env или дефолт окружения."""
    defaults = _DEFAULTS[_default_env()]
    return {
        "typesense_host": os.environ.get("TYPESENSE_HOST", defaults["typesense_host"]),
        "typesense_key": os.environ.get("TYPESENSE_KEY", defaults["typesense_key"]),
        "ollama_host": os.environ.get("OLLAMA_HOST", defaults["ollama_host"]),
    }


def get_config() -> dict:
    """Возвращает текущую конфигурацию (dict: typesense_host/key, ollama_host)."""
    return _base()


def get_config_summary() -> str:
    c = get_config()
    env = "LOCAL" if c["typesense_host"].find("localhost") >= 0 or \
          c["typesense_host"].find("127.0.0.1") >= 0 else "PROD"
    return f"{env}: TS={c['typesense_host']} OLLAMA={c['ollama_host']}"


def get_embed_host() -> str:
    """Ollama для быстрой эмбеддинга запросов живои страницы."""
    return os.environ.get("OLLAMA_EMBED_HOST", get_config()["ollama_host"])


# Совместимость: прежний експорт для eval_server.
LOCAL_EMBED_HOST = os.environ.get("LOCAL_EMBED_HOST", "")


# ---------------------------------------------------------------------------
# Логирование обращений и аналитики (access_log).
# `ACCESS_DB_PATH` — SQLite-файл журнала. Пусто/`none` — логирование выключено.
# `ACCESS_TOKEN` — опциональный токен для /api/access с не-loopback адресов.
#   По умолчанию пусто = /api/access доступен только с loopback/внутр. IP.
# `ACCESS_RETENTION_DAYS` — срок хранения записей (0 = без автоочистки).
# `ACCESS_SESSION_DAYS` — Max-Age cookie-сессии в днях.
# ---------------------------------------------------------------------------
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_KB_ROOT = os.path.dirname(_SCRIPT_DIR)

ACCESS_ENABLED = os.environ.get("ACCESS_ENABLED", "1") not in ("0", "false", "no")
ACCESS_DB_PATH = os.environ.get(
    "ACCESS_DB_PATH",
    os.path.join(_KB_ROOT, "cache", "kb_access.db"),
)
ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN", "")
ACCESS_RETENTION_DAYS = int(os.environ.get("ACCESS_RETENTION_DAYS", "90"))
ACCESS_SESSION_DAYS = int(os.environ.get("ACCESS_SESSION_DAYS", "30"))


def access_is_enabled() -> bool:
    """Логирование включено и путь к БД задан (не 'none'/пусто)."""
    if not ACCESS_ENABLED:
        return False
    p = (ACCESS_DB_PATH or "").strip().lower()
    return bool(p) and p not in ("none", "off", "0")

