#!/usr/bin/env python3
"""Строит/обновляет SQLite FTS5-индекс по страницам БД.

Индекс: <KB_ROOT>/cache/kb_index.db
Инкрементальность: переиндексируются только страницы с изменённым mtime.
Крупные страницы разбиваются на секции по заголовкам (## / ###).
"""
import os
import pickle
import re
import sqlite3
import time
from multiprocessing import Pool

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
KB_ROOT = os.path.dirname(SCRIPT_DIR)
CACHE_DIR = os.path.join(KB_ROOT, "cache")
DB_PATH = os.path.join(CACHE_DIR, "kb_index.db")
PAGE_STEMS_PATH = os.path.join(CACHE_DIR, "page_stems.pkl")
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
CREATE VIRTUAL TABLE stems_fts USING fts5(
    title_stem,
    content_stem,
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
    """Полная переиндексация FTS-таблиц (chunks_fts + stems_fts), rowid = pages.id.

    stems_fts хранит морфологические стемы каждого токена (через stem.stem_text):
    «новый/нового/новые» → один стем «новый», а глагольная пара «настроить/
    настройки» матчится по префиксу корня «настро-*». Сырые тексты остаются в
    pages/chunks_fts для сниппетов и bm25-дисплея.
    """
    from stem import stem_text

    def stems_of(text: str) -> str:
        return " ".join(stem_text(text or ""))

    db.execute("DROP TABLE IF EXISTS chunks_fts")
    db.execute("DROP TABLE IF EXISTS stems_fts")
    db.executescript(FTS_SCHEMA)
    rows = db.execute(
        "SELECT id, product, page, title, path, chunk, content FROM pages"
    ).fetchall()
    db.executemany(
        "INSERT INTO chunks_fts(rowid, title, content, product, page, path, chunk) "
        "VALUES (?,?,?,?,?,?,?)",
        [(r[0], r[3], r[6], r[1], r[2], r[4], r[5]) for r in rows],
    )
    db.executemany(
        "INSERT INTO stems_fts(rowid, title_stem, content_stem, product, page, path, chunk) "
        "VALUES (?,?,?,?,?,?,?)",
        [(r[0], stems_of(r[3]), stems_of(r[6]), r[1], r[2], r[4], r[5]) for r in rows],
    )


def _prime_page(args):
    """Стемы страницы и абзацев в формате search.py (для мультипроцессного
    префилда). Используем константы/токенизатор search.py, чтобы зеркально
    совпадать с runtime-вычислением (сниппеты, дискриминаторы)."""
    import search as s
    from stem import stem_text, stem_word
    key, text = args
    stems = [p for p in stem_text(text) if p]
    paragraphs = [ln.strip() for ln in text.splitlines() if ln.strip()][:s._SNIPPET_PARAS_LIMIT]
    paras = []
    for para in paragraphs:
        words = [stem_word(w.lower()) for w in s.TOKEN_RE.findall(para)]
        paras.append([para, [w for w in words if w]])
    return key, stems, paras


def prime_page_stems(db_path, workers=6):
    """Полный предрасчёт page_stems.pkl (формат search.py) по содержимому индекса.

    Поисковый сервер лениво достраивает стемы страниц при запросах; префилд
    делает холодный старт мгновенным для всех страниц сразу. Пикл привязан
    к mtime индекса — сервер использует его, только если индекс не менялся.
    Стоимость полного прогона ~0.15 с/страница; распараллеливается по процессам.
    """
    ts = time.time()
    if os.path.exists(PAGE_STEMS_PATH):
        os.remove(PAGE_STEMS_PATH)
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT product, page, content FROM pages ORDER BY product, page, chunk"
    ).fetchall()
    con.close()
    joined = {}
    for product, page, content in rows:
        joined.setdefault((product, page), []).append(content or "")
    items = [(k, "\n".join(v)) for k, v in joined.items()]
    stemmed, paraed = {}, {}
    if workers > 1 and len(items) > 128:
        with Pool(workers) as pool:
            for key, stems, paras in pool.imap_unordered(_prime_page, items, chunksize=8):
                stemmed[key] = stems
                paraed[key] = paras
    else:
        for key, stems, paras in (_prime_page(it) for it in items):
            stemmed[key] = stems
            paraed[key] = paras
    payload = {"_mtime": os.path.getmtime(db_path), "stems": stemmed, "paras": paraed}
    with open(PAGE_STEMS_PATH + ".tmp", "wb") as f:
        pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(PAGE_STEMS_PATH + ".tmp", PAGE_STEMS_PATH)
    print(f"page_stems.pkl: {len(stemmed)} страниц за {round(time.time() - ts, 1)} с")


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Построение/обновление FTS5-индекса")
    ap.add_argument("--rebuild", action="store_true",
                    help="Принудительная полная переиндексация всех страниц")
    ap.add_argument("--no-prime-stems", action="store_true",
                    help="Не предрасчитывать page_stems.pkl после пересборки")
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
        # данные могут лежать в -wal; сервер читает через WAL, поэтому для инвалидации
        # его кэшей достаточно сменить mtime основного файла индекса.
        try:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            db.commit()
        except sqlite3.OperationalError:
            pass  # другой процесс держит БД — и так сойдёт, см. utime ниже

    db.commit()

    total = db.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
    db.close()

    if changed or removed:
        # сигнал пересборки для поискового сервера (сброс/пересоздание кэшей)
        try:
            os.utime(DB_PATH, None)
        except OSError:
            pass
        # фиксируем состояние стемминг-слоя для freshness.py
        try:
            import freshness
            freshness.write_meta(freshness.build_fingerprint(),
                                 os.path.getmtime(DB_PATH))
        except Exception as e:
            print(f"[warn] не удалось записать build_meta.json: {e}")
        print(f"Индекс обновлён: изменено {changed}, удалено {removed}, всего чанков {total}")
        if args.no_prime_stems:
            print("Префилд стемов пропущен (--no-prime-stems)")
        else:
            prime_page_stems(DB_PATH)
    else:
        print("Индекс актуален, переиндексация не требуется")


if __name__ == "__main__":
    main()
