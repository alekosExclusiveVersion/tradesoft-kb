#!/usr/bin/env python3
"""Cross-linking: привязка решений ts-b24-knowledge к документации tradesoft-kb.

Создаёт SQLite базу cross_links.db с таблицами:
  - solution_to_doc: решение → релевантные страницы документации
  - doc_to_solution: страница документации → релевантные решения

Алгоритм:
  1. Для каждого решения: извлекаем ключевые слова, ищем в docs FTS5
  2. Для каждой страницы docs: извлекаем ключевые слова, ищем в solutions FTS5
  3. Сохраняем top-N связей с BM25 scores

Использование:
  python3 cross_link.py --build
  python3 cross_link.py --query "выгрузка прайсов" --limit 5
"""
import argparse
import json
import os
import sqlite3
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
KB_ROOT = os.path.dirname(SCRIPT_DIR)

# Paths
KNOWLEDGE_ROOT = os.path.join(os.path.dirname(KB_ROOT), "ts-b24-knowledge")
CROSS_LINK_DB = os.path.join(KB_ROOT, "cache", "cross_links.db")
DOCS_DB = os.path.join(KB_ROOT, "cache", "kb_index.db")
SOLUTIONS_DB = os.path.join(KNOWLEDGE_ROOT, "data", "solutions.db")

# Limits
MAX_LINKS_PER_SOLUTION = 5
MAX_LINKS_PER_DOC = 5
MIN_BM25_SCORE = -10  # minimum BM25 score to keep a link


def get_docs_conn():
    """Подключение к docs FTS5 индексу."""
    conn = sqlite3.connect(f"file:{DOCS_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def get_solutions_conn():
    """Подключение к solutions БД."""
    conn = sqlite3.connect(f"file:{SOLUTIONS_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def get_cross_link_conn():
    """Подключение/создание cross_links.db."""
    os.makedirs(os.path.dirname(CROSS_LINK_DB), exist_ok=True)
    conn = sqlite3.connect(CROSS_LINK_DB)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(conn):
    """Создаёт таблицы cross_links.db."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS solution_to_doc (
            solution_id TEXT NOT NULL,
            doc_path TEXT NOT NULL,
            doc_product TEXT,
            doc_title TEXT,
            bm25_score REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (solution_id, doc_path)
        );

        CREATE TABLE IF NOT EXISTS doc_to_solution (
            doc_path TEXT NOT NULL,
            solution_id TEXT NOT NULL,
            solution_title TEXT,
            solution_product TEXT,
            bm25_score REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (doc_path, solution_id)
        );

        CREATE INDEX IF NOT EXISTS idx_s2d_solution ON solution_to_doc(solution_id);
        CREATE INDEX IF NOT EXISTS idx_s2d_doc ON solution_to_doc(doc_path);
        CREATE INDEX IF NOT EXISTS idx_d2s_doc ON doc_to_solution(doc_path);
        CREATE INDEX IF NOT EXISTS idx_d2s_solution ON doc_to_solution(solution_id);

        CREATE TABLE IF NOT EXISTS build_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );
    """)


def build_solution_to_doc(sol_conn, docs_conn, cross_conn):
    """Для каждого решения ищет релевантные страницы документации."""
    print("[1/2] Строим solution → doc links...")

    # Получаем решения с текстом
    rows = sol_conn.execute("""
        SELECT id, title, question, resolution, product
        FROM cases
        WHERE title IS NOT NULL AND title != ''
    """).fetchall()

    print(f"  Решений для обработки: {len(rows)}")

    inserted = 0
    batch = []
    t0 = time.time()

    for i, row in enumerate(rows):
        solution_id = row["id"]
        title = row["title"] or ""
        question = row["question"] or ""
        resolution = row["resolution"] or ""

        # Формируем поисковый запрос из решения
        # Берём первые 200 символов из каждого поля
        text = f"{title} {question[:200]} {resolution[:200]}"
        tokens = [t for t in text.split() if len(t) > 2][:10]

        if not tokens:
            continue

        # Ищем в docs FTS5
        try:
            # Используем chunks_fts для поиска (содержит полный текст)
            match_query = " OR ".join(tokens[:5])
            doc_rows = docs_conn.execute("""
                SELECT path, product, snippet(chunks_fts, 1, '', '', '...', 32) as snippet,
                       rank
                FROM chunks_fts
                WHERE chunks_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            """, (match_query, MAX_LINKS_PER_SOLUTION)).fetchall()

            for doc_row in doc_rows:
                if doc_row["rank"] < MIN_BM25_SCORE:
                    continue
                # Normalize path to "product__page.htm.md" format
                full_path = doc_row["path"]
                # Extract product and page from full path
                # e.g., "/Users/.../cache/parts-resource-guide/parsed/page.htm.md"
                # → "parts-resource-guide__page.htm.md"
                import re
                match = re.search(r'/cache/([^/]+)/parsed/([^/]+\.htm\.md)$', full_path)
                if match:
                    norm_path = f"{match.group(1)}__{match.group(2)}"
                else:
                    norm_path = full_path
                batch.append((
                    solution_id,
                    norm_path,
                    doc_row["product"],
                    doc_row["snippet"][:100],
                    doc_row["rank"],
                ))
                inserted += 1

        except Exception as e:
            # FTS5 match может упасть на невалидном запросе
            pass

        if (i + 1) % 500 == 0:
            elapsed = time.time() - t0
            rate = (i + 1) / elapsed
            print(f"  {i+1}/{len(rows)} ({rate:.0f}/s), links: {inserted}")

    # Batch insert
    if batch:
        cross_conn.executemany("""
            INSERT OR REPLACE INTO solution_to_doc
            (solution_id, doc_path, doc_product, doc_title, bm25_score)
            VALUES (?, ?, ?, ?, ?)
        """, batch)
        cross_conn.commit()

    elapsed = time.time() - t0
    print(f"  Готово: {inserted} links за {elapsed:.1f}s")
    return inserted


def build_doc_to_solution(sol_conn, docs_conn, cross_conn):
    """Для каждой страницы документации ищет релевантные решения."""
    print("[2/2] Строим doc → solution links...")

    # Получаем страницы документации
    doc_rows = docs_conn.execute("""
        SELECT path, product, content
        FROM pages
        WHERE content IS NOT NULL AND content != ''
        LIMIT 2000
    """).fetchall()

    print(f"  Страниц docs для обработки: {len(doc_rows)}")

    inserted = 0
    batch = []
    t0 = time.time()

    for i, row in enumerate(doc_rows):
        doc_path = row["path"]
        content = row["content"] or ""

        # Формируем поисковый запрос из контента
        tokens = [t for t in content.split() if len(t) > 3][:15]

        if not tokens:
            continue

        # Ищем в solutions
        try:
            match_query = " OR ".join(tokens[:5])
            sol_rows = sol_conn.execute("""
                SELECT id, title, product, confidence
                FROM cases
                WHERE title LIKE ? OR question LIKE ? OR
                      COALESCE(resolution,'') LIKE ?
                ORDER BY confidence DESC
                LIMIT ?
            """, (f"%{tokens[0]}%", f"%{tokens[0]}%", f"%{tokens[0]}%",
                  MAX_LINKS_PER_DOC)).fetchall()

            for sol_row in sol_rows:
                # Простой score на основе confidence решения
                score = sol_row["confidence"] or 0.5
                batch.append((
                    doc_path,
                    str(sol_row["id"]),
                    sol_row["title"] or "",
                    sol_row["product"] or "",
                    score,
                ))
                inserted += 1

        except Exception as e:
            pass

        if (i + 1) % 200 == 0:
            elapsed = time.time() - t0
            rate = (i + 1) / elapsed
            print(f"  {i+1}/{len(doc_rows)} ({rate:.0f}/s), links: {inserted}")

    # Batch insert
    if batch:
        cross_conn.executemany("""
            INSERT OR REPLACE INTO doc_to_solution
            (doc_path, solution_id, solution_title, solution_product, bm25_score)
            VALUES (?, ?, ?, ?, ?)
        """, batch)
        cross_conn.commit()

    elapsed = time.time() - t0
    print(f"  Готово: {inserted} links за {elapsed:.1f}s")
    return inserted


def build_all():
    """Полная пересборка cross-links."""
    t0 = time.time()

    sol_conn = get_solutions_conn()
    docs_conn = get_docs_conn()
    cross_conn = get_cross_link_conn()

    init_db(cross_conn)

    # Очищаем старые данные
    cross_conn.execute("DELETE FROM solution_to_doc")
    cross_conn.execute("DELETE FROM doc_to_solution")
    cross_conn.commit()

    n_s2d = build_solution_to_doc(sol_conn, docs_conn, cross_conn)
    n_d2s = build_doc_to_solution(sol_conn, docs_conn, cross_conn)

    # Сохраняем мета-данные сборки
    cross_conn.execute("""
        INSERT OR REPLACE INTO build_meta (key, value)
        VALUES (?, ?)
    """, ("built_at", time.strftime("%Y-%m-%dT%H:%M:%S")))
    cross_conn.execute("""
        INSERT OR REPLACE INTO build_meta (key, value)
        VALUES (?, ?)
    """, ("solution_to_doc_count", str(n_s2d)))
    cross_conn.execute("""
        INSERT OR REPLACE INTO build_meta (key, value)
        VALUES (?, ?)
    """, ("doc_to_solution_count", str(n_d2s)))
    cross_conn.commit()

    sol_conn.close()
    docs_conn.close()
    cross_conn.close()

    elapsed = time.time() - t0
    print(f"\nCross-links построены за {elapsed:.1f}s:")
    print(f"  solution → doc: {n_s2d}")
    print(f"  doc → solution: {n_d2s}")
    print(f"  DB: {CROSS_LINK_DB}")


def query_links(query, limit=5):
    """Ищет cross-links по запросу."""
    if not os.path.exists(CROSS_LINK_DB):
        print("Cross-links не построены. Запустите: python3 cross_link.py --build")
        return

    conn = get_cross_link_conn()

    # Ищем по solution_to_doc
    rows = conn.execute("""
        SELECT s.solution_id, s.doc_path, s.doc_product, s.doc_title, s.bm25_score,
               c.title as sol_title, c.product as sol_product
        FROM solution_to_doc s
        JOIN cases c ON c.id = s.solution_id
        WHERE s.doc_title LIKE ? OR s.doc_path LIKE ? OR c.title LIKE ?
        ORDER BY s.bm25_score DESC
        LIMIT ?
    """, (f"%{query}%", f"%{query}%", f"%{query}%", limit)).fetchall()

    print(f"Cross-links для '{query}':")
    for r in rows:
        print(f"  [{r['sol_title'][:40]}] → [{r['doc_path'][:50]}] (score={r['bm25_score']:.2f})")

    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cross-linking решений и документации")
    parser.add_argument("--build", action="store_true", help="Построить cross-links")
    parser.add_argument("--query", help="Поиск по cross-links")
    parser.add_argument("--limit", type=int, default=5, help="Лимит результатов")
    args = parser.parse_args()

    if args.build:
        build_all()
    elif args.query:
        query_links(args.query, args.limit)
    else:
        parser.print_help()
