#!/usr/bin/env python3
"""Лёгкая русская морфология через pymorphy3 для FTS-поиска.

Приводит словоформы к общему префиксному корню, чтобы «выгрузку/выгрузки/
выгрузка» матчились как один корень «выгрузк». Ленивая инициализация анализатора.

Если pymorphy3 не установлен — работает как пассивный fallback (возвращает
слово в нижнем регистре), чтобы не ломать поиск без зависимости.
"""
import re
import threading

_LOCK = threading.Lock()
_ANALYZER = None
_ANALYZER_FAILED = False

# Окончания, которые безопасно срезать с нормальной формы существительных/
# прилагательных, чтобы получить более широкий корень-префикс: выгрузка->выгрузк,
# магазин->магазин, прайс-лист->прайс-лист.
_STRIP = ("ового", "его", "ого", "ыми", "ими", "ым", "им",
          "ые", "ие", "ая", "яя", "ое", "ее", "ой", "ей", "ый", "ий",
          "ах", "ях", "ам", "ям", "ами", "ями", "у", "ю", "а", "я", "о",
          "е", "ы", "и")
_MIN_ROOT = 4


def _get_analyzer():
    global _ANALYZER, _ANALYZER_FAILED
    if _ANALYZER is not None or _ANALYZER_FAILED:
        return _ANALYZER
    with _LOCK:
        if _ANALYZER is not None or _ANALYZER_FAILED:
            return _ANALYZER
        try:
            import pymorphy3
            _ANALYZER = pymorphy3.MorphAnalyzer()
        except Exception:
            _ANALYZER_FAILED = True
    return _ANALYZER


def _strip_vowel(word: str) -> str:
    """Срезает окончание с нормальной формы для широкого префикс-матча."""
    # Дефисные (прайс-лист) срезаем по частям -> берём обе части как есть.
    if "-" in word:
        return word
    for end in _STRIP:
        if len(word) - len(end) >= _MIN_ROOT and word.endswith(end):
            return word[: len(word) - len(end)]
    return word


_RE_WORD = re.compile(r"[\wа-яё]+", re.I)


def stem_word(word: str) -> str:
    """Возвращает корень-префикс для слова (fallback — нижний регистр)."""
    w = word.strip().lower()
    if not w:
        return ""
    # Не-кириллица (цифры, 1с и пр.): только нижний регистр, без морфологии.
    if not re.search("[а-яё]", w):
        return w
    an = _get_analyzer()
    if an is None:
        return w
    try:
        norm = an.parse(w)[0].normal_form
    except Exception:
        norm = w
    if not norm or not re.search("[а-яё]", norm):
        return norm or w
    return _strip_vowel(norm)


def stem_text(text: str):
    """Стемит все слова в тексте (для токенов индекса)."""
    parts = _RE_WORD.findall(text or "")
    return [stem_word(p) for p in parts]
