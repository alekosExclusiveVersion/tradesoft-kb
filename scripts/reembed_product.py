#!/usr/bin/env python3
"""Incremental re-embed + upsert of chunks for a single product.

Reads only the selected product's chunks from SQLite, embeds them,
and upserts (overwrites) those docs in the Typesense vector collection.
Useful when content of one source changes, avoiding a full rebuild.
"""
import argparse
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
KB_ROOT = os.path.dirname(SCRIPT_DIR)
DB_PATH = os.path.join(KB_ROOT, "cache", "kb_index.db")

sys.path.insert(0, SCRIPT_DIR)
from config import COLLECTION_NAME
from typesense_client import ensure_collection, upsert_documents
from embed import embed_text, ollama_is_available


def get_chunks_for_product(product):
    if not os.path.exists(DB_PATH):
        sys.exit(f"Индекс не найден: {DB_PATH}. Запустите scripts/build_index.py")
    db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    rows = db.execute(
        "SELECT product, page, title, path, chunk, mtime, content "
        "FROM pages WHERE product = ? ORDER BY page, chunk",
        (product,),
    ).fetchall()
    db.close()
    return rows


def make_doc_id(product, page, chunk):
    clean_page = page.replace(".htm.md", "").replace("/", "_")
    return f"{product}__{clean_page}__{chunk}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--product", required=True)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--batch", type=int, default=16)
    args = ap.parse_args()

    if not ollama_is_available():
        sys.exit("Ollama недоступна. Убедитесь, что Ollama запущена.")

    chunks = get_chunks_for_product(args.product)
    print(f"Чанков продукта '{args.product}': {len(chunks)}")
    if not chunks:
        return

    ensure_collection(COLLECTION_NAME)
    t0 = time.time()
    docs = []
    errors = 0
    total = len(chunks)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(embed_text, row[6]): (row[0], row[1], row[4])
            for row in chunks
        }
        for fut in futures:
            row = futures[fut]
            try:
                vector = fut.result()
            except Exception as e:
                errors += 1
                print(f"  ОШИБКА embed {row}: {e}")
                continue
            prod, page, chunk = row
            doc_id = make_doc_id(prod, page, chunk)
            docs.append({"id": doc_id, "embedding": vector})

    for i in range(0, len(docs), args.batch):
        batch = docs[i:i + args.batch]
        try:
            res = upsert_documents(batch)
            ok = len(res) if isinstance(res, list) else int(res)
            if ok < len(batch):
                print(f"  upsert: {ok}/{len(batch)} записаны")
        except Exception as e:
            print(f"  ОШИБКА batch upsert: {e}")
            errors += len(batch)

    dt = time.time() - t0
    print(f"Готово: {len(docs)} docs upsert, ошибок {errors}, {dt:.1f}s "
          f"({total} чанков, {total/dt:.2f} doc/s)")


if __name__ == "__main__":
    main()
