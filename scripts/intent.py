#!/usr/bin/env python3
"""Intent router — классификация запроса по цели пользователя.

Определяет интент (configure/explain/troubleshoot/define/navigate/general)
и порядок блоков ответа (how_to vs how_it_works).

Примеры:
  python3 -m scripts.intent "как настроить выгрузку прайсов"
  python3 -m scripts.intent "как работает поиск по наименованию"
  python3 -m scripts.intent "проблема с авторизацией в Диадок"
"""
import json
import os
import re
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PRODUCTS_PATH = os.path.join(SCRIPT_DIR, "products.json")

_PRODUCTS = None


def _load_products():
    global _PRODUCTS
    if _PRODUCTS is None:
        with open(PRODUCTS_PATH, encoding="utf-8") as f:
            _PRODUCTS = json.load(f)["products"]
    return _PRODUCTS


# ---------------------------------------------------------------------------
# Intent definitions
# ---------------------------------------------------------------------------

class Intent:
    CONFIGURE = "configure"      # "как настроить X" → how_to → how_it_works
    EXPLAIN = "explain"          # "как работает X" → how_it_works → how_to
    TROUBLESHOOT = "troubleshoot" # "проблема с X" → how_to (fix) → how_it_works
    DEFINE = "define"            # "что такое X" → how_it_works → how_to
    NAVIGATE = "navigate"        # "где найти X" → how_to → how_it_works
    GENERAL = "general"          # по умолчанию → how_it_works → how_to

    ORDER = {
        CONFIGURE:  ("how_to", "how_it_works"),
        EXPLAIN:    ("how_it_works", "how_to"),
        TROUBLESHOOT: ("how_to", "how_it_works"),
        DEFINE:     ("how_it_works", "how_to"),
        NAVIGATE:   ("how_to", "how_it_works"),
        GENERAL:    ("how_it_works", "how_to"),
    }


# ---------------------------------------------------------------------------
# Stemming (lightweight, from stem.py)
# ---------------------------------------------------------------------------

_RE_WORD = re.compile(r"[\wа-яё]+", re.I)
_STRIP = ("ого", "его", "овых", "евыми", "ыми", "ими", "ым", "им",
          "ые", "ие", "ая", "яя", "ое", "ее", "ой", "ей", "ый", "ий",
          "ов", "ев", "ах", "ях", "ам", "ям", "ами", "ями", "у", "ю",
          "а", "я", "о", "е", "ы", "и")
_MIN_ROOT = 4
_VERB_SUFFIXES = ("ться", "тись", "чься", "ти", "ть", "чь")
_CONJ_SUFFIXES = ("аются", "ятся", "ются", "ается", "ится", "утся",
                  "ают", "ят", "ает", "ит", "ут", "ют", "овает", "евает")


def stem(word: str) -> str:
    """Упрощённый стеммер (без pymorphy3, для правил интента)."""
    w = word.strip().lower()
    if not w or not re.search("[а-яё]", w):
        return w
    # Срезаем глагольные окончания (инфинитив)
    for suff in _VERB_SUFFIXES:
        if w.endswith(suff):
            base = w[:-len(suff)]
            if len(base) >= _MIN_ROOT:
                for gl in "аяуюеио":
                    if base.endswith(gl) and len(base) - 1 >= _MIN_ROOT:
                        base = base[:-1]
                        break
                return base
            break
    # Срезаем спряжения (работает → работ)
    for suff in _CONJ_SUFFIXES:
        if w.endswith(suff) and len(w) - len(suff) >= _MIN_ROOT:
            return w[:-len(suff)]
    # Срезаем суффиксы существительных/прилагательных
    for end in _STRIP:
        if len(w) - len(end) >= _MIN_ROOT and w.endswith(end):
            return w[:-len(end)]
    return w


def _stem_set(text: str) -> set:
    return {stem(w) for w in _RE_WORD.findall(text.lower())}


# ---------------------------------------------------------------------------
# Intent patterns
# ---------------------------------------------------------------------------

# (stemmed keywords, intent, weight)
INTENT_PATTERNS = [
    # CONFIGURE — "как настроить", "как подключить", "как создать"
    (("настро",), Intent.CONFIGURE, 3),
    (("подключ",), Intent.CONFIGURE, 3),
    (("созд",), Intent.CONFIGURE, 2),
    (("добав",), Intent.CONFIGURE, 2),
    (("включ",), Intent.CONFIGURE, 2),
    (("активиров",), Intent.CONFIGURE, 2),
    (("установ",), Intent.CONFIGURE, 2),
    (("загруз",), Intent.CONFIGURE, 2),
    (("выгруз",), Intent.CONFIGURE, 2),
    (("выгрузк",), Intent.CONFIGURE, 2),
    (("печа",), Intent.CONFIGURE, 2),
    (("печать",), Intent.CONFIGURE, 2),

    # EXPLAIN — "как работает", "как устроен", "принцип"
    (("работ",), Intent.EXPLAIN, 3),
    (("устроен",), Intent.EXPLAIN, 3),
    (("принцип",), Intent.EXPLAIN, 3),
    (("механизм",), Intent.EXPLAIN, 3),
    (("происходит",), Intent.EXPLAIN, 2),
    (("выполняется",), Intent.EXPLAIN, 2),

    # TROUBLESHOOT — "проблема", "ошибка", "не работает"
    (("проблем",), Intent.TROUBLESHOOT, 4),
    (("ошибк",), Intent.TROUBLESHOOT, 4),
    (("не", "работ"), Intent.TROUBLESHOOT, 5),
    (("сбой",), Intent.TROUBLESHOOT, 4),
    (("лома",), Intent.TROUBLESHOOT, 4),
    (("не", "мож"), Intent.TROUBLESHOOT, 4),
    (("не", "уда"), Intent.TROUBLESHOOT, 4),
    (("почем",), Intent.TROUBLESHOOT, 3),

    # DEFINE — "что такое", "что это", "чем отлич"
    (("что", "тако"), Intent.DEFINE, 3),
    (("что", "это"), Intent.DEFINE, 3),
    (("чем", "отлич"), Intent.DEFINE, 3),
    (("определ",), Intent.DEFINE, 2),

    # NAVIGATE — "где найти", "как найти", "как зайти"
    (("где",), Intent.NAVIGATE, 2),
    (("найт",), Intent.NAVIGATE, 2),
    (("зайт",), Intent.NAVIGATE, 2),
    (("перейт",), Intent.NAVIGATE, 2),
]


def detect_intent(query: str) -> str:
    """Определяет интент запроса по ключевым словам."""
    q_stems = _stem_set(query)

    scores = {}
    for pattern_stems, intent, weight in INTENT_PATTERNS:
        if all(s in q_stems for s in pattern_stems):
            scores[intent] = scores.get(intent, 0) + weight

    if not scores:
        return Intent.GENERAL

    return max(scores, key=scores.get)


def detect_product(query: str) -> str:
    """Определяет продукт по ключевым словам запроса.

    Возвращает canonical ID из products.json или None.
    """
    products = _load_products()
    q_lower = query.lower()
    q_stems = _stem_set(query)

    best = None
    best_score = 0

    for pid, meta in products.items():
        score = 0
        for kw in meta.get("search_keywords", []):
            kw_stems = _stem_set(kw)
            # Все стемы ключевого слова присутствуют в запросе
            if kw_stems and all(s in q_stems for s in kw_stems):
                score += len(kw_stems)
            elif kw in q_lower:
                score += 1
        if score > best_score:
            best_score = score
            best = pid

    return best


def classify_block(source: str, content: str) -> str:
    """Определяет тип блока (how_to / how_it_works) по контенту.

    source: "docs" | "solution" | "crm"
    """
    content_lower = (content or "").lower()

    how_to_signals = [
        "шаг ", "шаги", "перейдите", "нажмите", "откройте",
        "в меню", "в разделе", "на вкладке", "в поле",
        "нужно ", "необходимо ", "для настройки", "для создания",
        "порядок действий", "инструкция",
    ]
    how_it_works_signals = [
        "работает", "функционирует", "выполняет", "обеспечивает",
        "предназначен для", "позволяет", "используется",
        "принцип работы", "как работает", "механизм",
        "автоматически", "при этом",
    ]

    to_score = sum(1 for s in how_to_signals if s in content_lower)
    works_score = sum(1 for s in how_it_works_signals if s in content_lower)

    # По умолчанию: docs → how_it_works, solutions → how_to
    if to_score > works_score:
        return "how_to"
    if works_score > to_score:
        return "how_it_works"
    return "how_it_works" if source == "docs" else "how_to"


def classify_results(results: list, intent: str) -> list:
    """Классифицирует и сортирует результаты по блокам для данного интента."""
    for r in results:
        r["block_type"] = classify_block(r.get("source", ""), r.get("content", ""))

    primary, secondary = Intent.ORDER.get(intent, Intent.ORDER[Intent.GENERAL])

    def sort_key(r):
        is_primary = 1 if r["block_type"] == primary else 0
        conf = r.get("confidence", 0)
        return (-is_primary, -conf)

    results.sort(key=sort_key)
    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Использование: python3 -m scripts.intent \"запрос\"")
        sys.exit(1)

    query = " ".join(sys.argv[1:])
    intent = detect_intent(query)
    product = detect_product(query)
    order = Intent.ORDER[intent]

    print(f"Запрос:  {query}")
    print(f"Интент:  {intent} → блоки: {order[0]} → {order[1]}")
    print(f"Продукт: {product or '(не определён)'}")
