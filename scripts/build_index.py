#!/usr/bin/env python3
"""Строит/обновляет SQLite FTS5-индекс по страницам БД.

Индекс: <KB_ROOT>/cache/kb_index.db
Инкрементальность: переиндексируются только страницы с изменённым mtime.
Крупные страницы разбиваются на секции по заголовкам (## / ###).
"""
import os
import re
import sqlite3

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
KB_ROOT = os.path.dirname(SCRIPT_DIR)
CACHE_DIR = os.path.join(KB_ROOT, "cache")
DB_PATH = os.path.join(CACHE_DIR, "kb_index.db")
MAX_CHUNK = 15000

SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
    id INTEGER PRIMARY KEY,
    product TEXT NOT NULL,
    page TEXT NOT NULL,
    title TEXT NOT NULL,
    path TEXT NOT NULL,
    chunk INTEGER NOT NULL DEFAULT 0,
    mtime REAL NOT NULL,
    content TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS pages_prod ON pages(product, page);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE chunks_fts USING fts5(
    title,
    content,
    product UNINDEXED,
    page UNINDEXED,
    path UNINDEXED,
    chunk UNINDEXED
);
"""


def split_chunks(text: str) -> list[str]:
    if len(text) <= MAX_CHUNK:
        return [text]
    parts = []
    current = []
    size = 0
    for line in text.splitlines(keepends=True):
        if current and re.match(r"^#{1,3}\s", line) and size + len(line) > MAX_CHUNK:
            parts.append("".join(current))
            current = []
            size = 0
        current.append(line)
        size += len(line)
    parts.append("".join(current))
    return parts


def page_title(content: str) -> str:
    m = re.search(r"^#{1,4}\s+(.+)$", content, re.MULTILINE)
    if not m:
        return ""
    title = m.group(1).strip()
    title = re.sub(r"\s+#+$", "", title)
    return title


def iter_pages():
    if not os.path.isdir(CACHE_DIR):
        return
    for product in sorted(os.listdir(CACHE_DIR)):
        parsed = os.path.join(CACHE_DIR, product, "parsed")
        if not os.path.isdir(parsed):
            continue
        for name in sorted(os.listdir(parsed)):
            if not name.endswith(".htm.md"):
                continue
            path = os.path.join(parsed, name)
            yield product, name, path


def rebuild_fts(db):
    db.execute("DROP TABLE IF EXISTS chunks_fts")
    db.executescript(FTS_SCHEMA)
    rows = db.execute(
        "SELECT id, product, page, title, path, chunk, content FROM pages"
    ).fetchall()
    db.executemany(
        "INSERT INTO chunks_fts(rowid, title, content, product, page, path, chunk) "
        "VALUES (?,?,?,?,?,?,?)",
        [(r[0], r[3], r[6], r[1], r[2], r[4], r[5]) for r in rows],
    )


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Построение/обновление FTS5-индекса")
    ap.add_argument("--rebuild", action="store_true",
                    help="Принудительная полная переиндексация всех страниц")
    args = ap.parse_args()

    os.makedirs(CACHE_DIR, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    db.commit()

    if args.rebuild:
        db.execute("DELETE FROM pages")
        db.commit()

    known = {
        (p, page): mtime
        for p, page, mtime in db.execute(
            "SELECT product, page, mtime FROM pages"
        ).fetchall()
    }

    changed = 0
    removed = 0
    current = set()

    for product, page, path in iter_pages():
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        key = (product, page)
        current.add(key)
        if known.get(key) == mtime:
            continue
        with open(path, encoding="utf-8") as f:
            content = f.read()
        title = page_title(content)
        chunks = split_chunks(content)
        db.execute("DELETE FROM pages WHERE product=? AND page=?", key)
        db.executemany(
            "INSERT INTO pages(product, page, title, path, chunk, mtime, content) "
            "VALUES (?,?,?,?,?,?,?)",
            [(product, page, title, path, i, mtime, ch)
             for i, ch in enumerate(chunks)],
        )
        changed += 1

    for key in known:
        if key not in current:
            db.execute("DELETE FROM pages WHERE product=? AND page=?", key)
            removed += 1

    if changed or removed:
        rebuild_fts(db)

    db.commit()

    total = db.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
    db.close()

    if changed or removed:
        print(f"Индекс обновлён: изменено {changed}, удалено {removed}, всего чанков {total}")
    else:
        print("Индекс актуален, переиндексация не требуется")


if __name__ == "__main__":
    main()
