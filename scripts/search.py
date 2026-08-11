#!/usr/bin/env python3
"""Поиск по FTS5-индексу БД.

Примеры:
  python3 scripts/search.py "НДС эквайринг"
  python3 scripts/search.py "налоговая система" --product parts-resource-guide
  python3 scripts/search.py "nastrojka onlajn kassy" --top 3
"""
import argparse
import os
import re
import sqlite3
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
KB_ROOT = os.path.dirname(SCRIPT_DIR)
DB_PATH = os.path.join(KB_ROOT, "cache", "kb_index.db")

PRODUCTS = {
    "parts-intellect-guide": "Parts.Intellect",
    "parts-intellect-synch": "Синхронизатор",
    "parts-resource-guide": "Parts.Resource",
    "parts-resource-rest-api": "Parts.Resource — REST API",
}

PRODUCT_MARKERS = {
    "parts-intellect-synch": ["синхронизац", "перенос", "передач", "синхрониз"],
    "parts-resource-rest-api": ["api", "rest", "json", "метод", "endpoint", "curl", "параметр запроса"],
    "parts-resource-guide": ["интернет-магазин", "сайт", "корзина", "прайс-лист", "прайс лист",
                             "поставщик", "клиентская часть", "каталог", "пополнение баланса"],
    "parts-intellect-guide": ["наша фирма", "склад", "приходная", "расходная", "торговая точка",
                              "эквайринг", "интеллект", "касс"],
}


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
    """Убирает стоп-слова и разбивает дефисные слова («прайс-листов» → «прайс», «листов»)."""
    out = []
    for t in terms:
        t = t.strip().strip('"').lower()
        if t in STOPWORDS or len(t) < 2:
            continue
        for part in t.replace("-", " ").split():
            if part not in STOPWORDS and len(part) >= 2 and part not in out:
                out.append(part)
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


def search(terms, product=None, limit=5, snippets=True):
    if not os.path.exists(DB_PATH):
        sys.exit(f"Индекс не найден: {DB_PATH}. Запустите scripts/build_index.py")
    db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)

    t0 = time.perf_counter()
    terms = normalize_terms(terms)
    if not terms:
        db.close()
        return [], 0.0

    cond = " AND product=?" if product else ""
    args_extra = [product] if product else []
    q = fts_query(terms, "AND")
    sql = (
        "SELECT product, page, title, path, snippet(chunks_fts, 1, '⟦', '⟧', '…', 24), "
        "bm25(chunks_fts), content FROM chunks_fts WHERE chunks_fts MATCH ?"
        + cond
        + " ORDER BY rank LIMIT 500"
    )

    ranked = []
    for r in db.execute(sql, [q] + args_extra).fetchall():
        product_r, page, title, path, snip, score, content = r
        words = word_positions(title) + word_positions(content)
        first = {}
        ok = True
        for t in terms:
            hits = [i for i, w in enumerate(words) if w.startswith(t)]
            if not hits:
                ok = False
                break
            first[t] = min(hits)
        if not ok:
            continue
        span = max(first.values()) - min(first.values())
        if span > PROXIMITY_WINDOW:
            continue
        title_hits = sum(
            1 for t in terms if any(w.startswith(t) for w in word_positions(title))
        )
        ph = phrase_hits(words, terms)
        ranked.append((-ph, -title_hits, score, span, r[:6]))
    ranked.sort(key=lambda k: k[:4])
    rows = [k[4] for k in ranked[:limit]]
    elapsed = (time.perf_counter() - t0) * 1000
    db.close()

    if not rows:
        rows = fallback_like(terms, product, limit)
    return rows, elapsed


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
    rows = db.execute(sql, args).fetchall()
    db.close()
    return rows


def main():
    ap = argparse.ArgumentParser(description="Поиск по БД (FTS5-индекс)")
    ap.add_argument("query", help="Поисковый запрос")
    ap.add_argument("--product", choices=sorted(PRODUCTS), help="Продукт")
    ap.add_argument("--auto", action="store_true", help="Без вопросов, поиск по всем продуктам")
    ap.add_argument("--top", type=int, default=5, help="Сколько результатов (по умолчанию 5)")
    ap.add_argument("--no-snippet", action="store_true", help="Не показывать сниппеты")
    args = ap.parse_args()

    terms = [t for t in args.query.split() if t]
    if not terms:
        sys.exit("Пустой запрос")

    product = ask_product(args.query, forced_product=args.product, auto=args.auto)
    if product:
        print(f"Продукт: {PRODUCTS[product]}")

    rows, elapsed = search(terms, product, args.top, not args.no_snippet)
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
