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
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
KB_ROOT = os.path.dirname(SCRIPT_DIR)

# Path to other projects (relative to tradesoft-kb)
KNOWLEDGE_ROOT = os.path.join(os.path.dirname(KB_ROOT), "ts-b24-knowledge")
CRM_ROOT = os.path.join(os.path.dirname(KB_ROOT), "ts-b24")

sys.path.insert(0, SCRIPT_DIR)
from intent import detect_intent, detect_product, classify_block, classify_results, Intent

# ---------------------------------------------------------------------------
# Source adapters (lazy singletons)
# ---------------------------------------------------------------------------

_docs_source = None
_solutions_source = None
_crm_source = None
_cross_links_source = None


def _get_docs_source():
    global _docs_source
    if _docs_source is None:
        _docs_source = DocsSource()
    return _docs_source


def _get_solutions_source():
    global _solutions_source
    if _solutions_source is None:
        _solutions_source = SolutionsSource()
    return _solutions_source


def _get_crm_source():
    global _crm_source
    if _crm_source is None:
        _crm_source = CRMSource()
    return _crm_source


def _get_cross_links_source():
    global _cross_links_source
    if _cross_links_source is None:
        _cross_links_source = CrossLinksSource()
    return _cross_links_source


def _build_doc_url(product, page):
    """Абсолютный URL страницы на product-doc.tradesoft.ru.

    docs-продукт -> (раздел, подраздел) карты сайта product-doc. Имена страниц
    Храним с суффиксом .htm.md, на сайте они публикуются как .htm.
    """
    base, sub = PRODUCT_DOC_SECTIONS.get(product, (product, product))
    stem = page[:-3] if page.endswith(".md") else page
    return f"https://product-doc.tradesoft.ru/{base}/{sub}/{stem}"


# product-doc.tradesoft.ru: соответствие docs-продукт -> (раздел, подраздел).
# Собрано с заскрейпленных .md.imgs (реальные URL картинок тех же статей).
PRODUCT_DOC_SECTIONS = {
    "parts-intellect-guide": ("ai", "ai"),
    "parts-intellect-synch": ("ai", "synch"),
    "parts-intellect-changes": ("ai", "changes"),
    "parts-index-rest-api": ("ai", "rest_api"),
    "delivery_schedule": ("ai", "delivery_schedule"),
    "diadok": ("ai", "diadok"),
    "marketplace": ("ai", "marketplace"),
    "tsd": ("ai", "tsd"),
    "wazzup": ("ai", "other"),
    "parts-resource-guide": ("ar", "ar"),
    "parts-resource-changes": ("ar", "changes"),
    "parts-resource-rest-api": ("ar", "rest_api"),
    "seo-guide": ("ar", "online_guides"),
}


def _build_excerpt(product, snippet, full_text, max_len=400):
    """Осмысленный эксцерпт для отображения.

    Если snippet — цельный текст, оставляем его. Иначе строим из полного
    контента: для changelog-страниц берём несколько первых пунктов списка,
    для остальных — тело страницы (без заголовков и картинок).
    """
    if not full_text:
        return (snippet or "")[:max_len]
    if len(full_text) <= max_len + 60:
        return full_text.strip()

    lines = []
    for ln in full_text.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("![") or (ln.startswith("<") and ln.endswith(">")):
            continue
        lines.append(ln)

    body = [ln for ln in lines if not ln.startswith("#")]
    if not body:
        return (snippet or "")[:max_len]

    def is_body(snip):
        return len(snip) >= 120 and any(not l.startswith("#") for l in snip.splitlines())

    if product and product.endswith("-changes") and any(
            l.startswith(("- ", "* ")) for l in body):
        start = next(i for i, l in enumerate(body) if l.startswith(("- ", "* ")))
        out = "\n".join(body[start:]).strip()
    elif is_body(snippet or ""):
        return (snippet or "")[:max_len]
    else:
        out = "\n".join(body).strip()

    if len(out) > max_len:
        cut = out.rfind(" ", 0, max_len)
        out = out[:cut if cut > 170 else max_len] + "…"
    return out


class DocsSource:
    """tradesoft-kb: документация products (FTS5 + vectors)."""

    def __init__(self, hybrid_timeout_ms=5000):
        import threading
        from search import search_hybrid
        self._search = search_hybrid
        self._threading = threading
        self._hybrid_timeout_ms = hybrid_timeout_ms

    def search(self, query, product=None, limit=5, _skip_auto_product=False):
        """Возвращает list[dict] с ключами: path, product, title, snippet, score.

        Использует гибридный поиск с таймаутом: если semantic search не
        завершился за hybrid_timeout_ms, возвращает результаты FTS-only.
        """
        result_holder = [[]]

        def _do_hybrid():
            try:
                rows, _ = self._search(query, product, limit=limit,
                                       _skip_auto_product=_skip_auto_product)
                result_holder[0] = rows
            except Exception:
                pass

        t = self._threading.Thread(target=_do_hybrid, daemon=True)
        t.start()
        t.join(timeout=self._hybrid_timeout_ms / 1000.0)

        # If hybrid timed out, use FTS-only fallback
        if not result_holder[0]:
            try:
                from search import terms, search
                fts_terms = terms(query)
                rows, _ = search(fts_terms, product, limit=limit)
                result_holder[0] = rows
            except Exception:
                pass

        results = []
        pending = []
        for row in result_holder[0]:
            # row = [product, page_file, title, path, snippet, score]
            prod = row[0] if len(row) > 0 else ""
            page = row[1] if len(row) > 1 else ""
            title = row[2] if len(row) > 2 else ""
            path = row[3] if len(row) > 3 else ""
            snippet = row[4] if len(row) > 4 else ""
            score = row[5] if len(row) > 5 else 0
            entry = {
                "source": "docs",
                "path": f"{prod}__{page}",
                "product": prod,
                "page": page,
                "title": title,
                "content": snippet or "",
                "content_full": "",
                "score": score,
                "url": _build_doc_url(prod, page),
            }
            results.append(entry)
            pending.append(entry)

        # Подтягиваем полный контент из кэша для всех найденных страниц
        # (для первичного блока ответа нужна полная инструкция). Одним запросом
        # пачками (SQLite лимит параметров ~999), а не N одиночных SELECT'ов.
        if pending:
            try:
                con = sqlite3.connect(f"file:{KB_ROOT}/cache/kb_index.db?mode=ro", uri=True)
                try:
                    # (product, page) -> все чанки страницы по порядку
                    full_by_key = {}
                    pairs = [(p["product"], p["page"]) for p in pending]
                    for i in range(0, len(pairs), 64):
                        batch = pairs[i:i + 64]
                        ph = ",".join("(?,?)" for _ in batch)
                        cursor = con.execute(
                            f"SELECT product, page, content FROM pages "
                            f"WHERE (product, page) IN ({ph}) ORDER BY chunk",
                            [v for pair in batch for v in pair])
                        for r in cursor:
                            full_by_key.setdefault((r[0], r[1]), []).append(r[2] or "")
                    for entry in pending:
                        parts = full_by_key.get((entry["product"], entry["page"]))
                        if not parts:
                            continue
                        full = "\n".join(parts)
                        entry["content_full"] = full
                        if len(entry["content"]) < 120:
                            entry["content"] = _build_excerpt(entry["product"],
                                                              entry["content"], full)
                finally:
                    con.close()
            except Exception as e:
                print(f"[docs] подтягивание контента: {e}", file=sys.stderr)
        return results


class SolutionsSource:
    """ts-b24-knowledge: решения поддержки (FTS5)."""

    def __init__(self):
        import threading
        self._lock = threading.RLock()
        db_path = os.path.join(KNOWLEDGE_ROOT, "data", "solutions.db")
        if not os.path.exists(db_path):
            self._conn = None
            return
        self._conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True,
                                     check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=3000")

    def search(self, query, product=None, limit=5):
        if not self._conn:
            return []
        with self._lock:
            return self._search_unlocked(query, product, limit)

    def get_solution(self, solution_id):
        """Полный текст случая по id (для глубинного просмотра)."""
        if not self._conn:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT id, title, question, resolution, product_display, "
                "date_create, confidence FROM cases WHERE id = ?",
                (solution_id,)).fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "title": row["title"] or "",
            "question": row["question"] or "",
            "resolution": row["resolution"] or "",
            "product": row["product_display"] or "",
            "date": row["date_create"] or "",
        }

    def _search_unlocked(self, query, product=None, limit=5):
        tokens = [t for t in query.split() if t][:5]
        if not tokens:
            return []

        # Use stemmed tokens for better matching
        from intent import stem
        stemmed_tokens = [stem(t) for t in tokens]
        n_tok = len(stemmed_tokens)
        max_score = 3.0 * n_tok  # title ×3 per token

        # Score-based matching: title > question > resolution
        where = []
        args = []
        score_parts = []
        for tok in stemmed_tokens:
            pat = f"%{tok}%"
            # Title match: weight 3
            # Question match: weight 2
            # Resolution match: weight 1
            score_parts.append("(CASE WHEN title LIKE ? THEN 3 WHEN question LIKE ? THEN 2 ELSE 0 END)")
            where.append("(title LIKE ? OR question LIKE ? OR "
                         "COALESCE(resolution,'') LIKE ?)")
            args += [pat, pat, pat, pat, pat]

        # Filter by canonical product ID (in 'product' column)
        # Solutions DB uses: auto_intellect, parts_intellect, parts_resource, sync, var, other
        if product:
            # Map our canonical IDs to solutions DB format
            sol_product_map = {
                "parts_intellect": ["parts_intellect", "auto_intellect"],
                "parts_resource": ["parts_resource"],
                "sync": ["sync"],
                "var": ["var"],
            }
            sol_products = sol_product_map.get(product, [product])
            placeholders = ",".join(["?"] * len(sol_products))
            where.append(f"product IN ({placeholders})")
            args.extend(sol_products)

        where_sql = " AND ".join(where)
        score_sql = " + ".join(score_parts)
        rows = self._conn.execute(
            f"SELECT id, title, question, resolution, product_display, "
            f"date_create, confidence, "
            f"({score_sql}) as match_score "
            f"FROM cases WHERE {where_sql} "
            f"ORDER BY match_score DESC, confidence DESC LIMIT ?",
            args + [limit]).fetchall()

        results = []
        for r in rows:
            # Не совпал ни один токен — мусор, не отдаём
            if not (r["match_score"] or 0):
                continue
            # Берём первые 500 символов из resolution как snippet
            resolution = (r["resolution"] or "")[:500]
            # Нормированный скор совпадения (0..1): match_score / (3 × токенов).
            # Отличает попадание «по заголовку по всем словам» от случайного
            # совпадения одного стема в резолюции (confidence не подходит).
            if max_score > 0:
                norm = (r["match_score"] or 0) / max_score
            else:
                norm = r["confidence"] or 0
            results.append({
                "source": "solution",
                "path": f"solution_{r['id']}",
                "product": r["product_display"] or "",
                "title": r["title"] or "",
                "content": resolution,
                "score": round(min(1.0, norm), 3),
                "url": f"https://ts-b24-knowledge.search/api/solution?id={r['id']}",
                "deal_id": r["id"],
                "date": r["date_create"] or "",
            })
        return results


class CRMSource:
    """ts-b24: CRM сделки и контекст (FTS5)."""

    def __init__(self):
        import threading
        self._lock = threading.RLock()
        db_path = os.path.join(CRM_ROOT, "data", "ts_b24.db")
        if not os.path.exists(db_path):
            self._conn = None
            return
        self._conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True,
                                     check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=3000")

    def search(self, query, product=None, limit=3, timeout_ms=1000):
        if not self._conn:
            return []
        q = query.strip()
        if not q:
            return []

        # Use thread + timeout for CRM search (lock guards shared connection)
        result_holder = [[]]

        def _do_search():
            with self._lock:
                try:
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
                        [f"%{q}%"] * 3 + [limit]).fetchall()

                    for r in rows:
                        result_holder[0].append({
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
                except Exception:
                    pass

        import threading
        t = threading.Thread(target=_do_search, daemon=True)
        t.start()
        t.join(timeout=timeout_ms / 1000.0)

        return result_holder[0]


# ---------------------------------------------------------------------------
# Cross-links source
# ---------------------------------------------------------------------------

class CrossLinksSource:
    """Cross-links между решениями и документацией."""

    _DOCS_INDEX = os.path.join(KB_ROOT, "cache", "kb_index.db")

    def __init__(self):
        import threading
        self._lock = threading.RLock()
        db_path = os.path.join(KB_ROOT, "cache", "cross_links.db")
        if not os.path.exists(db_path):
            self._conn = None
            return
        self._conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True,
                                     check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._docs_conn = None
        if os.path.exists(self._DOCS_INDEX):
            try:
                self._docs_conn = sqlite3.connect(
                    f"file:{self._DOCS_INDEX}?mode=ro", uri=True,
                    check_same_thread=False)
                self._docs_conn.row_factory = sqlite3.Row
            except Exception:
                self._docs_conn = None

    def _page_title(self, product, page):
        """Настоящий заголовок страницы из kb_index.pages."""
        if not self._docs_conn or not product or not page:
            return None
        with self._lock:
            try:
                row = self._docs_conn.execute(
                    "SELECT title FROM pages WHERE product=? AND page=? LIMIT 1",
                    (product, page)).fetchone()
            except Exception:
                return None
        return row["title"] if row else None

    def get_related_docs(self, solution_id, limit=3):
        """Для решения возвращает связанные страницы документации."""
        if not self._conn:
            return []
        with self._lock:
            try:
                rows = self._conn.execute("""
                    SELECT doc_path, doc_product, doc_title, bm25_score
                    FROM solution_to_doc
                    WHERE solution_id = ?
                    ORDER BY bm25_score DESC
                    LIMIT ?
                """, (str(solution_id), limit)).fetchall()
            except Exception:
                return []
        results = []
        for r in rows:
            d = dict(r)
            path = d.get("doc_path") or ""
            if "__" in path:
                prod, _, page = path.partition("__")
                real_title = self._page_title(prod, page)
                if real_title:
                    d["doc_title"] = real_title
            results.append(d)
        return results

    def get_related_solutions(self, doc_path, limit=3):
        """Для страницы документации возвращает связанные решения."""
        if not self._conn:
            return []
        with self._lock:
            try:
                rows = self._conn.execute("""
                    SELECT solution_id, solution_title, solution_product, bm25_score
                    FROM doc_to_solution
                    WHERE doc_path = ?
                    ORDER BY bm25_score DESC
                    LIMIT ?
                """, (doc_path, limit)).fetchall()
            except Exception:
                return []
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

# Читаемые названия продуктов для отображения (кнопка продукта в веб-UI,
# «Продукт:» в telegram_bot). В ответе сохраняем оба поля: product (label),
# product_id (canonical) — исторический canonical уехал в топ-уровень/логи.
PRODUCT_DISPLAY_NAME = {
    "parts_intellect": "Parts.Intellect",
    "parts_resource": "Parts.Resource",
    "sync": "Синхронизатор",
    "diadok": "Диадок",
    "seo": "SEO-руководство",
    "delivery": "Parts.Intellect",
    "wazzup": "Wazzup",
    "tsd": "ТСД",
    "marketplace": "Маркетплейсы",
    "var": "ВАР",
    "other": "Другое",
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
    # Docs format (product prefixes → canonical IDs, одинаковые с решениями)
    _DOCS_PREFIX_CANONICAL = {
        "parts-resource-guide": "parts_resource",
        "parts-intellect-guide": "parts_intellect",
        "parts-intellect-synch": "sync",
        "diadok": "diadok",
        "seo-guide": "seo",
        "delivery_schedule": "delivery",
        "wazzup": "wazzup",
        "tsd": "tsd",
        "marketplace": "marketplace",
    }
    for prefix, canonical in _DOCS_PREFIX_CANONICAL.items():
        if product_name.startswith(prefix):
            return canonical
    return product_name


_CHANGES_HINTS = ("версия", "изменени", "что нового", "нового", "обновлени",
                  "changelog", "release", "5.2", "5.1", "6.", "7.")


def _asks_for_changes(query):
    """Запрос про версии/изменения → changelog-страницы уместны."""
    q = query.lower()
    return any(h in q for h in _CHANGES_HINTS)


# Сколько docs-страниц берём из гибридного поиска в общий пул. RRF считает по
# топ-30 (RRF_TOP) каждого источника, так что это лишь глубина среза; 8 было
# мало — после среза в пулах продуктов оставалось по 1-2 страницы, и родственные
# материалы (все «печать чека» Parts.Resource) не попадали в related_docs.
DOCS_MERGE_TOP = 24

# Feature-продукты, чья тема существует в нескольких системах (график поставок —
# в Parts.Resource/Parts.Intellect; маркетплейс — в Parts.Intellect + Resource и
# т.п.). При детекте такого продукта пул дополняется широким поиском, чтобы в
# ответе появлялись карточки остальных продуктов (см. _search_docs).
_FEATURE_CROSS_PRODUCTS = {"delivery"}

# API-справочники (parts-index-rest-api, parts-resource-rest-api) — в них
# справочный контент (методы, параметры, коды ответов), который
# для обычных (не-API) запросов не релевантен, но выбивается в топ из-за
# общих терминов («оплаты по банковским картам»). Для запросов без API-маркеров
# сдвигаем их вниз; для самих API-запросов демпт не применяем.
_API_QUERY_MARKERS = ("api", "rest", "токен", "token", "endpoint", "метод",
                      "http", "авторизаци", "ключ")
_API_DEMOTE_RANK = 8


def _api_intent(query):
    return any(m in query.lower() for m in _API_QUERY_MARKERS)


def merge_results(docs, solutions, crm, max_docs=DOCS_MERGE_TOP, max_solutions=5, max_crm=3,
                  query=""):
    """Объединяет результаты из 3 источников в единый список.

    Скоры разных источников несопоставимы (docs: гибрид/BM25, бывает
    отрицательным; solutions: 0..1; crm: 0..1). Для сквозного упорядочивания
    присваиваем скор по рангу в источнике: docs > solutions > crm на равных
    позициях, но хорошее решение (ранг 1) обгоняет слабую страницу (ранг ≥4).
    Исходный скор сохраняется в 'raw_score'.
    """
    all_results = []
    api_intent = _api_intent(query)

    for i, r in enumerate(docs[:max_docs]):
        r["raw_score"] = r.get("score", 0)
        rank = i
        if not api_intent and (r.get("product") or "").endswith("-rest-api"):
            rank += _API_DEMOTE_RANK
        r["score"] = round(1.0 * (0.92 ** rank), 3)
        r["product_canonical"] = _normalize_product("docs", r.get("product"))
        all_results.append(r)

    for i, r in enumerate(solutions[:max_solutions]):
        r["raw_score"] = r.get("score", 0)
        r["score"] = round(0.9 * (0.9 ** i), 3)
        r["product_canonical"] = _normalize_product("solution", r.get("product"))
        all_results.append(r)

    for i, r in enumerate(crm[:max_crm]):
        r["raw_score"] = r.get("score", 0)
        r["score"] = round(0.45 * (0.85 ** i), 3)
        r["product_canonical"] = None
        all_results.append(r)

    return all_results


# Страницы-«основная информация» по темам продуктов. Когда в результатах группы
# есть и основная страница раздела, и её фрагменты-дополнения (правила/примеры/
# алгоритмы), первичным блоком берём основную, а дополнения остаются в related.
# Ключ: (docs_product, тема из запроса).
MAIN_DOC_PAGES = {
    ("parts-intellect-guide", "печать чеков"): "protsess_pechati_chekov_v_programme.htm.md",
    # Часы Parts.Resource: «настроить печать чеков … принимать оплаты на сайте»
    # ведёт на Настройку онлайн-кассы (и настройка чеков, и приём онлайн-оплат),
    # а не на «Примеры печати чеков», которые находятся выше по FTS-скору.
    ("parts-resource-guide", "печать чеков"): "nastrojka_onlajn_kassy.htm.md",
}

# Страницы-«оглавления»: короткий тизер («Рассмотрим … подробнее»), а содержание
# лежит в дочерних страницах. Список детей показываем в related_docs блока.
# Ключ: path родительской страницы.
DOC_CHILD_PAGES = {
    "parts-resource-guide__pechat_chekov_polnogo_rascheta.htm.md": [
        "parts-resource-guide__pechat_cheka_polnogo_rascheta_po_pozitsiyam_zakazov.htm.md",
        "parts-resource-guide__pechat_cheka_polnogo_rascheta_po_otgruzkam.htm.md",
    ],
}


@lru_cache(maxsize=256)
def _page_title(path):
    """Заголовок страницы документации по path='product__page.htm.md'."""
    product, _, page = path.partition("__")
    try:
        con = sqlite3.connect(f"file:{KB_ROOT}/cache/kb_index.db?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT title FROM pages WHERE product=? AND page=?",
                (product, page),
            ).fetchone()
            return row[0] if row else path
        finally:
            con.close()
    except Exception:
        return path


def _docs_fts_related(query, product, exclude=(), limit=8):
    """Связанные страницы документации продукта по запросу (FTS-only, дёшево).

    Гибридный поиск обрезает топ-8 по всем продуктам сразу, из-за чего родственные
    материалы продукта (например, все страницы печати чеков Parts.Resource)
    не попадают в related_docs. Здесь — прямой FTS по продукту без векторов.
    """
    if not query or not product:
        return []
    try:
        from search import search as _fts, terms
        rows, _ = _fts(terms(query), product, limit=limit)
        out = []
        for row in rows:
            prod = row[0] if len(row) > 0 else ""
            page = row[1] if len(row) > 1 else ""
            title = row[2] if len(row) > 2 else ""
            path = f"{prod}__{page}"
            if path in exclude:
                continue
            out.append({"path": path, "title": title,
                        "product": prod, "score": 0.0})
        return out
    except Exception as e:
        print(f"[docs-fts-related] error: {e}", file=sys.stderr)
        return []


def compose_answers(results, intent, max_answers=2, cross_links=None, query=""):
    """Собирает 1-2 структурированных ответа из результатов.

    Каждый ответ содержит блоки how_to и how_it_works, упорядоченные по интенту.
    Если доступны cross-links, добавляет related_docs и related_solutions.
    """
    # Классифицируем блоки (по полному контенту, если он подтянут)
    for r in results:
        r["block_type"] = classify_block(r.get("source", ""),
                                         r.get("content_full") or r.get("content", ""))

    # Группируем по product_canonical
    by_product = {}
    for r in results:
        prod = r.get("product_canonical") or "general"
        by_product.setdefault(prod, []).append(r)

    primary, secondary = Intent.ORDER.get(intent, Intent.ORDER[Intent.GENERAL])

    # Иерархия источников для оформления ответа: инструкции документации
    # важнее карточек решений/CRM (решения и сделки не "воруют" заголовок).
    SOURCE_PRIORITY = {"docs": 0, "solution": 1, "crm": 2}

    # Основные продукты сведены раньше вспомогательных при прочих равных
    # (например, Инструкция Parts.Resource обходит механику Sync при близких скорах).
    PRODUCT_ORDER = {
        "parts_intellect": 0, "parts_resource": 1, "sync": 2, "diadok": 3,
        "seo": 4, "delivery": 5, "wazzup": 6, "tsd": 7, "marketplace": 8,
        "var": 9, "other": 10,
    }

    def _best(pool, block_type, exclude=None):
        """Лучший блок заданного типа из пула (сначала по типу, потом по скору)."""
        lst = [b for b in pool if block_type is None or b["block_type"] == block_type]
        # Кросс-продуктовые RRF-строки (product вида "a,b") в блоки не берём:
        # пустой контент и product, из которого не собрать ссылку. Их позиция
        # в пуле сохранена (см. _search_docs), чтобы не сдвигать ранги.
        lst = [b for b in lst if "," not in (b.get("product") or "")]
        if exclude:
            ex = {id(x) for x in exclude}
            lst = [b for b in lst if id(b) not in ex]
        lst.sort(key=lambda r: r.get("score", 0), reverse=True)
        return lst

    def _group_key(key, items):
        n_docs = sum(1 for r in items if r.get("source") == "docs")
        max_score = max(r.get("score", 0) for r in items)
        # Близкие скоры (до 0.1) сравниваем по приоритету продукта,
        # чтобы Инструкция Parts.Resource шла раньше механик Sync.
        bucket = round(max_score, 1)
        priority = PRODUCT_ORDER.get(key, 99)
        return (int(n_docs > 0), bucket, -priority, max_score)

    answers = []
    for prod, prod_results in sorted(
            by_product.items(),
            key=lambda x: _group_key(x[0], x[1]), reverse=True):
        pools = {
            "docs": [r for r in prod_results if r["source"] == "docs"],
            "solution": [r for r in prod_results if r["source"] == "solution"],
            "crm": [r for r in prod_results if r["source"] == "crm"],
        }

        # Первичный блок (заголовок): лучшая doc-страница группы по скору
        # (общие инструкции документации — в приоритете над решениями/CRM).
        # Ответ = ОДИН блок с полной информацией по запросу в рамках продукта.
        primary_block = None
        for src in ("docs", "solution", "crm"):
            lst = _best(pools[src], None)
            if lst:
                primary_block = lst[0]
                break

        # Если по теме запроса известна «основная» страница продукта — ставим её
        # первичным блоком (дополнения группы уходят в related).
        if primary_block is not None and primary_block.get("source") == "docs":
            for (doc_prod, topic), page in MAIN_DOC_PAGES.items():
                if doc_prod == primary_block.get("product") and topic in query:
                    prefs = [r for r in pools["docs"] if r.get("product") == doc_prod
                             and r.get("page") == page]
                    if prefs:
                        primary_block = prefs[0]
                    break

        if primary_block is None and prod_results:
            primary_block = prod_results[0]

        if primary_block is None:
            continue

        # В блок кладём полный контент страницы (не усечённый snippet).
        if primary_block.get("content_full"):
            primary_block["content"] = primary_block.pop("content_full")

        blocks = [primary_block]

        if blocks:
            answer = {
                "title": blocks[0].get("title", ""),
                "product": PRODUCT_DISPLAY_NAME.get(prod, prod) if prod != "general" else None,
                "product_id": prod if prod != "general" else None,
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
                        # И другие релевантные страницы того же продукта
                        # (собраны поиском, но не вошли в единственный блок,
                        # включая фрагменты-дополнения вроде «Правил… по СНО»).
                        sec_docs = [r for r in pools.get("docs", [])
                                    if r.get("path") != b.get("path")
                                    and "," not in (r.get("product") or "")]
                        sec_docs.sort(key=lambda r: r.get("score", 0), reverse=True)
                        for sd in sec_docs[:5]:
                            if not any(d.get("path") == sd["path"]
                                       for d in answer["related_docs"]):
                                answer["related_docs"].append({
                                    "path": sd["path"],
                                    "title": sd.get("title", ""),
                                    "product": sd.get("product", ""),
                                    "score": sd.get("score", 0),
                                })
                        # Страница-«оглавление»: подтягиваем дочерние страницы.
                        for child in DOC_CHILD_PAGES.get(b["path"], ()):
                            if not any(d.get("path") == child
                                       for d in answer["related_docs"]):
                                answer["related_docs"].append({
                                    "path": child,
                                    "title": _page_title(child),
                                    "product": child.partition("__")[0],
                                    "score": 0.0,
                                })
                        # Родственные материалы продукта по запросу (FTS-only):
                        # гибридный топ-8 обрезает их (например, всю «печать
                        # чеков» Parts.Resource), related не должен пустовать.
                        for rd in _docs_fts_related(query, b.get("product"),
                                                    exclude={b.get("path")},
                                                    limit=8):
                            if len(answer["related_docs"]) >= 12:
                                break
                            if not any(d.get("path") == rd["path"]
                                       for d in answer["related_docs"]):
                                answer["related_docs"].append(rd)

            answers.append(answer)

        if len(answers) >= max_answers:
            break

    return answers


# ---------------------------------------------------------------------------
# Main search function
# ---------------------------------------------------------------------------

# LRU cache for repeated queries (maxsize=128, ttl handled by caller)
@lru_cache(maxsize=128)
def _cross_search_cached(query, max_answers, limit_per_source):
    """Кешированный inner cross_search (ключ = query + params)."""
    return _cross_search_impl(query, max_answers, limit_per_source)


def cross_search(query, max_answers=2, limit_per_source=5):
    """Единый поиск из 3 источников (с кешем).

    Возвращает dict с ключами:
      - answers: list[Answer] — структурированные ответы
      - intent: str — определённый интент
      - product: str|None — определённый продукт
      - latency_ms: int — время выполнения
    """
    _ensure_index_fresh()
    t0 = time.time()
    result = _cross_search_cached(query, max_answers, limit_per_source)
    result["latency_ms"] = int((time.time() - t0) * 1000)
    return result


_LAST_IDX_MTIME = None


def _ensure_index_fresh():
    """Сброс LRU-ответов и заголовков страниц при пересборке индекса.

    Поисковый LRU (128 запросов) и _page_title без инвалидации отдавали бы
    устаревшие данные до перезапуска/вытеснения. Индекс (cache/kb_index.db)
    пересобирается build_index по WatchPaths — сверяем mtime на каждый запрос.
    """
    global _LAST_IDX_MTIME
    try:
        mt = os.path.getmtime(os.path.join(KB_ROOT, "cache", "kb_index.db"))
    except OSError:
        return
    if _LAST_IDX_MTIME is None:
        _LAST_IDX_MTIME = mt
        return
    if mt == _LAST_IDX_MTIME:
        return
    _LAST_IDX_MTIME = mt
    _cross_search_cached.cache_clear()
    _page_title.cache_clear()


def _cross_search_impl(query, max_answers, limit_per_source):
    """Основная логика поиска (вызывается из кеша или напрямую)."""
    t0 = time.time()

    # 1. Intent + Product detection
    intent = detect_intent(query)
    product = detect_product(query)

    # Map canonical product ID to docs product name
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
    docs_product = DOCS_PRODUCT_MAP.get(product) if product else None
    sol_product = product  # solutions DB uses same canonical IDs

    # 2. Parallel retrieval from all sources
    doc_results = []
    sol_results = []
    crm_results = []

    def _search_docs():
        try:
            ds = _get_docs_source()
            # Ищем по всем продуктам документации: строгий фильтр по detect_product
            # теряет changelog-страницы (-changes), wazzup, marketplace и т.п.,
            # которые присутствуют в docs-эталоне. Продукт остаётся per-документ
            # (флаг для группировки в compose), а не фильтром.
            # Запрашиваем больше docs, чтобы в результат попадали страницы
            # разных продуктов (Parts.Intellect, Parts.Resource, Sync...)
            rows = ds.search(query, None, limit=max(limit_per_source, DOCS_MERGE_TOP))
            if _asks_for_changes(query):
                return rows
            # Подавляем changelog-шум: страницы *-changes попадают в топ для
            # обычных запросов («как направить seo» → «Версия 6.64»).
            # Кросс-продуктовые «comma»-строки НЕ вырезаем из пула: их удаление
            # сдвигает позиционные скоры и ломает порядок групп (например, у
            # «печать чеков» Sync обгонял Parts.Resource в top-2). Отсеиваем их
            # при компоновке блоков (см. compose_answers), сохраняя ранги.
            rows = [r for r in rows
                    if not ("-changes" in (r.get("product") or ""))]

            # Тематическая экспансия: детектированный feature-продукт не сужает
            # тему — страницы «график поставок» есть и в Parts.Resource, и в
            # Parts.Intellect. Дополняем пул широким (без авто-детекта) поиском,
            # отбрасывая страницы самого feature-продукта; compose соберёт из них
            # отдельные карточки ответа ниже lead-карточки.
            if product in _FEATURE_CROSS_PRODUCTS:
                try:
                    wide = ds.search(query, None,
                                     limit=max(limit_per_source, DOCS_MERGE_TOP),
                                     _skip_auto_product=True)
                except Exception as e:
                    print(f"[docs] расширение пула: {e}", file=sys.stderr)
                    wide = []
                seen = {(r.get("product"), r.get("page")) for r in rows}
                for r in wide:
                    if "-changes" in (r.get("product") or ""):
                        continue
                    if (r.get("product"), r.get("page")) in seen:
                        continue
                    if _normalize_product("docs", r.get("product")) == product:
                        continue
                    rows.append(r)
            return rows
        except Exception as e:
            print(f"[docs] error: {e}", file=sys.stderr)
            return []

    def _search_solutions():
        try:
            sol = _get_solutions_source()
            rows = sol.search(query, sol_product, limit=limit_per_source)
            if _asks_for_changes(query):
                # Для запросов про версии ответы должны браться из docs-changelog;
                # решения показываем только при полном совпадении токенов запроса.
                rows = [r for r in rows if (r.get("score") or 0) >= 1.0]
            return rows
        except Exception as e:
            print(f"[solutions] error: {e}", file=sys.stderr)
            return []

    def _search_crm():
        try:
            crm = _get_crm_source()
            return crm.search(query, product, limit=3, timeout_ms=1500)
        except Exception as e:
            print(f"[crm] error: {e}", file=sys.stderr)
            return []

    # Parallel execution
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            executor.submit(_search_docs): "docs",
            executor.submit(_search_solutions): "solutions",
            executor.submit(_search_crm): "crm",
        }
        for future in as_completed(futures):
            source = futures[future]
            try:
                result = future.result()
                if source == "docs":
                    doc_results = result
                elif source == "solutions":
                    sol_results = result
                elif source == "crm":
                    crm_results = result
            except Exception as e:
                print(f"[{source}] future error: {e}", file=sys.stderr)

    # 3. Merge + Compose with cross-links
    merged = merge_results(doc_results, sol_results, crm_results, query=query)

    cross_links = None
    try:
        cross_links = _get_cross_links_source()
    except Exception as e:
        print(f"[cross-links] init error: {e}", file=sys.stderr)

    try:
        answers = compose_answers(merged, intent, max_answers=max_answers,
                                  cross_links=cross_links, query=query)
    except Exception as e:
        print(f"[compose] error: {e}", file=sys.stderr)
        answers = compose_answers(merged, intent, max_answers=max_answers,
                                  cross_links=None, query=query)

    latency_ms = int((time.time() - t0) * 1000)

    return {
        "answers": answers,
        "intent": intent,
        "product": product,
        "product_display": PRODUCT_DISPLAY_NAME.get(product, product),
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
