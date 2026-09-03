#!/usr/bin/env python3
"""Vector index builder: reads chunks from SQLite FTS index → embeds → upserts to Typesense.

Incremental: only re-indexes chunks whose mtime differs from what's stored
in Typesense. Full rebuild with --rebuild.

Usage:
  python3 build_vector_index.py
  python3 build_vector_index.py --rebuild
  python3 build_vector_index.py --dry-run
"""
import argparse
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
KB_ROOT = os.path.dirname(SCRIPT_DIR)
DB_PATH = os.path.join(KB_ROOT, "cache", "kb_index.db")

sys.path.insert(0, SCRIPT_DIR)
from config import COLLECTION_NAME
from typesense_client import (
    ensure_collection, delete_collection, upsert_documents,
    collection_stats, export_doc_ids,
)
from embed import embed_text, ollama_is_available


def get_chunks_from_sqlite():
    """Read all chunks from the SQLite pages table."""
    if not os.path.exists(DB_PATH):
        sys.exit(f"Индекс не найден: {DB_PATH}. Запустите scripts/build_index.py")
    db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    rows = db.execute(
        "SELECT product, page, title, path, chunk, mtime, content FROM pages ORDER BY product, page, chunk"
    ).fetchall()
    db.close()
    return rows


def make_doc_id(product: str, page: str, chunk: int) -> str:
    """Create a deterministic document ID."""
    clean_page = page.replace(".htm.md", "").replace("/", "_")
    return f"{product}__{clean_page}__{chunk}"


def build_index(rebuild: bool = False, dry_run: bool = False,
                batch_size: int = 16, workers: int = 4):
    """Main indexing pipeline."""
    if not ollama_is_available():
        sys.exit("Ollama недоступна. Убедитесь, что Ollama запущена.")

    chunks = get_chunks_from_sqlite()
    print(f"Всего чанков в SQLite: {len(chunks)}")

    if not chunks:
        print("Нет чанков для индексации")
        return

    if rebuild:
        print("Полная переиндексация: удаляю коллекцию...")
        delete_collection(COLLECTION_NAME)

    ensure_collection(COLLECTION_NAME)

    if not rebuild:
        existing = export_doc_ids(COLLECTION_NAME)
        kept = [
            row for row in chunks
            if make_doc_id(row[0], row[1], row[4]) not in existing
        ]
        skipped = len(chunks) - len(kept)
        if skipped:
            print(f"Уже в Typesense: {skipped}, к индексации новых: {len(kept)}")
        chunks = kept
        if not chunks:
            print("Новых чанков нет — индекс актуален")
            return

    if dry_run:
        print("[DRY RUN] Индексация не будет выполнена")
        # Show sample
        for row in chunks[:3]:
            product, page, title, path, chunk_idx, mtime, content = row
            doc_id = make_doc_id(product, page, chunk_idx)
            preview = content[:100].replace("\n", " ")
            print(f"  {doc_id}: [{product}] {title} (chunk {chunk_idx}, {len(content)} chars)")
            print(f"    preview: {preview}...")
        return

    t0 = time.time()
    indexed = 0
    errors = 0
    total = len(chunks)

    def embed_one(row):
        """Embed a single chunk. Returns (doc, err) or (None, err_string)."""
        product, page, title, path, chunk_idx, mtime, content = row
        doc_id = make_doc_id(product, page, chunk_idx)
        try:
            vector = embed_text(content)
        except Exception as e:
            return None, f"[{doc_id}] {e}"
        doc = {
            "id": doc_id,
            "product": product,
            "page": page,
            "title": title or "",
            "path": path,
            "chunk_idx": chunk_idx,
            "content": content,
            "embedding": vector,
        }
        return doc, None

    in_flight = workers * 2
    pending = {}          # future -> row
    docs_to_upsert = []
    submitted = 0

    def upsert_batch():
        nonlocal indexed, errors
        if not docs_to_upsert:
            return
        batch = docs_to_upsert[:]
        try:
            result = upsert_documents(batch)
            # import returns a list of per-line results
            fail = 0
            if isinstance(result, list):
                fail = sum(1 for r in result if isinstance(r, dict) and not r.get("success"))
            indexed += len(batch) - fail
            errors += fail
            if fail:
                print(f"\n  ОШИБКА upsert: {fail} из {len(batch)} не записаны")
        except Exception as e:
            print(f"\n  ОШИБКА batch upsert: {e}")
            errors += len(batch)
        docs_to_upsert.clear()
        elapsed = time.time() - t0
        rate = indexed / elapsed if elapsed > 0 else 0
        print(f"\r  Индексировано: {indexed}/{total} ({rate:.1f} doc/s, {errors} ошибок)",
              end="", flush=True)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        # Fill the in-flight window
        while submitted < min(in_flight, total):
            fut = pool.submit(embed_one, chunks[submitted])
            pending[fut] = chunks[submitted]
            submitted += 1

        while pending:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for fut in done:
                row = pending.pop(fut)
                doc, err = fut.result()
                if err:
                    print(f"\n  ОШИБКА embed {err}")
                    errors += 1
                elif doc:
                    docs_to_upsert.append(doc)

                # Submit a replacement to keep the window full
                if submitted < total:
                    fut2 = pool.submit(embed_one, chunks[submitted])
                    pending[fut2] = chunks[submitted]
                    submitted += 1

            if len(docs_to_upsert) >= batch_size:
                upsert_batch()

        if docs_to_upsert:
            upsert_batch()

    elapsed = time.time() - t0
    stats = collection_stats()
    print(f"\n\nГотово за {elapsed:.1f}s:")
    print(f"  Индексировано: {indexed}")
    print(f"  Ошибок: {errors}")
    print(f"  Документов в Typesense: {stats['num_documents']}")


def main():
    ap = argparse.ArgumentParser(description="Построение векторного индекса в Typesense")
    ap.add_argument("--rebuild", action="store_true",
                    help="Полная переиндексация (удалить и пересоздать коллекцию)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Показать что будет проиндексировано, без реальной индексации")
    ap.add_argument("--batch-size", type=int, default=16,
                    help="Размер батча для upsert (по умолчанию 16)")
    ap.add_argument("--workers", type=int, default=4,
                    help="Число параллельных запросов к Ollama (по умолчанию 4)")
    args = ap.parse_args()
    build_index(rebuild=args.rebuild, dry_run=args.dry_run,
                batch_size=args.batch_size, workers=args.workers)


if __name__ == "__main__":
    main()
