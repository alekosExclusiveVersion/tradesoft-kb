#!/usr/bin/env python3
"""Vector search CLI.

Usage:
  python3 vector_search.py "оплата картой"
  python3 vector_search.py "оплата картой" --top 10
  python3 vector_search.py "НДС эквайринг" --product parts-resource-guide
"""
import argparse
import sys

from config import EMBEDDING_MODEL
from embed import embed_text, ollama_is_available
from typesense_client import vector_search
from search import PRODUCTS


def main():
    ap = argparse.ArgumentParser(description="Векторный поиск по Typesense")
    ap.add_argument("query", help="Поисковый запрос")
    ap.add_argument("--product", choices=sorted(PRODUCTS), help="Продукт")
    ap.add_argument("--top", type=int, default=5, help="Сколько результатов (по умолчанию 5)")
    args = ap.parse_args()

    if not ollama_is_available():
        sys.exit("Ollama недоступна. Убедитесь, что Ollama запущена.")

    query = args.query.strip()
    if not query:
        sys.exit("Пустой запрос")

    import time
    t0 = time.time()

    print(f"Эмбеддинг запроса через {EMBEDDING_MODEL}...")
    vector = embed_text(query)
    embed_ms = (time.time() - t0) * 1000

    t1 = time.time()
    hits = vector_search(vector, k=args.top * 10, product=args.product)
    search_ms = (time.time() - t1) * 1000
    total_ms = (time.time() - t0) * 1000

    hits = hits[:args.top]

    print(f"Найдено: {len(hits)} (embed {embed_ms:.0f}ms, search {search_ms:.0f}ms, "
          f"total {total_ms:.0f}ms)")
    for i, h in enumerate(hits, 1):
        score = h.get("_score", 0)
        print(f"\n{i}. [{PRODUCTS.get(h.get('product'), h.get('product'))}] "
              f"{h.get('title') or h.get('page')}  (cosine={score:.4f})")
        print(f"   файл: {h.get('path')}  (chunk {h.get('chunk_idx')})")
        content = (h.get('content') or '').replace('\n', ' ')[:150]
        print(f"   {content}...")


if __name__ == "__main__":
    main()