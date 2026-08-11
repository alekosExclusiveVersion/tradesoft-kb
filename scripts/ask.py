#!/usr/bin/env python3
"""Поиск релевантных страниц и вывод контекста для LLM.

Примеры:
  python3 scripts/ask.py "передача НДС в эквайринг"
  python3 scripts/ask.py "ставка НДС онлайн касса" --product parts-resource-guide
  python3 scripts/ask.py "интернет-магазин оплата картой" --top 3 --max-chars 12000
"""
import argparse
import os
import sqlite3
import sys

from search import PRODUCTS, ask_product, clean_content, search

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
KB_ROOT = os.path.dirname(SCRIPT_DIR)
DB_PATH = os.path.join(KB_ROOT, "cache", "kb_index.db")


def load_chunks(product, page, max_chars):
    db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    rows = db.execute(
        "SELECT chunk, content FROM pages "
        "WHERE product=? AND page=? ORDER BY chunk",
        (product, page),
    ).fetchall()
    db.close()
    out = []
    size = 0
    for chunk, content in rows:
        if size + len(content) > max_chars and out:
            break
        out.append(content)
        size += len(content)
    return "".join(out)


def main():
    ap = argparse.ArgumentParser(description="Контекст по запросу для LLM")
    ap.add_argument("query", help="Поисковый запрос")
    ap.add_argument("--product", choices=sorted(PRODUCTS), help="Продукт")
    ap.add_argument("--auto", action="store_true", help="Без вопросов, поиск по всем продуктам")
    ap.add_argument("--top", type=int, default=4, help="Сколько страниц (по умолчанию 4)")
    ap.add_argument("--max-chars", type=int, default=20000,
                    help="Максимум символов на страницу (по умолчанию 20000)")
    ap.add_argument("--no-sources", action="store_true", help="Без списка источников")
    args = ap.parse_args()

    if not os.path.exists(DB_PATH):
        sys.exit(f"Индекс не найден: {DB_PATH}. Запустите scripts/build_index.py")

    terms = [t for t in args.query.split() if t]
    if not terms:
        sys.exit("Пустой запрос")

    product = ask_product(args.query, forced_product=args.product, auto=args.auto)
    if product:
        print(f"Продукт: {PRODUCTS[product]}")

    rows, elapsed = search(terms, product, args.top)
    if not rows:
        sys.exit("По введенным данным нет результатов.\n"
                 "Попробуйте изменить формулировку запроса или укажите продукт.")

    for product, page, title, path, _snippet, _score in rows:
        content = clean_content(load_chunks(product, page, args.max_chars))
        print(f"===== [Страница] {title or page} ({PRODUCTS.get(product, product)}) =====")
        print(content.rstrip())
        print()

    if not args.no_sources:
        print("===== [Источники] =====")
        for product, page, title, path, _snippet, _score in rows:
            print(f"- {title or page} ({PRODUCTS.get(product, product)}): "
                  f"{os.path.relpath(path, KB_ROOT)}")


if __name__ == "__main__":
    main()
