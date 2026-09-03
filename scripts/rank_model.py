"""Загрузка и применение обученных весов ранжирования.

Самообучающаяся модель: набор линейных весов над признаками результата
(см. FEAT_NAMES в fusion.py). Веса обучаются off-line скриптом rank_train.py по
неявным сигналам (клики/открытия из логов) и сохраняются в JSON. Здесь — только
загрузка весов и включение/выключение применения по гейту (активность + версия).

Ключевое свойство безопасности: если весов нет, файл неактивен или обучение не
накопило достаточно данных — применяются ВСЕ нули, то есть поведение ранжирования
ровно такое же, как до введения модели.
"""
import json
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
KB_ROOT = os.path.dirname(SCRIPT_DIR)
WEIGHTS_PATH = os.path.join(KB_ROOT, "cache", "rank_weights.json")

# Минимальное число пар и запросов для срабатывания весов (обучение/применение).
DEFAULT_MIN_QUERIES = 50
DEFAULT_MIN_PAIRS = 200

# Порог: признак, на который веса могли бы «переобучиться» в шум при малых данных.
_FEAT_DIM = 6


def _load() -> dict:
    if not os.path.exists(WEIGHTS_PATH):
        return {}
    try:
        with open(WEIGHTS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _active(data: dict) -> bool:
    return bool(data and data.get("active", False) and data.get("weights"))


def get_active_weights() -> list[float]:
    """Возвращает веса для применения или None, если модель выключена."""
    data = _load()
    if not _active(data):
        return None
    w = data.get("weights", [])
    if len(w) != _FEAT_DIM:
        return None
    return [float(x) for x in w]


def dump_status() -> dict:
    """Сводка статуса модели (для диагностики/эндпоинта)."""
    data = _load()
    return {
        "present": bool(data),
        "active": _active(data),
        "version": data.get("version", 0),
        "n_queries": data.get("n_queries", 0),
        "n_pairs": data.get("n_pairs", 0),
        "ndcg_before": data.get("ndcg_before"),
        "ndcg_after": data.get("ndcg_after"),
        "weights": data.get("weights") if _active(data) else None,
        "min_queries": DEFAULT_MIN_QUERIES,
        "min_pairs": DEFAULT_MIN_PAIRS,
    }


def weight_adjustment(features: list[float]) -> float:
    """Аддитивная поправка к RRF для одного результата по его признакам.

    features: вектор длины FEAT_DIM. Если модель выключена — 0.0 (без изменений
    относительно текущего поведения).
    """
    w = get_active_weights()
    if not w or not features:
        return 0.0
    return sum(wi * fi for wi, fi in zip(w, features))
