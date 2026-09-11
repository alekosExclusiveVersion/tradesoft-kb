#!/usr/bin/env python3
"""Поиск по FTS5-индексу БД.

Примеры:
  python3 scripts/search.py "НДС эквайринг"
  python3 scripts/search.py "налоговая система" --product parts-resource-guide
  python3 scripts/search.py "nastrojka onlajn kassy" --top 3
"""
import argparse
import os
import pickle
import re
import sqlite3
import sys
import threading
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
KB_ROOT = os.path.dirname(SCRIPT_DIR)
DB_PATH = os.path.join(KB_ROOT, "cache", "kb_index.db")

RRF_K = 40
RRF_TOP = 30
RECALL_MIN = 2  # если строгий AND-поиск даёт меньше — подключаем OR-fallback для recall

PRODUCTS = {
    "parts-intellect-guide": "Parts.Intellect",
    "parts-intellect-synch": "Синхронизатор",
    "parts-resource-guide": "Parts.Resource",
    "parts-resource-rest-api": "Parts.Resource — REST API",
    "service-api": "Tradesoft service API",
    "diadok": "Диадок",
    "delivery_schedule": "График поставок",
    "wazzup": "Wazzup",
    "tsd": "ТСД",
    "marketplace": "Маркетплейсы",
    "parts-resource-changes": "Изменения Parts.Resource (версии)",
    "parts-intellect-changes": "Изменения Parts.Intellect (версии)",
}

PRODUCT_MARKERS = {
    "parts-intellect-synch": ["синхронизац", "перенос", "передач", "синхрониз"],
    "parts-resource-rest-api": ["api", "rest", "json", "метод", "endpoint", "curl", "параметр запроса"],
    "parts-resource-guide": ["интернет-магазин", "сайт", "корзина", "прайс-лист", "прайс лист",
                             "поставщик", "клиентская часть", "каталог", "пополнение баланса"],
    "parts-intellect-guide": ["наша фирма", "склад", "приходная", "расходная", "торговая точка",
                              "эквайринг", "интеллект", "касс"],
    "service-api": ["api", "веб-поставщик", "веб поставщик", "service", "tradesoft service",
                    "endpoint", "getproviderlist", "getpricelist", "поставщик по api", "подключ"],
}


# Явные имена/клички продуктов в запросе. Более конкретные варианты идут раньше
# (проверяются первыми). Сравнение — по подстроке в нижнем регистре; точки,
# подчёркивания и дефисы нормализуются в пробел, чтобы «parts.resource» и
# «parts resource» матчились одинаково.
#
# Блоки делятся на два класса приоритета:
#   - PRODUCT_NAMES_SYSTEM — явные имена систем (Parts.Resource, Parts.Intellect,
#     service api, …). Если в запросе названа система, она перекрывает любой
#     тематический маркер продукта (detect_product_name ищет сначала здесь).
#   - PRODUCT_NAMES_TOPIC — тематические/фичерные маркеры продуктов (график
#     поставок, ТСД, маркетплейс, wazzup). Используются только если имя системы
#     не найдено, чтобы «настроить график поставок» оставался delivery_schedule,
#     а «…график поставок в Parts.Resource» уходил в parts-resource-guide.
PRODUCT_NAMES_SYSTEM = [
    (("изменения parts resource", "изменения в parts resource",
      "изменение в parts resource", "изменение parts resource",
      "что изменилось в parts resource",
      "parts resource changes", "resource changes",
      "что нового в parts resource", "версия 6", "ver.6", "версии 6", "версией 6"),
     "parts-resource-changes"),
    (("изменения parts intellect", "изменения в parts intellect",
      "изменение в parts intellect", "изменение parts intellect",
      "что изменилось в parts intellect",
      "parts intellect changes", "intellect changes",
      "что нового в parts intellect", "версия 5", "ver.5", "версии 5", "версией 5"),
     "parts-intellect-changes"),
    (("диадок", "сервис диадок", "скб контур", "контур",
      "экспорт в диадок", "импорт из диадок", "эдо"),
     "diadok"),
    (("parts resource rest api", "resource rest api", "rest api ресурс"),
     "parts-resource-rest-api"),
    (("parts intellect sinc", "parts intellect sync", "интеллект синхронизатор",
      "синхронизатор"),
     "parts-intellect-synch"),
    (("parts intellect", "parts.intellect", "parts intellect сервер",
      "интеллект"),
     "parts-intellect-guide"),
    (("parts resource", "parts.resource", "ресурс", "resource"),
     "parts-resource-guide"),
    (("service api", "tradesoft service", "service", "сервис апи"),
     "service-api"),
]

PRODUCT_NAMES_TOPIC = [
    (("график поставок", "графики поставок", "плановая дата поставки",
      "график поставки", "плановой даты поставки", "плановую дату поставки",
      "расчет плановой даты", "расчёт плановой даты"),
     "delivery_schedule"),
    (("wazzup",), "wazzup"),
    (("тсд", "терминал сбора данных"), "tsd"),
    (("маркетплейс", "маркетплейсы", "маркет плейс", "маркетплейсов",
      "ozon", "оzon", "яндекс маркет", "яндex", "авито", "дром", "onboxmarket"),
     "marketplace"),
]

# Полный список — для обратной совместимости (если где-то перебирается
# PRODUCT_NAMES целиком).
PRODUCT_NAMES = PRODUCT_NAMES_SYSTEM + PRODUCT_NAMES_TOPIC


def _name_key(query: str) -> str:
    """Нормализует запрос для сопоставления с именами продуктов."""
    import re as _re
    q = query.lower()
    q = _re.sub(r"[.\-_]", " ", q)
    return _re.sub(r"\s+", " ", q).strip()


def detect_product_name(query: str):
    """Явное имя продукта в запросе → ключ продукта, или None.

    В отличие от detect_products (маркеры-темы), здесь распознаются именно
    названия: «Parts.Resource», «Parts.Intellect», «Интеллект», «Ресурс»,
    «Rest API» и т.п. Возвращает ОДИН продукт (самый приоритетный).

    Сначала ищется явное имя системы (PRODUCT_NAMES_SYSTEM): если в запросе
    назван продукт, он перекрывает любой тематический маркер. Тематические
    маркеры (график поставок → delivery_schedule, ТСД, маркетплейс и т.п.,
    PRODUCT_NAMES_TOPIC) учитываются только если имя системы не найдено.
    Так «можно ли настроить график поставок в Parts.Resource?» направляется
    в parts-resource-guide, а «настроить график поставок для клиентов» —
    в delivery_schedule.
    """
    q = _name_key(query)
    for names in (PRODUCT_NAMES_SYSTEM, PRODUCT_NAMES_TOPIC):
        for product_names, product in names:
            for name in product_names:
                if name in q:
                    return product
    return None


# Точечный паттерн «оплата/эквайринг на сайте»: для интернет-магазина Parts.Resource.
# Срабатывает ТОЛЬКО когда в запросе намешаны оплата+сайт и нет POS/офисных маркеров
# (касса, торговый терминал и т.п.). Нужен, т.к. «эквайринг» сам по себе — индикатор
# Parts.Intellect (POS), а «на сайте» переводит тему в способы оплаты Parts.Resource.
_WEBSITE_TERMS = ("сайт", "интернет-магазин", "интернет магазин", "онлайн-магазин",
                  "веб-витрина", "интернет магазина")
_PAYMENT_TERMS = ("эквайринг", "эквайринга", "оплат", "платеж", "платёж")
_POS_COUNTER_SIGNALS = ("касс", "розничн", "торговой", "торговая точка", "терминал",
                        " pos", "офис")

# Платёжная экспансия: «эквайринг» — индикатор Parts.Intellect (POS), но без
# POS-сигналов (терминал/касса/розница/офис) запрос может касаться способов оплаты
# и онлайн-касс Parts.Resource, страницы которых слово «эквайринг» не содержат.
# Добавляем стемы платёжного контекста в FTS-реколл и скорринг.
_PAYMENT_EXPANSION = ("оплат", "платеж", "касс", "онлайн", "сбп")


def web_payment_product(query):
    """parts-resource-guide, если запрос — про оплату/эквайринг на сайте, иначе None.

    Узкий паттерн: (оплата И сайт) И никаких POS/офисных признаков. Не затрагивает
    ни api/поставщик-запросы, ни «эквайринг на кассе».
    """
    if not query:
        return None
    q = query.lower()
    has_pay = any(t in q for t in _PAYMENT_TERMS)
    has_web = any(t in q for t in _WEBSITE_TERMS)
    has_pos = any(t in q for t in _POS_COUNTER_SIGNALS)
    if has_pay and has_web and not has_pos:
        return "parts-resource-guide"
    return None


def detect_products(query):
    """Ранжированный список продуктов по совпадению маркеров в запросе.

    Возвращает [(product, score), ...], отсортированный по убыванию score.
    score = число различных маркеров продукта, найденных в запросе.
    """
    q = query.lower()
    scores = {}
    for product, markers in PRODUCT_MARKERS.items():
        n = sum(1 for m in markers if m in q)
        if n:
            scores[product] = n
    return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)


# Минимальное число маркеров, чтобы считать продукт «подтверждённым» по
# контексту запроса (без явного имени). 2 маркера — уже сильный сигнал,
# например «поставщик»+«веб» для parts-resource-guide.
CONTEXT_PRODUCT_MIN_SCORE = 2


def infer_product_context(query, forced_product=None):
    """Определяет продукт по явному имени ИЛИ по маркерам-контексту (fallback).

    - Явное имя из PRODUCT_NAMES («Parts.Resource», «Интеллект», «service api»)
      — наивысший приоритет.
    - Если имя не найдено, но маркеры (detect_products) явно указывают на один
      продукт (сильный перевес), возвращаем его — например «как подключить
      поставщика» → parts-resource-guide по маркерам «поставщик»/«веб».
    - В противном случае — None (поиск по всем продуктам).

    Возвращает ключ продукта или None.
    """
    if forced_product is not None:
        return forced_product if forced_product in PRODUCTS else None
    explicit = detect_product_name(query)
    if explicit:
        return explicit
    det = detect_products(query)
    if not det:
        return None
    top_product, top_score = det[0]
    # Более конкретные маркеры в остальных продуктах не должны перебивать.
    # Требуем уверенного лидера: максимальный счёт ≥ порога и отсутствие
    # близкого конкурента (разрыв > 0, чтобы не угадывать при ничьей).
    if top_score >= CONTEXT_PRODUCT_MIN_SCORE and \
            (len(det) == 1 or det[1][1] < top_score):
        return top_product
    return None


def ask_product(query, forced_product=None, auto=False):
    """Уточняет продукт у пользователя. Возвращает ключ продукта или None (все).

    - forced_product: задан --product → сразу возвращается;
    - auto: --auto → без вопросов, None (все);
    - stdin не терминал → без вопросов, None (все);
    - 1 кандидат → подтверждение [Y/n]; при 'n' — меню;
    - 2+ кандидата → меню из кандидатов;
    - 0 кандидатов → меню из всех продуктов.
    """
    if forced_product:
        return forced_product
    if auto or not sys.stdin.isatty():
        return None

    candidates = detect_products(query)
    if not candidates:
        return _menu(
            [(p, PRODUCTS[p]) for p in PRODUCTS],
            "Продукт не определён — выберите вручную",
        )
    if len(candidates) == 1:
        product = candidates[0][0]
        try:
            answer = input(f"Запрос «{query}» относится к продукту «{PRODUCTS[product]}»? [Y/n] ")
        except EOFError:
            return product
        if answer.strip().lower() in ("", "y", "yes", "д", "да"):
            return product
        return _menu(
            [(product, PRODUCTS[product]) for product in PRODUCTS],
            "Выберите продукт для поиска",
        )

    options = [(p, PRODUCTS[p]) for p, _score in candidates]
    return _menu(options, "Кандидаты по запросу (совпадений) — выберите продукт")


def _menu(options, title):
    """Меню выбора продукта. Возвращает ключ продукта или None («все»)."""
    for _ in range(3):
        print()
        print(title + ":")
        for i, (key, label) in enumerate(options, 1):
            print(f"  {i}) {label}")
        print(f"  {len(options) + 1}) Все продукты")
        try:
            answer = input(f"Введите номер (по умолчанию {len(options) + 1}): ")
        except EOFError:
            return None
        answer = answer.strip()
        if answer == "":
            return None
        try:
            n = int(answer)
        except ValueError:
            print(f"  [неверный ввод: {answer}]")
            continue
        if 1 <= n <= len(options):
            return options[n - 1][0]
        if n == len(options) + 1:
            return None
        print(f"  [неверный номер: {n}]")
    return None


def fts_query(terms, mode="AND"):
    parts = []
    for t in terms:
        t = t.strip().strip('"')
        if not t:
            continue
        parts.append(f'"{t}"*')
    joiner = " AND " if mode == "AND" else " OR "
    return joiner.join(parts)


STOPWORDS = frozenset(
    "в на для и по из от с со к у о а не то что как при но чем чём же бы без до".split()
)
PROXIMITY_WINDOW = 120
TOKEN_RE = re.compile(r"[\wа-яё]+", re.I)


def normalize_terms(terms):
    """Убирает стоп-слова, разбивает дефисные слова («прайс-листов» → «прайс», «листов»)
    и приводит к морфологическому корню («выгрузку/выгрузки» → «выгрузк»)."""
    from stem import stem_word
    out = []
    for t in terms:
        t = t.strip().strip('"').lower()
        if t in STOPWORDS or len(t) < 2:
            continue
        parts0 = t.replace("-", " ").split()
        for part in parts0:
            # Версионные токены («5.25», «6.74», «1.5.0») в стем-индексе разбиты
            # токенизатором FTS на отдельные числа, поэтому «5.25» заменяем на
            # составляющие «5», «25» — иначе строгий AND по «5.25» не находит
            # страницу (нет токена-префикса «5.25»).
            if re.fullmatch(r"\d+([.,]\d+)+", part):
                for sub in re.split(r"[.,]", part):
                    if sub not in STOPWORDS:
                        out.append(sub)
                continue
            if part in STOPWORDS or len(part) < 2:
                continue
            s = stem_word(part)
            if s and s not in out:
                out.append(s)
    return out


def word_positions(text):
    return [w.lower() for w in TOKEN_RE.findall(text or "")]


def phrase_hits(words, terms):
    """Сколько терминов запроса идут подряд (максимум по тексту)."""
    best = 0
    for i in range(len(words)):
        run = 0
        for j, t in enumerate(terms):
            if i + j < len(words) and words[i + j].startswith(t):
                run = j + 1
            else:
                break
        if run > best:
            best = run
    return best


_HL_OPEN, _HL_CLOSE = "\x02", "\x03"


def clean_snippet(raw, html=False):
    """Очищает сниппет FTS от markdown-мусора; найденные термины выделяются жирным.

    raw содержит служебные разделители FTS ⟦термин⟧. При html=True выделение
    превращается в <b>термин</b>, иначе в **термин**.
    """
    t = raw or ""
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", t)
    t = re.sub(r"!\[[^\]]*\]?\([^)]*\)?[\s…]*$", "", t)
    t = re.sub(r"^…\s*(?:⟦[^⟧]*⟧\s*|[^()\[\]\n]){0,80}\]\([^)]*\)", "", t)
    t = re.sub(r"⟦([^⟧]*)⟧", _HL_OPEN + r"\1" + _HL_CLOSE, t)
    lines = []
    for line in t.splitlines():
        line = re.sub(r"^#{1,6}\s*", "", line)
        line = line.replace("**", "").replace("__", "").replace("*", "")
        line = line.strip().lstrip("-\u2013\u2022").strip()
        line = line.replace("»", "").replace("►", "")
        if line:
            lines.append(line)
    t = " ".join(lines)
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > 300:
        t = t[:297].rstrip() + "…"
    if html:
        import html as htmlmod
        t = htmlmod.escape(t, quote=False)
        t = t.replace(_HL_OPEN, "<b>").replace(_HL_CLOSE, "</b>")
    else:
        t = t.replace(_HL_OPEN, "**").replace(_HL_CLOSE, "**")
    return t


def clean_content(text):
    """Убирает картинки и лишние пустые строки из текста страницы."""
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def load_chunks(product, page, max_chars=None):
    """Полный текст страницы по ключу (product, page), чанки по порядку."""
    if not os.path.exists(DB_PATH):
        sys.exit(f"Индекс не найден: {DB_PATH}. Запустите scripts/build_index.py")
    db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    rows = db.execute(
        "SELECT content FROM pages WHERE product=? AND page=? ORDER BY chunk",
        (product, page),
    ).fetchall()
    db.close()
    out = []
    size = 0
    for (content,) in rows:
        if max_chars is not None and size + len(content) > max_chars and out:
            break
        out.append(content)
        size += len(content)
    return "".join(out)


def _score_row(r, terms, require_all, min_hits=None):
    """Скорит один FTS-чанк. require_all=True — нужны ВСЕ термины; иначе — частичное
    совпадение (не меньше min_hits, по умолч. RECALL_MIN), чтобы поднять recall.
    Возвращает None, если чанк не подходит под условие. Работает по СТЕМОВОЙ паре
    строк (title_stem, content_stem из stems_fts), чтобы «новый» матчил
    «нового/новые», а «настро-*» покрывал «настройки/настроить»."""
    if min_hits is None:
        min_hits = RECALL_MIN
    _, page, title, _, _, score, content, title_stem = r
    all_words = word_positions(title_stem) + word_positions(content)
    title_words = word_positions(title_stem)
    first = {}
    matched = 0
    for t in terms:
        hits = [i for i, w in enumerate(all_words) if w.startswith(t)]
        if not hits:
            if require_all:
                return None
            continue
        first[t] = min(hits)
        matched += 1
    if not require_all and matched < min_hits:
        return None
    if first:
        span = max(first.values()) - min(first.values())
    else:
        span = 10 ** 6
    title_hits = sum(
        1 for t in terms if any(w.startswith(t) for w in title_words)
    )
    ph = phrase_hits(all_words, terms)
    # Структура ключа: matched > полный охват заголовка (title_hits) > смежность в
    # тексте (ph). Совпадение терминов в ЗАГОЛОВКЕ важнее случайной фразовой
    # близости в тексте: иначе «Подключение нового поставщика…» в тексте SMS-
    # сервиса (ph=2, title_hits=1) обгоняет «Мастер подключения веб-поставщика»
    # (ph=1, title_hits=2).
    return (-matched, -title_hits, -ph, score, span)


def search(terms, product=None, limit=5, snippets=True, _expand_names=False,
           _force_or=False):
    if not os.path.exists(DB_PATH):
        sys.exit(f"Индекс не найден: {DB_PATH}. Запустите scripts/build_index.py")
    _detect_index_change()
    db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)

    t0 = time.perf_counter()
    terms = _correct_terms(normalize_terms(terms))
    if not terms:
        db.close()
        return [], 0.0

    # Термины-имена продукта (напр. «синхронизатор» в parts-intellect-synch) не
    # различают релевантность: внутри продукта они встречаются почти везде и
    # задирают нерелевантные страницы в топ (страница про уведомления опережает
    # страницу «Выгрузка прайс-листов» только потому, что содержит слово
    # продукта). Исключаем их из подсчёта совпадений при скорринге.
    # При product=None (кросс-продуктовый поиск) ранжируем по ВСЕМ терминам:
    # частичное детектирование имени (напр. «resource» -> parts-resource-guide)
    # не должно сужать скоринг до одного токена — иначе относительный вес
    # смежных терминов теряется.
    if product is None:
        score_terms = terms
    else:
        score_terms = [t for t in terms if detect_product_name(t) != product]
    if not score_terms or _expand_names:
        score_terms = terms

    cond = " AND product=?" if product else ""
    args_extra = [product] if product else []

    def fetch(q):
        sql = (
            "SELECT s.product, s.page, p.title, p.path, '', "
            "bm25(stems_fts), s.content_stem, s.title_stem "
            "FROM stems_fts s LEFT JOIN pages p ON p.id = s.rowid "
            "WHERE stems_fts MATCH ?"
            + cond.replace("product", "s.product", 1)
            + " ORDER BY rank LIMIT 500"
        )
        return db.execute(sql, [q] + args_extra).fetchall()

    # Tier-2 (recall): сначала строгий AND (все термины), затем, если результатов
    # мало, дополняем частичными совпадениями через OR — повышает recall там, где
    # ни одна страница не содержит все термины сразу (напр. «куда перечисляется выручка»).
    ranked = []
    raw_all = fetch(fts_query(terms, "AND"))
    for r in raw_all:
        if not (r[2] or "").strip():
            continue
        rk = _score_row(r, score_terms, require_all=True)
        if rk is not None:
            ranked.append(rk + (r[:6],))

    if len(ranked) < RECALL_MIN:
        seen = {(x[5][0], x[5][1]) for x in ranked}
        # Relaxed AND по подмножествам значимых терминов. Нужно из-за морфологии:
        # стем из запроса (напр. «настроить») не является префиксом словоформ
        # документа («настройки», «настроек»), поэтому AND по всем терминам
        # пропускает релевантные страницы, которые ловит vector. Перебираем все
        # подмножества от большего к меньшему (исключая одиночные) и пробуем
        # строгий AND по ним. OR-fallback ниже тоже находит их, но с плохим рангом.
        from itertools import combinations
        for size in range(len(score_terms) - 1, 1, -1):
            for sub in combinations(score_terms, size):
                raw_sub = fetch(fts_query(sub, "AND"))
                for r in raw_sub:
                    if not (r[2] or "").strip():
                        continue
                    rk = _score_row(r, sub, require_all=True)
                    if rk is None or (r[0], r[1]) in seen:
                        continue
                    seen.add((r[0], r[1]))
                    ranked.append(rk + (r[:6],))
                if len(ranked) >= RECALL_MIN:
                    break
            if len(ranked) >= RECALL_MIN:
                break

    if len(ranked) < RECALL_MIN or _force_or:
        seen = {(x[5][0], x[5][1]) for x in ranked}
        raw_or = fetch(fts_query(terms, "OR"))
        for r in raw_or:
            if not (r[2] or "").strip():
                continue
            rk = _score_row(r, score_terms, require_all=False, min_hits=1)
            if rk is None or (r[0], r[1]) in seen:
                continue
            ranked.append(rk + (r[:6],))

    ranked.sort(key=lambda k: k[:5])
    rows = [k[5] for k in ranked[:limit]]
    if rows and snippets:
        # Сниппет из stems_fts-матча не формируется FTS (для него строим сами:
        # по абзацам страницы с подсветкой терминов маркерами ⟦⟧).
        rows = [
            (p, pg, ttl, path, smart_snippet(p, pg, score_terms), score)
            for (p, pg, ttl, path, tmp, score) in rows
        ]
    elapsed = (time.perf_counter() - t0) * 1000
    db.close()

    if not rows:
        rows = fallback_like(terms, product, limit)
    if not rows and not product and not _expand_names:
        rows, _ = search(terms, product=product, limit=limit, snippets=snippets, _expand_names=True)
    return rows, elapsed


def payment_resource_rows(query, limit=4):
    """Релевантные Parts.Resource страницы для платёжного запроса без POS-сигналов.

    «Эквайринг/способы оплаты/онлайн-касса»: страницы Parts.Intellect содержат
    слово «эквайринг», а страницы Parts.Resource («Настройка способов оплаты»,
    «Онлайн-кассы») — нет, поэтому дополняем кросс-продуктовый пул отдельным
    продукт-ограниченным поиском с платёжной экспансией терминов.
    """
    pterms = _correct_terms(normalize_terms(terms(query)))
    if not any(t.startswith("эквайринг") for t in pterms):
        return []
    if any(t.startswith(s.strip()) for t in pterms for s in _POS_COUNTER_SIGNALS):
        return []
    expanded = list(dict.fromkeys(pterms + list(_PAYMENT_EXPANSION)))
    rows, _ = search(expanded, product="parts-resource-guide", limit=limit,
                     snippets=False, _force_or=True)
    return rows


def fallback_like(terms, product, limit):
    """Поиск по именам страниц (латинская транслитерация) через LIKE."""
    if not os.path.exists(DB_PATH):
        return []
    db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    like = "%" + "%".join(t.lower() for t in terms) + "%"
    sql = (
        "SELECT product, page, title, path, NULL, 0.0 "
        "FROM pages WHERE lower(page) LIKE ? "
        "OR lower(title) LIKE ?"
    )
    args = [like, like]
    if product:
        sql += " AND product=?"
        args.append(product)
    sql += " LIMIT ?"
    args.append(limit)
    rows = [
        r for r in db.execute(sql, args).fetchall()
        if (r[2] or "").strip()
    ]
    db.close()
    return rows


RARE_DF_THRESHOLD = 100  # термин с числом чанков ≤ порога считаем кандидатом в дискриминатор
DISCRIMINATOR_COVERAGE = 0.5  # дискриминатор — термин, встречающийся в < 50% кандидатов
DFS_CACHE = {}

# Ultra-common stems — DF > 1500, не являются стоп-словами, но настолько
# частые, что не могут различать страницы (товар, клиент, заказ…). Исключаем
# из дискриминаторов и снижаем вес в векторном бусте.
ULTRA_COMMON_DF = 1500


def _build_ultra_common():
    """Вычисляет множество ultra-common стемов из лексикона (лениво)."""
    try:
        import pickle
        lex_path = os.path.join(os.path.dirname(__file__),
                                "..", "cache", "stem_lexicon.pkl")
        lex = pickle.load(open(lex_path, "rb"))
        lex.pop("_mtime", None)
        return frozenset(s for s, df in lex.items()
                         if df > ULTRA_COMMON_DF and s not in STOPWORDS)
    except Exception:
        return frozenset()


ULTRA_COMMON = _build_ultra_common()


def _term_doc_frequency(stem, db=None):
    """Сколько чанков содержит термин (префиксный матч). Кэшируется."""
    if stem in DFS_CACHE:
        return DFS_CACHE[stem]
    df = 0
    try:
        if db is None:
            db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
            close = True
        else:
            close = False
        row = db.execute(
            "SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH ?",
            (f'"{stem}"*',)).fetchone()
        df = row[0] if row else 0
        if close:
            db.close()
    except Exception:
        df = 0
    DFS_CACHE[stem] = df
    return df


def _discriminators(terms, candidate_keys):
    """Термины запроса, которые действительно «различают» релевантные страницы.

    Дискриминатор — редкий термин (мало чанков), при этом встречающийся лишь
    в меньшинстве страниц-кандидатов. Если же редкий термин есть почти у всех
    кандидатов (например «выгрузк» в запросе про выгрузку) — это тема запроса,
    а не отличительный признак, и его не берём.
    """
    if not candidate_keys:
        return []
    try:
        db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        out = []
        for t in terms:
            if t in ULTRA_COMMON:
                continue
            df = _term_doc_frequency(t, db)
            if df > RARE_DF_THRESHOLD or df == 0:
                continue
            hit = sum(1 for (p, pg) in candidate_keys
                      if _page_has_terms(p, pg, [t]))
            coverage = hit / len(candidate_keys)
            if coverage < DISCRIMINATOR_COVERAGE:
                out.append(t)
        db.close()
    except Exception:
        out = [t for t in terms
               if t not in ULTRA_COMMON and 0 < _term_doc_frequency(t) <= RARE_DF_THRESHOLD]
    return out


# ---------------------------------------------------------------------------
# Терпимость к опечаткам: лексикон стемов + замена df=0-термина на ближайший.
# ---------------------------------------------------------------------------
_LEXICON = None
_LEX_CACHE_PATH = os.path.join(KB_ROOT, "cache", "stem_lexicon.pkl")
_TYPO_MAX_DIST = 2


def _load_lexicon():
    """Частотный словарь стемов (stem -> число вхождений) по всем чанкам.

    Строится один раз из страниц индекса и кэшируется в pickle (быстрый старт
    сервера/повторных запусков). Кэш инвалидируется по mtime DB.
    """
    global _LEXICON
    if _LEXICON is not None:
        return _LEXICON
    try:
        import pickle
        db_mtime = os.path.getmtime(DB_PATH) if os.path.exists(DB_PATH) else 0
        if os.path.exists(_LEX_CACHE_PATH):
            with open(_LEX_CACHE_PATH, "rb") as f:
                saved = pickle.load(f)
            if isinstance(saved, dict) and saved.get("_mtime") == db_mtime:
                _LEXICON = saved
                return _LEXICON
    except Exception:
        pass

    from stem import stem_text
    lex = {}
    try:
        db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        for (content,) in db.execute("SELECT content FROM pages"):
            for s in stem_text(content or ""):
                if s:
                    lex[s] = lex.get(s, 0) + 1
        db.close()
    except Exception:
        lex = {}
    lex["_mtime"] = os.path.getmtime(DB_PATH) if os.path.exists(DB_PATH) else 0
    try:
        import pickle
        os.makedirs(os.path.dirname(_LEX_CACHE_PATH), exist_ok=True)
        with open(_LEX_CACHE_PATH, "wb") as f:
            pickle.dump(lex, f)
    except Exception:
        pass
    _LEXICON = lex
    return lex


def _lev(a, b):
    m, n = len(a), len(b)
    if n < m:
        a, b = b, a
        m, n = n, m
    prev = list(range(m + 1))
    for i, ch in enumerate(b):
        cur = [i + 1]
        for j in range(m):
            cur.append(min(cur[-1] + 1, prev[j + 1] + 1, prev[j] + (ch != a[j])))
        prev = cur
    return prev[m]


def _closest_stem(word):
    """Ближайший известный стем к `word` (edit-distance) или None."""
    lex = _load_lexicon()
    if not word or len(word) < 4:
        return None
    # Кандидаты: та же 1-я буква и длина в пределах ±_TYPO_MAX_DIST — дешёвый фильтр.
    candidates = []
    for s in lex:
        if s == "_mtime":
            continue
        if s[0] == word[0] and abs(len(s) - len(word)) <= _TYPO_MAX_DIST:
            candidates.append(s)
    if not candidates:
        return None
    best = min(candidates, key=lambda s: (_lev(word, s), -lex[s]))
    if _lev(word, best) <= _TYPO_MAX_DIST:
        return best
    return None


def _correct_terms(norm_terms):
    """Заменяет непонятные (df=0) стемы запроса на ближайшие известные."""
    out = []
    for t in norm_terms:
        if _term_doc_frequency(t) > 0 or len(t) < 4:
            out.append(t)
            continue
        fix = _closest_stem(t)
        out.append(fix if fix else t)
    return out


def _canonical_query(query):
    """Запрос для векторной ноги: нижний регистр + исправление опечаток слов.

    FTS-нога уже нормализует регистр (стеминг) и правит опечатки (лексикон),
    а вот embed получал сырую строку — из-за чего регистр и опечатки искажали
    векторные соседи и, как следствие, гибридный порядок. Здесь приводим текст
    к нижнему регистру и заменяем слова с df=0 на ближайшие известные
    («автоимопрт» -> «автоимпорт»), оставляя валидные и латинские токены
    (транслит) нетронутыми. Возвращает (canonical_text, использована_ли_коррекция).
    """
    if not query or not query.strip():
        return query, False
    out, changed = [], False
    for tok in query.split():
        low = tok.lower()
        has_cyr = any('\u0400' <= c <= '\u04FF' for c in low)
        if not has_cyr or _term_doc_frequency(low) > 0 or len(low) < 4:
            out.append(low if has_cyr else tok)
            continue
        fix = _closest_stem(low)
        if fix and fix != low:
            out.append(fix)
            changed = True
        else:
            out.append(low)
    canonical = " ".join(out)
    return canonical, changed


_PAGE_CONTENT_CACHE = {}

# ---------------------------------------------------------------------------
# Свежесть кэшей относительно индекса.
# Индекс (cache/kb_index.db) пересобирается build_index (launchd по WatchPaths),
# поэтому все in-memory кэши, производные от содержимого страниц (контент, стемы
# страниц/абзацев, частоты терминов), должны сбрасываться при изменении mtime.
# ---------------------------------------------------------------------------
_INDEX_MTIME = None


def index_mtime():
    try:
        return os.path.getmtime(DB_PATH)
    except OSError:
        return 0.0


def _detect_index_change():
    """Сброс кэшей содержимого/стемов, если индекс обновился с прошлого раза."""
    global _INDEX_MTIME
    mt = index_mtime()
    if _INDEX_MTIME is None:
        _INDEX_MTIME = mt
        return False
    if mt == _INDEX_MTIME:
        return False
    _INDEX_MTIME = mt
    # кэши стемов защищены своими блокировками (сохранение на диск итерирует их)
    with _PAGE_STEMS_LOCK:
        with _PARAGRAPH_LOCK:
            _PAGE_STEMS_CACHE.clear()
            _PARAGRAPH_STEMS.clear()
    _PAGE_CONTENT_CACHE.clear()
    DFS_CACHE.clear()
    return True


# Персистентный кэш стемов страниц и абзацев (по образцу stem_lexicon.pkl):
# морфоразбор содержимого страниц — самый дорогой узел холодного старта поиска
# (58 страниц кандидатов за один запрос). Страницы статичны, поэтому считаем
# стемы один раз и сохраняем на диск; инвалидация по mtime индекса (см. выше).
_PAGE_STEMS_PATH = os.path.join(KB_ROOT, "cache", "page_stems.pkl")
_PAGE_STEMS_LOADED = False
_PAGE_STEMS_DIRTY = 0
_PAGE_STEMS_LAST_SAVE = 0.0
_PAGE_STEMS_SAVE_TRIGGER = 32  # страниц до принудительного сохранения
_PAGE_STEMS_SAVE_SECS = 5.0
_PAGE_STEMS_LOCK = threading.RLock()


def _load_page_stems_disk():
    """Загрузка кэша стемов страниц/абзацев с диска (при совпадении mtime
    индекса). Лениво, один раз за процесс; должна вызываться под
    _PAGE_STEMS_LOCK."""
    global _PAGE_STEMS_LOADED
    if _PAGE_STEMS_LOADED:
        return
    _PAGE_STEMS_LOADED = True
    try:
        with open(_PAGE_STEMS_PATH, "rb") as f:
            saved = pickle.load(f)
        if not (isinstance(saved, dict) and saved.get("_mtime") == index_mtime()):
            return
        for key, v in (saved.get("stems") or {}).items():
            _PAGE_STEMS_CACHE[key] = set(v)
        for key, v in (saved.get("paras") or {}).items():
            _PARAGRAPH_STEMS[key] = [(p, frozenset(w)) for p, w in v]
    except Exception:
        pass


def _save_page_stems_locked():
    """Сохранение стемов на диск (атомарно: tmp + rename). Под _PAGE_STEMS_LOCK."""
    global _PAGE_STEMS_DIRTY, _PAGE_STEMS_LAST_SAVE
    _PAGE_STEMS_DIRTY = 0
    _PAGE_STEMS_LAST_SAVE = time.time()
    payload = {
        "_mtime": index_mtime(),
        "stems": {k: sorted(v) for k, v in _PAGE_STEMS_CACHE.items()},
        "paras": {
            k: [[p, sorted(w)] for p, w in v]
            for k, v in _PARAGRAPH_STEMS.items()
        },
    }
    try:
        tmp = _PAGE_STEMS_PATH + ".tmp"
        with open(tmp, "wb") as f:
            pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, _PAGE_STEMS_PATH)
    except Exception:
        pass


def _mark_stems_dirty_and_save():
    """Подсчёт грязных страниц и периодическое сохранение (троттлинг).

    Под _PAGE_STEMS_LOCK. Сериализация вызова: если другой поток уже запустил
    сохранение, его результат покрывает и наши записи — флагов не дублируем.
    """
    global _PAGE_STEMS_DIRTY
    _PAGE_STEMS_DIRTY += 1
    now = time.time()
    if (_PAGE_STEMS_DIRTY >= _PAGE_STEMS_SAVE_TRIGGER
            or now - _PAGE_STEMS_LAST_SAVE >= _PAGE_STEMS_SAVE_SECS):
        _save_page_stems_locked()


def force_row(product, page, stems):
    """Строка результата (product, page, title, path, snippet, score) для
    страницы, выпавшей из кандидатов из-за продукт-сужения или отсутствия в
    топ-30 обеих ног. Нужно для роутинг-правил (fusion.route_rows): целевая
    страница правильная по смыслу, но гибрид её вообще не видит.
    """
    try:
        db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        row = db.execute(
            "SELECT title, path FROM pages WHERE product=? AND page=? "
            "ORDER BY chunk LIMIT 1",
            (product, page)).fetchone()
        db.close()
    except Exception:
        return None
    if not row:
        return None
    title, path = row
    try:
        snip = smart_snippet(product, page, stems)
    except Exception:
        snip = ""
    return (product, page, title or page, path or page, snip, 100.0)


def _page_content(product, page):
    """Весь текст страницы (все чанки) из SQLite. Кэшируется."""
    key = (product, page)
    if key in _PAGE_CONTENT_CACHE:
        return _PAGE_CONTENT_CACHE[key]
    text = ""
    try:
        db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        rows = db.execute(
            "SELECT content FROM pages WHERE product=? AND page=? ORDER BY chunk",
            (product, page)).fetchall()
        db.close()
        text = "\n".join(r[0] or "" for r in rows)
    except Exception:
        text = ""
    _PAGE_CONTENT_CACHE[key] = text
    return text


_PAGE_STEMS_CACHE = {}


def _page_stems(product, page):
    """Стеммированные слова страницы (все чанки). Кэшируется в памяти и на диск."""
    key = (product, page)
    with _PAGE_STEMS_LOCK:
        cached = _PAGE_STEMS_CACHE.get(key)
        if cached is not None:
            return cached
        _load_page_stems_disk()
        cached = _PAGE_STEMS_CACHE.get(key)
        if cached is not None:
            return cached
        from stem import stem_text
        stems = set()
        for p in stem_text(_page_content(product, page)):
            if p:
                stems.add(p)
        # параллельный поток мог построить ту же страницу — возвращаем его результат
        existed = _PAGE_STEMS_CACHE.get(key)
        if existed is not None:
            return existed
        _PAGE_STEMS_CACHE[key] = stems
        _mark_stems_dirty_and_save()
        return stems

def _page_has_terms(product, page, stems):
    """Содержит ли текст страницы любой из дискриминативных терминов.

    Сравниваем пословно стемы текста со стемами терминов (равенство или
    префикс-соответствие), а не точную подстроку в сыром тексте — так русская
    морфология («подключить»/«подключённых») корректно матчится.
    """
    if not stems:
        return False
    page_stems = _page_stems(product, page)
    if not page_stems:
        return False
    for term in stems:
        if any(w == term or w.startswith(term) or term.startswith(w)
               for w in page_stems):
            return True
    return False


_SNIPPET_MAX = 320

# Снниппет-кэш: стемы слов абзаца по странице. Морфоразбор (pymorphy3) слов
# абзаца — самый дорогой узел горячего пути (до 2.5с на запрос: 30 страниц ×
# абзацы × слова). Текст страниц статичен, поэтому стемы абзаца считаем один
# раз на страницу и переиспользуем во всех следующих запросах.
_SNIPPET_PARAS_LIMIT = 200  # дальше по странице сниппету делать нечего
_SNIPPET_PARAS_CACHE_MAX = 6144
_PARAGRAPH_STEMS = {}  # (product, page) -> [(text, frozenset(word_stems)), ...]
_PARAGRAPH_LOCK = threading.RLock()


def _paragraph_stems(product, page, paragraphs):
    """Стеммы слов каждого абзаца страницы (один раз, потом из кэша).

    Порядок блокировок общий для путей стемов страниц и абзацев:
    всегда _PAGE_STEMS_LOCK снаружи, _PARAGRAPH_LOCK внутри — без вложенного
    обратного порядка не бывает (сохранение тоже читает оба кэша).
    """
    key = (product, page)
    with _PAGE_STEMS_LOCK:
        with _PARAGRAPH_LOCK:
            cached = _PARAGRAPH_STEMS.get(key)
            if cached is not None:
                return cached
        _load_page_stems_disk()
        with _PARAGRAPH_LOCK:
            cached = _PARAGRAPH_STEMS.get(key)
            # загруженный с диска кэш валиден, только если абзацы совпадают по числу
            if cached is not None and len(cached) == len(paragraphs):
                return cached
            from stem import stem_word
            out = []
            for p in paragraphs:
                words = [stem_word(w.lower()) for w in TOKEN_RE.findall(p)]
                out.append((p, frozenset(w for w in words if w)))
            if len(_PARAGRAPH_STEMS) >= _SNIPPET_PARAS_CACHE_MAX:
                _PARAGRAPH_STEMS.clear()
            _PARAGRAPH_STEMS[key] = out
        _mark_stems_dirty_and_save()
        return out


def smart_snippet(product, page, stems, max_len=_SNIPPET_MAX):
    """Связный сниппет по полному тексту страницы.

    FTS5 snippet() для совпадений в середине большого документа возвращает
    фрагмент, начинающийся с '…' посреди фразы (обрыв контекста). Здесь мы
    строим сниппет по абзацам: выбираем первый абзац с наибольшим покрытием
    терминов, якоря на границу абзаца и подсвечиваем термины маркерами ⟦⟧
    (clean_snippet потом превращает их в жирный).
    """
    content = _page_content(product, page)
    if not content or not stems:
        return ""
    paragraphs = [ln.strip() for ln in content.splitlines()
                  if ln.strip()][:_SNIPPET_PARAS_LIMIT]
    if not paragraphs:
        return ""
    para = _paragraph_stems(product, page, paragraphs)
    para_stems = [w for _, w in para]

    def paragraph_score(i):
        wstems = para_stems[i]
        text = para[i][0]
        matched = 0
        for t in stems:
            if any(w == t or w.startswith(t) or t.startswith(w) for w in wstems):
                matched += 1
        return matched, matched - max(0, len(text) - max_len) / 200.0

    best_p, best_i, best_score = None, -1, (-1, -1)
    for i in range(len(paragraphs)):
        score = paragraph_score(i)
        # предпочитаем абзац с максимальным покрытием; при равенстве — более ранний
        if score[0] > best_score[0] or (
            score[0] == best_score[0] and score[1] > best_score[1]):
            best_score = score
            best_i = i
    if best_i < 0:
        return ""
    best_text = paragraphs[best_i]
    if len(best_text) > max_len:
        best_text = best_text[:max_len].rstrip() + "…"
    # подсветка терминов маркерами (как в clean_snippet, который переводит их в <b>)
    for t in stems:
        pattern = re.compile(r"(?<!\w)" + re.escape(t) + r"\w*", re.I)
        best_text = pattern.sub(lambda m: _HL_OPEN + m.group(0) + _HL_CLOSE,
                                best_text)
    return best_text


def _layout_variant(query: str):
    """Кириллический вариант запроса, если он набран в неверной раскладке."""
    try:
        from layout import detect_and_fix_layout
        fixed = detect_and_fix_layout(query)
        return fixed if fixed and fixed != query else None
    except Exception:
        return None


def _result_coverage(query, rows):
    """Какая доля топ-3 результатов содержит хотя бы один стем запроса.

    Считается по стемам страниц (`_page_has_terms`). Непустой, но «шумный»
    результат (векторные совпадения без совпадения терминов) даёт низкое
    покрытие — это признак неверной раскладки, а не релевантного ответа.
    """
    if not rows:
        return 0.0
    stems = _correct_terms(normalize_terms(terms(query)))
    if not stems:
        return 0.0

    def key(r):
        if isinstance(r, dict):
            return (r.get("product"), r.get("page"))
        return (r[0], r[1])

    hit = sum(1 for r in rows[:3]
              if _page_has_terms(key(r)[0], key(r)[1], stems))
    return hit / min(3, len(rows[:3]))


def _result_term_coverage(query, rows):
    """Какая доля ТЕРМИНОВ запроса присутствует хотя бы в одной из топ-3 страниц.

    Зависит от того, какие термины вообще ввели. Если продукт-детектор сузил
    поиск, пропущенный термин (напр. «выгрузка» в marketplace-продукте против
    parts-resource-guide) виден здесь как дыра, хотя «доля результатов с термином»
    (_result_coverage) остаётся высокой.
    """
    if not rows:
        return 0.0
    stems = _correct_terms(normalize_terms(terms(query)))
    if not stems:
        return 0.0

    def key(r):
        if isinstance(r, dict):
            return (r.get("product"), r.get("page"))
        return (r[0], r[1])

    keys = [key(r) for r in rows[:3]]
    return sum(
        1 for t in stems
        if any(_page_has_terms(k[0], k[1], [t]) for k in keys)
    ) / len(stems)


def search_hybrid(query, product=None, limit=5, snippets=True, embed_host=None,
                  with_meta=False, _skip_auto_product=False):
    """Гибридный поиск с распознаванием продукта и исправлением раскладки.

    with_meta: дополнительно возвращать meta из fuse ((rows, elapsed, meta)).
    Для боевых вызовов контракт прежний — (rows, elapsed).

    - Если в запросе явно назван продукт («Parts.Resource», «Интеллект» и пр.)
      и product не задан — поиск ограничивается только этим продуктом.
    - Раскладку исправляем как fallback: если основной поиск не нашёл ничего
      ИЛИ дал результат плохого качества (мало терминов в топе), а запрос похож
      на русский в латинской раскладке — повторяем поиск по восстановленной
      кириллице и возвращаем его, если он заметно лучше. Легитимный латинский
      транслит (например «nastrojka onlajn kassy») при этом не ломается, т.к.
      кириллический вариант по качеству не превосходит основной.

    _skip_auto_product: не определять продукт по запросу вообще (продукт
    остаётся None) — кросс-продуктовый поиск по всем продуктам для внешнего
    вызывает, которому нужен именно широкий пул (см. cross_search тематическую
    экспансию).
    """
    _detect_index_change()
    if product is None and not _skip_auto_product:
        product = detect_product_name(query)
    detected_product = product
    web_payment = False
    if product is None and not _skip_auto_product:
        p = web_payment_product(query)
        if p:
            product = p
            web_payment = True

    if web_payment:
        # Тема «оплата/эквайринг на сайте» семантически однозначна для эмбеддинга,
        # а FTS5 без стемминга для таких запросов даёт лишь шум (страницы-доноры
        # «сайт/подключ», не попавшие в векторный топ, получают RRF-буст и глушат
        # верные ответы оплаты). Поэтому здесь полагаемся на векторный поиск по
        # продукту — он возвращает именно «способы оплаты» Parts.Resource.
        rows, elapsed = vector_main(query, product, limit, embed_host)
        return rows, elapsed

    rows, elapsed, meta = _hybrid_inner(query, product, limit, snippets, embed_host)

    # Роутинг-правила применяем ПОСЛЕ wide/layout-фолбэков: они видят финальный
    # список и могут принудительно вернуть страницу, выпавшую из-за
    # продукт-сужения (см. fusion.route_rows).
    def _routed(r):
        from fusion import route_rows
        return route_rows(query, r)

    # Wide-fallback: если автодетектированный продукт сузил поиск, а результат
    # слабый (мало терминов в топе), повторяем по ВСЕМ продуктам и, если он
    # заметно лучше покрывает термины запроса, берём его. Детект ошибочен,
    # когда имя продукта совпало со словом запроса («выгрузка товаров на
    # маркетплейс» -> marketplace держит ответ в parts-resource-guide,
    # «версия 6.74» -> parts-resource-guide скрывает parts-resource-changes).
    #
    # Широкий пул НЕ должен вытеснять узкий, если тот уже осмысленно покрывает
    # запрос: «печать чеков … resource … оплаты на сайте» в parts-resource-guide
    # даёт 24 релевантных страницы, а wide-вариация по шумному запросу («зфкеы»)
    # возвращает 3 обрывочных страницы Parts.Intellect, «выигрывая» покрытием
    # 1.0 против 0.88 только за счёт случайного распределения терминов.
    # Требуем: заметный выигрыш по покрытию И широкий пул не единичный.
    if (detected_product is not None and detected_product == product
            and not web_payment and rows):
        det_cov = _result_term_coverage(query, rows)
        if det_cov < 1.0:
            wide_rows, _, wide_meta = _hybrid_inner(
                query, None, limit, snippets, embed_host)
            wide_cov = _result_term_coverage(query, wide_rows) if wide_rows else 0.0
            if (wide_rows and len(wide_rows) >= 4
                    and wide_cov >= det_cov + 0.05):
                rows = wide_rows
                meta = wide_meta
            elif (wide_rows and len(wide_rows) < 4
                    and wide_cov >= det_cov + 0.20):
                # Единичный широкий пул берём только при ярком выигрыше:
                # иначе 2-3 обрывочных страницы Parts.Intellect вытесняют
                # хорошо покрывающий узкий пул (отчёт о «печать чеков…
                # resource»).
                rows = wide_rows
                meta = wide_meta

    # Платёжная экспансия Parts.Resource: «эквайринг…» без POS-сигналов — обычно про
    # POS-терминал Parts.Intellect, но пользователи ждут и способы оплаты/онлайн-кассу
    # Parts.Resource (страницы которых слово «эквайринг» не содержат). Дополняем пул
    # отдельным продукт-ограниченным поиском сразу после ведущего ответа.
    if (detected_product is None and product is None and rows and not web_payment):
        pr_rows = payment_resource_rows(query, limit=4)
        if pr_rows:
            seen = {(r[0], r[1]) for r in rows}
            sec = [r for r in pr_rows if (r[0], r[1]) not in seen]
            if sec:
                rows = ([rows[0]] + sec
                        + [r for r in rows[1:] if (r[0], r[1]) not in seen])
                rows = rows[:limit]

    # Fallback по раскладке: только когда результат слабый (пустой или шумный).
    if product is None and rows:
        primary_cov = _result_coverage(query, rows)
    else:
        primary_cov = 0.0

    if (product is None
            and (not rows or primary_cov < 0.34)):
        alt = _layout_variant(query)
        if alt:
            alt_rows, _, alt_meta = _hybrid_inner(alt, None, limit, snippets, embed_host)
            if alt_rows:
                alt_cov = _result_coverage(alt, alt_rows)
                if not rows:
                    if with_meta:
                        return _routed(alt_rows), elapsed, alt_meta
                    return _routed(alt_rows), elapsed
                if alt_cov >= primary_cov + 0.34:
                    if with_meta:
                        return _routed(alt_rows), elapsed, alt_meta
                    return _routed(alt_rows), elapsed
    if with_meta:
        return _routed(rows), elapsed, meta
    return _routed(rows), elapsed


def _hybrid_inner(query, product, limit, snippets, embed_host):
    """Собственно слияние FTS5 + vector для одного запроса/продукта.
    Возвращает (rows, elapsed_ms, meta): meta из fuse (с фичами результатов)."""
    try:
        import embed
        from typesense_client import vector_search
    except ImportError:
        return search(terms(query), product, limit, snippets), 0.0, None

    if not embed.ollama_is_available(host=embed_host):
        return search(terms(query), product, limit, snippets), 0.0, None

    t0 = time.perf_counter()
    norm_terms = normalize_terms(terms(query))

    # 1) FTS5 results (сниппет не строим здесь — заполним его для итоговых
    #    строк ниже: из 30 кандидатов RRF/дедуп берёт ~24, и морфоразбор
    #    абзацев для отброшенных был чистым перерасходом CPU/GIL).
    fts_rows, _ = search(terms(query), product, limit=RRF_TOP, snippets=False)

    # 2) Vector results (по canonical-запросу: нижний регистр + исправление
    #    опечаток — иначе embed видит регистр/опечатки и искажает соседей).
    vec_rows = []
    try:
        canonical, _ = _canonical_query(query)
        vec = embed.embed_text(canonical, host=embed_host)
        vec_hits = vector_search(vec, k=RRF_TOP, product=product)
        # Normalize to same row format: (product, page, title, path, snippet, score)
        for h in vec_hits:
            vec_rows.append((
                h.get("product"), h.get("page"), h.get("title"),
                h.get("path"), "", h.get("_score", 0),
            ))
    except Exception:
        vec_rows = []

    # 3) Уверенное слияние (общий модуль fusion.py): RRF + терм-буст +
    #    API-intent буст + развязка ничьих по векторной уверенности (vec_score).
    from fusion import fuse
    merge_keys, meta = fuse(query, fts_rows, vec_rows, product)

    # 4) Топ-N (с дедупликацией кросс-продуктовых страниц при product=None)
    merged = merge_keys[:limit]
    if product is None and merged:
        deduped = {}  # page -> (key, products_set)
        for key in merged:
            pg = key[1]
            if pg not in deduped:
                deduped[pg] = (key, {key[0]})
            else:
                deduped[pg][1].add(key[0])
        merged = [v[0] for v in deduped.values()]
        page_products = {k: sorted(v[1]) for k, v in deduped.items()}
    else:
        page_products = {}

    # 5) Map back to full row
    fts_by_key = {(r[0], r[1]): r for r in fts_rows}
    vec_by_key = {(r[0], r[1]): r for r in vec_rows}
    out = []
    snippet_stems = _correct_terms(norm_terms) if snippets else []
    for key in merged:
        row = None
        if key in fts_by_key:
            row = list(fts_by_key[key])
        elif key in vec_by_key:
            row = list(vec_by_key[key])
        if row is None or not (row[2] or "").strip():
            continue
        pg = key[1]
        if pg in page_products and len(page_products[pg]) > 1:
            row = list(row)
            row[0] = ",".join(page_products[pg])  # comma-separated products
            row[4] = ""  # clear snippet for deduped
            row = tuple(row)
        elif snippets and snippet_stems and not row[4]:
            # Сниппет для итоговых строк (в т.ч. векторных, у них его нет).
            row = list(row)
            row[4] = smart_snippet(row[0], row[1], snippet_stems)
            row = tuple(row)
        out.append(row)

    elapsed = (time.perf_counter() - t0) * 1000

    if not out:
        # fallback
        return fallback_like(terms(query), product, limit), 0.0, meta
    return out, elapsed, meta


def terms(query):
    return [t for t in query.split() if t]


def vector_main(query, product=None, limit=5, embed_host=None):
    """Чисто векторный поиск через Typesense + Ollama.

    Возвращает rows в том же формате, что search():
      (product, page, title, path, snippet, score)
    embed_host: переопределение хоста Ollama для эмбеддинга запроса.
    """
    try:
        import embed
        from typesense_client import vector_search
    except ImportError:
        return [], 0.0

    if not embed.ollama_is_available(host=embed_host):
        import sys
        print("Warning: Ollama недоступна, векторный поиск невозможен. "
              "Попробуйте --mode fts", file=sys.stderr)
        return [], 0.0

    t0 = time.perf_counter()
    try:
        canonical, _ = _canonical_query(query)
        vec = embed.embed_text(canonical, host=embed_host)
        vec_hits = vector_search(vec, k=limit * 10, product=product)
    except Exception as e:
        return [], 0.0

    rows = []
    for h in vec_hits[:limit]:
        if not (h.get("title") or "").strip():
            continue
        rows.append((
            h.get("product"), h.get("page"), h.get("title") or "",
            h.get("path"), "", h.get("_score", 0),
        ))
    elapsed = (time.perf_counter() - t0) * 1000
    return rows, elapsed


def main():
    ap = argparse.ArgumentParser(description="Поиск по БД (FTS5-индекс)")
    ap.add_argument("query", help="Поисковый запрос")
    ap.add_argument("--product", choices=sorted(PRODUCTS), help="Продукт")
    ap.add_argument("--auto", action="store_true", help="Без вопросов, поиск по всем продуктам")
    ap.add_argument("--top", type=int, default=5, help="Сколько результатов (по умолчанию 5)")
    ap.add_argument("--no-snippet", action="store_true", help="Не показывать сниппеты")
    ap.add_argument("--mode", choices=["fts", "vector", "hybrid"], default="hybrid",
                    help="Режим поиска (по умолчанию hybrid)")
    args = ap.parse_args()

    terms_ = terms(args.query)
    if not terms_:
        sys.exit("Пустой запрос")

    product = ask_product(args.query, forced_product=args.product, auto=args.auto)
    if product:
        print(f"Продукт: {PRODUCTS[product]}")

    if args.mode == "hybrid":
        rows, elapsed = search_hybrid(args.query, product, args.top, not args.no_snippet)
    elif args.mode == "vector":
        rows, elapsed = vector_main(args.query, product, args.top)
    else:
        rows, elapsed = search(terms_, product, args.top, not args.no_snippet)
    if not rows:
        print(f"Найдено: {len(rows)} (время {elapsed:.1f} мс)")
        print("По введенным данным нет результатов.")
        print("Попробуйте изменить формулировку запроса или укажите продукт.")
        return
    print(f"Найдено: {len(rows)} (время {elapsed:.1f} мс)")
    for product, page, title, path, snippet, _score in rows:
        print()
        print(f"  [{PRODUCTS.get(product, product)}] {title or page}")
        print(f"  файл: {os.path.relpath(path, KB_ROOT)}")
        if snippet and not args.no_snippet:
            print(f"  {clean_snippet(snippet)}")


if __name__ == "__main__":
    main()
