#!/usr/bin/env python3
"""Cross-project unified search — единый поиск из 3 источников.

Ищет параллельно в:
  1. tradesoft-kb (документация — FTS5 + vectors)
  2. ts-b24-knowledge (решения поддержки — FTS5)
  3. ts-b24 (CRM-контекст — FTS5)

Возвращает структурированные ответы с блоками how_to / how_it_works,
упорядоченными по интенту запроса.

Примеры:
  python3 cross_search.py "как настроить выгрузку прайсов"
  python3 cross_search.py "проблема с авторизацией в Диадок" --json
"""
import json
import os
import sqlite3
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
KB_ROOT = os.path.dirname(SCRIPT_DIR)

# Path to other projects (relative to tradesoft-kb)
KNOWLEDGE_ROOT = os.path.join(os.path.dirname(KB_ROOT), "ts-b24-knowledge")
CRM_ROOT = os.path.join(os.path.dirname(KB_ROOT), "ts-b24")

sys.path.insert(0, SCRIPT_DIR)
from intent import detect_intent, detect_product, classify_block, classify_results, Intent

# ---------------------------------------------------------------------------
# Source adapters
# ---------------------------------------------------------------------------

class DocsSource:
    """tradesoft-kb: документация products (FTS5 + vectors)."""

    def __init__(self):
        from search import search_hybrid
        self._search = search_hybrid

    def search(self, query, product=None, limit=5):
        """Возвращает list[dict] с ключами: path, product, title, snippet, score."""
        rows, _ = self._search(query, product, limit=limit)
        results = []
        for row in rows:
            # row = [product, page_file, title, path, snippet, score]
            prod = row[0] if len(row) > 0 else ""
            page = row[1] if len(row) >1 else ""
            title = row[2] if len(row) >2 else ""
            path = row[3] if len(row) >3 else ""
            snippet = row[4] if len(row) >4 else ""
            score = row[5] if len(row) >5 else 0
            results.append({
                "source": "docs",
                "path": f"{prod}__{page}",
                "product": prod,
                "title": title,
                "content": snippet,
                "score": score,
                "url": f"https://docs.tradesoft.ru/{prod}/{page}",
            })
        return results


class SolutionsSource:
    """ts-b24-knowledge: решения поддержки (FTS5)."""

    def __init__(self):
        db_path = os.path.join(KNOWLEDGE_ROOT, "data", "solutions.db")
        if not os.path.exists(db_path):
            self._conn = None
            return
        self._conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=3000")

    def search(self, query, product=None, limit=5):
        if not self._conn:
            return []
        tokens = [t for t in query.split() if t][:5]
        if not tokens:
            return []

        # Use stemmed tokens for better matching
        from intent import stem
        stemmed_tokens = [stem(t) for t in tokens]

        where = []
        args = []
        for tok in stemmed_tokens:
            # Use prefix matching with stemmed token
            pat = f"%{tok}%"
            where.append("(title LIKE ? OR question LIKE ? OR "
                         "COALESCE(resolution,'') LIKE ? OR "
                         "COALESCE(product_display,'') LIKE ?)")
            args += [pat] * 4

        # Filter by canonical product ID (in 'product' column)
        if product:
            where.append("product = ?")
            args.append(product)

        where_sql = " AND ".join(where)
        rows = self._conn.execute(
            f"SELECT id, title, question, resolution, product_display, "
            f"date_create, confidence "
            f"FROM cases WHERE {where_sql} "
            f"ORDER BY confidence DESC LIMIT ?",
            args + [limit]).fetchall()

        results = []
        for r in rows:
            # Берём первые 500 символов из resolution как snippet
            resolution = (r["resolution"] or "")[:500]
            results.append({
                "source": "solution",
                "path": f"solution_{r['id']}",
                "product": r["product_display"] or "",
                "title": r["title"] or "",
                "content": resolution,
                "score": r["confidence"] or 0,
                "url": f"https://ts-b24-knowledge.search/api/solution?id={r['id']}",
                "deal_id": r["id"],
                "date": r["date_create"] or "",
            })
        return results


class CRMSource:
    """ts-b24: CRM сделки и контекст (FTS5)."""

    def __init__(self):
        db_path = os.path.join(CRM_ROOT, "data", "ts_b24.db")
        if not os.path.exists(db_path):
            self._conn = None
            return
        self._conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=3000")

    def search(self, query, product=None, limit=3):
        if not self._conn:
            return []
        q = query.strip()
        if not q:
            return []

        rows = self._conn.execute(
            "SELECT d.ID, d.TITLE, d.COMPANY_ID, c.TITLE AS company_title, "
            "d.CATEGORY_ID, d.STAGE_ID, s.NAME AS stage_name, "
            "d.DATE_CREATE "
            "FROM deals d "
            "LEFT JOIN deal_stages s ON s.STATUS_ID = d.STAGE_ID "
            "LEFT JOIN companies c ON c.ID = d.COMPANY_ID "
            "WHERE (d.TITLE LIKE ? OR COALESCE(d.COMMENTS,'') LIKE ? OR "
            "COALESCE(c.TITLE,'') LIKE ?) "
            "ORDER BY d.DATE_CREATE DESC LIMIT ?",
            [f"%{q}%"] *3 + [limit]).fetchall()

        results = []
        for r in rows:
            results.append({
                "source": "crm",
                "path": f"deal_{r['ID']}",
                "product": "",
                "title": r["TITLE"] or "",
                "content": (r["company_title"] or "") + " — " + (r["stage_name"] or ""),
                "score": 0.5,
                "url": f"https://b24.tradesoft.ru/crm/deal/details/{r['ID']}/",
                "deal_id": r["ID"],
                "company": r["company_title"] or "",
                "stage": r["stage_name"] or "",
                "date": r["DATE_CREATE"] or "",
            })
        return results


# ---------------------------------------------------------------------------
# Cross-links source
# ---------------------------------------------------------------------------

class CrossLinksSource:
    """Cross-links между решениями и документацией."""

    def __init__(self):
        db_path = os.path.join(KB_ROOT, "cache", "cross_links.db")
        if not os.path.exists(db_path):
            self._conn = None
            return
        self._conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        self._conn.row_factory = sqlite3.Row

    def get_related_docs(self, solution_id, limit=3):
        """Для решения возвращает связанные страницы документации."""
        if not self._conn:
            return []
        rows = self._conn.execute("""
            SELECT doc_path, doc_product, doc_title, bm25_score
            FROM solution_to_doc
            WHERE solution_id = ?
            ORDER BY bm25_score DESC
            LIMIT ?
        """, (str(solution_id), limit)).fetchall()
        return [dict(r) for r in rows]

    def get_related_solutions(self, doc_path, limit=3):
        """Для страницы документации возвращает связанные решения."""
        if not self._conn:
            return []
        rows = self._conn.execute("""
            SELECT solution_id, solution_title, solution_product, bm25_score
            FROM doc_to_solution
            WHERE doc_path = ?
            ORDER BY bm25_score DESC
            LIMIT ?
        """, (doc_path, limit)).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Cross-source merge + compose
# ---------------------------------------------------------------------------

# Map solution product names to canonical IDs
_SOLUTION_PRODUCT_MAP = {
    "Parts.Resource": "parts_resource",
    "Parts.Intellect": "parts_intellect",
    "AutoИнтеллект": "parts_intellect",  # AutoIntellect = Parts.Intellect
    "Синхронизатор": "sync",
    "ВАР": "var",
    "Другое": "other",
}


def _normalize_product(source_name, product_name):
    """Нормализует название продукта к canonical ID."""
    if not product_name:
        return None
    # Already canonical
    if product_name in ("parts_resource", "parts_intellect", "sync",
                        "diadok", "seo", "delivery", "wazzup", "tsd",
                        "marketplace", "var", "other"):
        return product_name
    # Solutions format
    if product_name in _SOLUTION_PRODUCT_MAP:
        return _SOLUTION_PRODUCT_MAP[product_name]
    # Docs format (product prefix)
    for prefix in ("parts-resource-guide", "parts-intellect-guide",
                   "parts-intellect-synch", "diadok", "seo-guide",
                   "delivery_schedule", "wazzup", "tsd", "marketplace"):
        if product_name.startswith(prefix):
            return prefix.replace("-", "_")
    return product_name


def merge_results(docs, solutions, crm, max_per_source=5):
    """Объединяет результаты из 3 источников в единый список."""
    all_results = []

    for r in docs[:max_per_source]:
        r["product_canonical"] = _normalize_product("docs", r.get("product"))
        all_results.append(r)

    for r in solutions[:max_per_source]:
        r["product_canonical"] = _normalize_product("solution", r.get("product"))
        all_results.append(r)

    for r in crm[:3]:
        r["product_canonical"] = None
        all_results.append(r)

    return all_results


def compose_answers(results, intent, max_answers=2, cross_links=None):
    """Собирает 1-2 структурированных ответа из результатов.

    Каждый ответ содержит блоки how_to и how_it_works, упорядоченные по интенту.
    Если доступны cross-links, добавляет related_docs и related_solutions.
    """
    # Классифицируем блоки
    for r in results:
        r["block_type"] = classify_block(r.get("source", ""), r.get("content", ""))

    # Группируем по product_canonical
    by_product = {}
    for r in results:
        prod = r.get("product_canonical") or "general"
        by_product.setdefault(prod, []).append(r)

    primary, secondary = Intent.ORDER.get(intent, Intent.ORDER[Intent.GENERAL])

    answers = []
    for prod, prod_results in sorted(by_product.items(),
                                      key=lambda x: -max(r.get("score", 0) for r in x[1])):
        # Для каждого продукта: берём лучший how_to и лучший how_it_works
        how_to = [r for r in prod_results if r["block_type"] == "how_to"]
        how_works = [r for r in prod_results if r["block_type"] == "how_it_works"]

        blocks = []
        # Упорядочиваем по интенту
        first_list = how_to if primary == "how_to" else how_works
        second_list = how_works if primary == "how_to" else how_to

        if first_list:
            blocks.append(first_list[0])
        if second_list:
            blocks.append(second_list[0])

        if not blocks and prod_results:
            blocks.append(prod_results[0])

        if blocks:
            answer = {
                "title": blocks[0].get("title", ""),
                "product": prod if prod != "general" else None,
                "blocks": blocks,
                "images": [],
                "related_deals": [],
                "related_docs": [],
                "related_solutions": [],
            }
            # Собираем связанные сделки
            for b in blocks:
                if b.get("deal_id"):
                    answer["related_deals"].append(b["deal_id"])
                if b.get("url"):
                    answer.setdefault("source_urls", []).append(b["url"])

            # Добавляем cross-links
            if cross_links:
                for b in blocks:
                    if b["source"] == "solution" and b.get("deal_id"):
                        # Для решения — связанные страницы документации
                        rel_docs = cross_links.get_related_docs(b["deal_id"], limit=2)
                        for rd in rel_docs:
                            answer["related_docs"].append({
                                "path": rd["doc_path"],
                                "title": rd["doc_title"],
                                "product": rd["doc_product"],
                                "score": rd["bm25_score"],
                            })
                    elif b["source"] == "docs" and b.get("path"):
                        # Для документации — связанные решения
                        rel_sols = cross_links.get_related_solutions(b["path"], limit=2)
                        for rs in rel_sols:
                            answer["related_solutions"].append({
                                "id": rs["solution_id"],
                                "title": rs["solution_title"],
                                "product": rs["solution_product"],
                                "score": rs["bm25_score"],
                            })

            answers.append(answer)

        if len(answers) >= max_answers:
            break

    return answers


# ---------------------------------------------------------------------------
# Main search function
# ---------------------------------------------------------------------------

def cross_search(query, max_answers=2, limit_per_source=5):
    """Единый поиск из 3 источников.

    Возвращает dict с ключами:
      - answers: list[Answer] — структурированные ответы
      - intent: str — определённый интент
      - product: str|None — определённый продукт
      - latency_ms: int — время выполнения
    """
    t0 = time.time()

    # 1. Intent + Product detection
    intent = detect_intent(query)
    product = detect_product(query)

    # 2. Parallel retrieval from all sources
    doc_results = []
    sol_results = []
    crm_results = []

    # Map canonical product ID to docs product name
    docs_product = None
    if product:
        DOCS_PRODUCT_MAP = {
            "parts_resource": "parts-resource-guide",
            "parts_intellect": "parts-intellect-guide",
            "sync": "parts-intellect-synch",
            "diadok": "diadok",
            "seo": "seo-guide",
            "delivery": "delivery_schedule",
            "wazzup": "wazzup",
            "tsd": "tsd",
            "marketplace": "marketplace",
        }
        docs_product = DOCS_PRODUCT_MAP.get(product)

    # Map canonical product ID to solutions product name (same canonical IDs)
    sol_product = product  # solutions DB uses same canonical IDs

    try:
        ds = DocsSource()
        doc_results = ds.search(query, docs_product, limit=limit_per_source)
    except Exception as e:
        print(f"[docs] error: {e}", file=sys.stderr)

    try:
        sol = SolutionsSource()
        sol_results = sol.search(query, sol_product, limit=limit_per_source)
    except Exception as e:
        print(f"[solutions] error: {e}", file=sys.stderr)

    try:
        crm = CRMSource()
        crm_results = crm.search(query, product, limit=3)
    except Exception as e:
        print(f"[crm] error: {e}", file=sys.stderr)

    # 3. Merge + Compose with cross-links
    merged = merge_results(doc_results, sol_results, crm_results)

    # Initialize cross-links source
    cross_links = None
    try:
        cross_links = CrossLinksSource()
    except Exception as e:
        print(f"[cross-links] init error: {e}", file=sys.stderr)

    answers = compose_answers(merged, intent, max_answers=max_answers,
                              cross_links=cross_links)

    latency_ms = int((time.time() - t0) * 1000)

    return {
        "answers": answers,
        "intent": intent,
        "product": product,
        "latency_ms": latency_ms,
        "counts": {
            "docs": len(doc_results),
            "solutions": len(sol_results),
            "crm": len(crm_results),
        },
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Единый поиск из 3 источников")
    parser.add_argument("query", help="Поисковый запрос")
    parser.add_argument("--json", action="store_true", help="Вывод в JSON")
    parser.add_argument("--max", type=int, default=2, help="Макс. ответов")
    args = parser.parse_args()

    result = cross_search(args.query, max_answers=args.max)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Запрос:  {args.query}")
        print(f"Интент:  {result['intent']}")
        print(f"Продукт: {result['product'] or '(не определён)'}")
        print(f"Источники: docs={result['counts']['docs']}, "
              f"solutions={result['counts']['solutions']}, "
              f"crm={result['counts']['crm']}")
        print(f"Латентность: {result['latency_ms']}ms")
        print()

        for i, answer in enumerate(result["answers"], 1):
            print(f"=== Ответ {i}: {answer['title'][:60]} ===")
            print(f"    Продукт: {answer['product'] or '-'}")
            for j, block in enumerate(answer["blocks"], 1):
                src = block["source"]
                btype = block["block_type"]
                content = block["content"][:200].replace("\n", " ")
                print(f"  Блок {j} [{btype}] ({src}):")
                print(f"    {content}...")
            if answer.get("related_deals"):
                print(f"  Связанные сделки: {answer['related_deals']}")
            print()
