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
import re
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
# Расширение решений-ссылок на тикеты DokuWiki
# (resolution вида «Ссыдка https://dokuwiki.tradesoft.ru/tickets:N …»)
# ---------------------------------------------------------------------------

_TICKET_URL_RE = re.compile(r"dokuwiki\.tradesoft\.ru/tickets:([\w.\-]+)")

# Стемы, являющиеся именами продуктов. Когда product уже отфильтрован
# отдельным условием, такие токены в AND-LIKE бесполезны (и даже вредны:
# кейсы с псевдонимом auto_intellect не содержат подстроки «parts.intellect»).
_SOL_PRODUCT_NAME_STEMS = {
    "parts_intellect": frozenset({"parts.intellect", "autoинтеллект",
                                  "интеллект", "parts"}),
    "parts_resource": frozenset({"parts.resource", "parts"}),
    "sync": frozenset({"синхронизатор", "sync"}),
    "var": frozenset({"var"}),
}


def _ticket_from_cache(tid):
    """Распарсенный тикет DokuWiki из локального raw-кэша ts-b24-knowledge.

    Сеть не трогаем: читаем только файлы в data/wiki_tickets/raw/<id>.txt.
    """
    if not re.match(r"^[\w.\-]+$", tid or ""):
        return None
    raw_path = os.path.join(KNOWLEDGE_ROOT, "data", "wiki_tickets",
                            "raw", tid + ".txt")
    try:
        with open(raw_path, encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        return None
    try:
        know_scripts = os.path.join(KNOWLEDGE_ROOT, "scripts")
        if know_scripts not in sys.path:
            sys.path.insert(0, know_scripts)
        from wiki_tickets import WikiTickets
        return WikiTickets._parse(WikiTickets(), raw, tid)
    except Exception:
        return None


def _expand_dokuwiki_links(resolution):
    """Если resolution ссылается на dokuwiki-тикет, дополняем контент
    распарсенным текстом тикета (Вопрос/Ответ/Примечание)."""
    if not resolution or "dokuwiki" not in resolution:
        return resolution
    tids = list(dict.fromkeys(_TICKET_URL_RE.findall(resolution)))
    if not tids:
        return resolution
    for tid in tids:
        t = _ticket_from_cache(tid)
        if not t:
            continue
        parts = []
        if t.get("title"):
            parts.append(t["title"])
        for key, label in (("question", "Вопрос:"),
                           ("answer", "Ответ:"),
                           ("note", "Примечание:")):
            v = (t.get(key) or "").strip()
            if v:
                parts.append(f"{label}\n{v}")
        if parts:
            resolution += "\n\n" + "\n".join(parts)
    return resolution


_TICKET_MIN_SCORE = 25.0  # minimum match score to attach a ticket as solution body


def _tickets_for_case(case_id):
    """Матчнутые тикеты (ticket_id, score) для кейса из matches.json.

    Используем только надёжные (strong/medium, score >= 25) — их текст
    почти наверняка отвечает на вопрос заявки. Дешёвые по подобию фраз
    (weak) в текст решения не тащим.
    """
    try:
        p = os.path.join(KNOWLEDGE_ROOT, "data", "wiki_tickets", "matches.json")
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        lst = (data.get("_reverse") or {}).get(case_id, [])
    except Exception:
        return []
    out = []
    for x in sorted(lst, key=lambda it: it.get("score") or 0, reverse=True):
        if (x.get("score") or 0) >= _TICKET_MIN_SCORE:
            out.append(x)
        if len(out) >= 2:
            break
    return out


def _expand_case_tickets(case_id, resolution):
    """Дополняем резолюцию решения текстом матчнутых тикетов.

    Для кейсов «без решения» (переписка не извлечена из заявки) ответ
    живёт в докеринге: заявка сопоставлена тикету (matches.json), чей
    текст — фактическое поведение и планы. Подтягиваем его в контент,
    если в резолюции нет dokuwiki-ссылок и она почти пустая/короткая.
    """
    if (resolution or "").strip() and "dokuwiki" not in (resolution or ""):
        return resolution
    if len((resolution or "").strip().split("\n")) > 3:
        return resolution
    for x in _tickets_for_case(case_id):
        t = _ticket_from_cache(x.get("ticket_id"))
        if not t:
            continue
        parts = []
        if t.get("title"):
            parts.append(t["title"])
        for key, label in (("question", "Вопрос:"),
                           ("answer", "Ответ:"),
                           ("note", "Примечание:")):
            v = (t.get(key) or "").strip()
            if v:
                parts.append(f"{label}\n{v}")
        if parts:
            resolution = (resolution or "").rstrip() + "\n\n" + "\n".join(parts)
    return resolution

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
        degraded = False
        if not result_holder[0]:
            degraded = True
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
                "_degraded": degraded,
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

    def get_by_path(self, path):
        """Возвращает entry страницы документации по 'product__page'
        с полным контентом, либо None, если страница не найдена."""
        product, _, page = path.partition("__")
        if not product or not page:
            return None
        try:
            con = sqlite3.connect(f"file:{KB_ROOT}/cache/kb_index.db?mode=ro", uri=True)
            try:
                chunks = con.execute(
                    "SELECT content FROM pages WHERE product=? AND page=? ORDER BY chunk",
                    (product, page)).fetchall()
            finally:
                con.close()
        except Exception as e:
            print(f"[docs] get_by_path: {e}", file=sys.stderr)
            return None
        if not chunks:
            return None
        full = "\n".join(r[0] or "" for r in chunks)
        return {
            "source": "docs",
            "path": path,
            "product": product,
            "page": page,
            "title": _page_title(path),
            "content": _build_excerpt(product, _page_title(path), full),
            "content_full": full,
            "score": 0.0,
            "url": _build_doc_url(product, page),
        }


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
        # Имена продуктов бесполезны в AND-LIKE: продукт уже отфильтрован
        # выше (или входит в WHERE), а в тексте кейсов часты псевдонимы
        # (auto_intellect и т.п.) без дословного наименования.
        if product:
            drop = _SOL_PRODUCT_NAME_STEMS.get(product, frozenset())
            stemmed_tokens = [t for t in stemmed_tokens if t not in drop]
        if not stemmed_tokens:
            return []
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
            # Берём первые 500 символов из resolution как сниппет;
            # ссылки на тикеты DokuWiki расширяем текстом тикета.
            resolution = _expand_dokuwiki_links(r["resolution"] or "")
            resolution = _expand_case_tickets(r["id"], resolution)
            if "Ответ:" not in resolution and "Вопрос:" not in resolution:
                resolution = resolution[:500]
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
    "parts-intellect-changes": "Parts.Intellect · список изменений",
    "parts-resource-changes": "Parts.Resource · список изменений",
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
                  "changelog", "release", "5.2", "5.1", "6.", "7.",
                  "реализова", "когда", "какой верси")


@lru_cache(maxsize=256)
def _corrected_query(query):
    """Каноническая форма запроса с исправлением опечаток (какоq→какой).

    Substring-хинты интента теряют опечатанные слова («в какоq версии…»),
    из-за чего changelog-ответ не включает версию-кандидат. Канонизация с
    правкой опечаток (search._canonical_query) — обычная стоимость каждого
    запроса, здесь она переиспользуется для проверок интента.
    """
    try:
        from search import _canonical_query
        cq, _ = _canonical_query(query)
        return cq or query
    except Exception:
        return query


def _has_hint(query, hints):
    q = query.lower()
    if any(h in q for h in hints):
        return True
    cq = _corrected_query(query).lower()
    return cq != q and any(h in cq for h in hints)


def _asks_for_changes(query):
    """Запрос про версии/изменения → changelog-страницы уместны."""
    return _has_hint(query, _CHANGES_HINTS)


# Вопросы, где changelog — прямой ответ («когда реализована возможность…»),
# а не просто фон: в таких случаях версии должны опережать guide-страницы.
_CHANGES_ANSWER_HINTS = ("когда", "с какой верси", "в какой верси",
                         "какая верси", "в каком обновлени",
                         "реализова", "ввели", "добавили")


def _changes_are_answer(query):
    """Niques, где список изменений отвечает на вопрос напрямую."""
    return _has_hint(query, _CHANGES_ANSWER_HINTS)


# Стоп-стемы, не несущие смысла для близости в changelog'ах («когда… в какой
# версии это случилось»): они частотны сами по себе и их присутствие в тексте
# версии только «притягивает» anchor в _proximity к шуму.
_CHANGES_NOISE_STEMS = frozenset({
    "когд", "какой", "это", "тот", "они", "теперь",
    "происход", "случилось", "про",
})

# Мета-стемы вопросов про версии: вопросительные/служебные слова и глаголы
# изменения («произошли изменения относительно…»), не несущие темы. Для
# changelog-подхода (см. _rank_changelog) они выкидываются из «темы» запроса,
# потому что засоряют и вектор, и лексику: «изменени» есть в каждом changelog
# (df=472), «произой/относительн/перед» («переехало»→«перед») отсутствуют в
# тексте правильной версии (5_17), из-за чего её вытесняют шумные кандидаты.
_CHANGES_META_STEMS = frozenset({
    "когд", "какой", "это", "тот", "они", "теперь",
    "происход", "случилось", "про", "какие", "какая",
    "произой", "изменен", "относительн", "верси", "перед",
    "стали", "стало", "быть", "введен", "ввели",
    "добавили", "добавлен", "реализован", "реализованн", "появился",
    "вышел", "обновлен", "в", "с", "по", "на", "для", "про",
})


def _promote_changes(rows, query=""):
    """Ставит -changes страницы в начало пула.

    Внутри changelog'ов приоритет определяется релевантностью запросу: сначала
    changelog продукта, названного в запросе («Parts.Intellect» → только его
    -changes впереди, resource-версии ниже), затем по близости значимых стемов
    запроса в тексте версии. Changelog-страницы большие (список изменений за
    версию), поэтому «наличие слова» их не различает — важна близость: версия,
    где «перенесен… шаблон… печатн» стоят рядом, отвечает точнее, чем список,
    где те же слова раскиданы по разделу.

    Дополнительно changelog'и делятся на «тематический ярус» — страницы, где
    встречается хотя бы один редкий значимый стем запроса (df в 20..80 или
    цифра в 1..80), — и остальные. Ярус релевантных версий идёт вперёд, остаток
    вниз; так длинный запрос «…в какой версии это случилось?» не затягивает
    версию, где значимые стемы разнесены, выше версии с локальной близостью.
    """
    changes = [r for r in rows if "-changes" in (r.get("product") or "")]
    others = [r for r in rows if "-changes" not in (r.get("product") or "")]
    if not changes:
        return rows

    from search import (_correct_terms, detect_product_name, normalize_terms,
                        terms, ULTRA_COMMON, _term_doc_frequency)
    product_name = detect_product_name(query) if query else None
    base_product = (product_name or "").partition("-")[0] if product_name else None
    stems = _correct_terms(normalize_terms(terms(query))) if query else []
    meaningful = [t for t in stems if t not in ULTRA_COMMON]

    def _proximity(r):
        """Минимальная дистанция (символы) от самого редкого значимого стема
        запроса до соседнего значимого стема. Большое число = стемы далеко,
        версия плохо отвечает на вопрос."""
        text = (r.get("content_full") or r.get("content") or "").lower()
        if not text or not meaningful:
            return 10 ** 7
        occ = {}
        for t in meaningful:
            m = [mm.start() for mm in re.finditer(
                r"(?<![a-zа-яё0-9])" + re.escape(t) + r"[a-zа-яё]*", text)]
            if m:
                occ[t] = m
        if len(occ) < 2:
            return 10 ** 7
        anchor = min(occ, key=lambda t: (_term_doc_frequency(t), t))
        others = [t for t in occ if t != anchor]
        return min(abs(a - b)
                   for a in occ[anchor]
                   for b in [p for t in others for p in occ[t]])

    def _changelog_key(r):
        prod = r.get("product") or ""
        # Приоритет 1: changelog того же продукта, что и в запросе.
        same_line = 0 if (base_product and prod.partition("-")[0] == base_product) else 1
        # Приоритет 2: плотность кластера значимых стемов.
        return (same_line, _proximity(r))

    def _df(t):
        try:
            return _term_doc_frequency(t)
        except Exception:
            return 10 ** 9

    rare_tier = [t for t in meaningful
                 if t not in _CHANGES_NOISE_STEMS
                 and ((t.isdigit() and 0 < _df(t) <= 80)
                      or (20 <= _df(t) <= 80))]

    def _has_rare(r):
        if not rare_tier:
            return False
        text = (r.get("content_full") or r.get("content") or "").lower()
        return any(re.search(r"(?<![a-zа-яё0-9])" + re.escape(t) + r"[a-zа-яё]*", text)
                   for t in rare_tier)

    if rare_tier:
        tier_top = [r for r in changes if _has_rare(r)]
        tier_rest = [r for r in changes if not _has_rare(r)]
    else:
        tier_top, tier_rest = changes, []
    tier_top.sort(key=_changelog_key)
    changes = tier_top + tier_rest
    return changes + others


# Сколько changelog-кандидатов из полного скана корпуса добавляем в начало пула
# для change-answer запросов. Кандидаты идут первыми и получают позиционный
# бонус при слиянии, поэтому тащим только релевантный топ.
_CHANGELOG_RANK_TOP = 10

_CHANGES_TOKEN_STOP = frozenset({
    "в", "и", "с", "по", "на", "для", "не", "те", "их", "это",
    "то", "том", "что", "за", "а", "или", "нужно", "как", "к",
    "от", "у", "если", "также", "был", "была", "были", "было",
})


def _change_topic(query):
    """Дистилляция «темы» вопроса про версии: отбрасывает мета-стемы и служебные
    слова, возвращает (topic_str, topic_stems). topic_str — исходные токены для
    векторного эмбеддинга, topic_stems — их корни для лексического слоя.
    Пустая тема -> (None, None): ранкер деградирует к _promote_changes."""
    from search import _correct_terms, normalize_terms, ULTRA_COMMON
    topic_tokens, topic_stems = [], []
    seen = set()
    for tk in query.split():
        if tk.lower() in _CHANGES_TOKEN_STOP:
            continue
        try:
            stems = _correct_terms(normalize_terms([tk]))
        except Exception:
            continue
        if not stems:
            continue
        s = stems[0]
        if s in _CHANGES_META_STEMS or s in ULTRA_COMMON:
            continue
        topic_tokens.append(tk)
        if s not in seen:
            seen.add(s)
            topic_stems.append(s)
    if not topic_tokens or not topic_stems:
        return None, None
    return " ".join(topic_tokens), topic_stems


def _rank_changelog(rows, query=""):
    """Ставит changelog-страницы в начало пула для change-answer запросов.

    В отличие от _promote_changes ранжирует changelog-корпус ПОЛНОСТЬЮ (все
    -changes страницы всех продуктов, без ретривального гейта top-N): правильная
    версия часто отсутствует в топ-24 гибрида, потому что мета-слова вопроса
    («произошли/изменения/относительно») не встречаются в её тексте и FTS/вектор
    ранжируют по зашумлённому полному запросу.

    Два слоя скоринга:
      - лексика (всегда): лучшая строка страницы — сколько уникальных стемов
        темы встречается в одной строке, tie-break по ширине span. Стемы темы
        нижесканятся по префиксным вариантам (без pymorphy): «база»→«баз»
        ловит «базу/базе/базы», остальные корни уже являются префиксами форм
        после среза гласного стемером («хранени» ⊆ «хранения»);
      - вектор (если Ollama доступна): ранг vector_main(topic_str, <prod>-changes)
        с таймаутом, чтобы не вешать запрос на зависший эмбеддер.
    Композит: product-линия, затем vector_pos (вектор доминирует над лексикой),
    затем лексический кортеж (cnt, span, редкие стемы).
    """
    topic_str, topic_stems = _change_topic(query)
    if not topic_str:
        # Тему не вычленить (вопрос почти весь из мета-слов) — прежнее поведение.
        return _promote_changes(rows, query)

    from search import _term_doc_frequency as _df, detect_product_name
    base_product = None
    try:
        pn = detect_product_name(query)
    except Exception:
        pn = None
    if pn:
        base_product = pn.partition("-")[0]

    # --- корпус changelog-страниц (из parsed-файлов, вне ретривного гейта) ---
    corpus = []
    for prod in ("parts-intellect-changes", "parts-resource-changes"):
        d = os.path.join(KB_ROOT, "cache", prod, "parsed")
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if fn.endswith(".htm.md"):
                p = os.path.join(d, fn)
                try:
                    with open(p, encoding="utf-8") as f:
                        text = f.read()
                except Exception:
                    continue
                corpus.append((prod, fn, text))
    if not corpus:
        return _promote_changes(rows, query)

    # --- лексический слой: лучшая строка по уникальным стемам темы ---
    VOWELS = "аяуюеио"

    def _prefixes(s):
        out = {s}
        if len(s) >= 4 and s[-1] in VOWELS:
            out.add(s[:-1])
        return sorted(out, key=len, reverse=True)

    stems = [(s, _prefixes(s)) for s in topic_stems]
    rare = {s for s in topic_stems if _df(s) <= 40}
    lex = {}  # (product, page) -> (cnt, span, rare_w)
    for prod, page, text in corpus:
        best = None
        for line in text.splitlines():
            line = line.lower()
            mpos = {}
            for s, pr in stems:
                pos = None
                for p2 in pr:
                    m = re.search(
                        r"(?<![a-zа-яё0-9])" + re.escape(p2) + r"[a-zа-яё]*",
                        line,
                    )
                    if m and (pos is None or m.start() < pos):
                        pos = m.start()
                if pos is not None:
                    mpos[s] = pos
            if not mpos:
                continue
            cnt = len(mpos)
            span = max(mpos.values()) - min(mpos.values()) + 1
            rare_w = sum(2.0 if s in rare else 1.0 for s in mpos)
            key = (cnt, -span, rare_w)
            if best is None or key > best:
                best = key
        if best:
            lex[(prod, page)] = (best[0], -best[1], best[2])

    if not lex:
        return _promote_changes(rows, query)

    # --- векторный слой (если доступен), с таймаутом как у DocsSource ---
    # Один unfiltered vector_main по всему docs-индексу: позиция страницы среди
    # changelog-корпуса выводится из её глобального ранга (страницы прямо
    # нерелевантного продукта отсекаются same_line). Продуктовый фильтр через
    # vector_main с продуктом содержит '-' и тихо возвращает пусто (Typesense
    # не парсит unquoted filter_by) — поэтому фильтруем по рангу, а не по RPC.
    vec_rank = {}
    try:
        import threading
        from search import vector_main
        holder = [[]]

        def _run():
            try:
                holder[0] = vector_main(topic_str, None, limit=25)[0]
            except Exception:
                pass

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=4.0)
        for i, hit in enumerate(holder[0]):
            if len(hit) >= 2 and hit[1]:
                vec_rank[(hit[0] or "", hit[1])] = i
    except Exception:
        pass

    # --- композитная сортировка ---
    scored = []
    for (prod, page), (cnt, span, rare_w) in lex.items():
        same_line = 0 if (base_product and prod.partition("-")[0] == base_product) else 1
        vpos = vec_rank.get((prod, page), 10 ** 9)
        scored.append((same_line, vpos, cnt, span, rare_w, prod, page))
    scored.sort(key=lambda x: (x[0], x[1], -x[2], x[3], -x[4]))

    picked = []
    keep = set()
    base_by_key = {(r.get("product"), r.get("page")): r for r in rows}
    for it in scored[:_CHANGELOG_RANK_TOP]:
        prod, page = it[5], it[6]
        key = (prod, page)
        entry = base_by_key.get(key)
        if entry is None:
            try:
                entry = _get_docs_source().get_by_path(f"{prod}__{page}")
            except Exception:
                entry = None
        if entry:
            picked.append(entry)
            keep.add(key)
    if not picked:
        return _promote_changes(rows, query)

    others = [r for r in rows if (r.get("product"), r.get("page")) not in keep]
    return picked + others


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

# Темы, не привязанные к одному продукту. Живут в нескольких системах
# (например, «печать чеков» — и POS Parts.Intellect, и онлайн-касса Parts.Resource).
# Для них пул дополняется поиском по самой теме в «другом» продукте (см. _search_docs).
_CROSS_PRODUCT_TOPICS = ("печать чек", "печати чек", "печатать чек")
_CROSS_TOPIC_OTHER = {
    "печать чеков": {
        "parts_resource": "parts-intellect-guide",
        "parts_intellect": "parts-resource-guide",
    },
}

# Подвопросы темы «печать чеков / касса / оплаты» и страницы, раскрывающие их
# как последовательные блоки карточек (полноценный ответ вместо ссылки в related):
#   Parts.Resource (интернет-магазин):
#     оплаты на сайте → «Прием онлайн платежей», «Настройка способов оплаты»;
#   Parts.Intellect (POS):
#     печать чеков    → «Настройка подключения ККТ»,
#     POS-терминал    → «Настройка подключения Эквайринга» (типы терминалов:
#                       интегрированный — Сбербанк / не интегрированный),
#                       «Утилита для работы с POS-терминалом».
_TOPIC_FOLLOWUP_BLOCKS = {
    "parts_resource": [
        {
            "markers": ("оплат", "платеж", "касс", "онлайн", "сайт"),
            "pages": [
                "priem_onlajn_platezhej.htm.md",
                "nastrojka_sposobov_oplaty.htm.md",
                "pechat_cheka_avansa_pri_oplate_v_onlajn.htm.md",
            ],
        },
        {
            "markers": ("маркировк", "маркированн", "честный знак"),
            "pages": [
                "rabota_s_markirovkoj.htm.md",
                "nastrojka_markirovannoj_produktsii.htm.md",
            ],
        },
    ],
    "parts_intellect": [
        {
            "markers": ("печать чек", "печати чек", "печатать чек",
                        "ккт", "терминал", "эквайринг", "онлайн-касс"),
            "pages": [
                "nastrojka_podklyucheniya_kkt.htm.md",
                "nastrojka_podklyucheniya_ehkvajringa.htm.md",
                "utilita_dlya_raboty_s_pos_terminalom.htm.md",
            ],
        },
    ],
}


def _topic_followup_pages(query, canonical):
    """Страницы-подблоки для карточки canonical, релевантные теме запроса."""
    topics = _TOPIC_FOLLOWUP_BLOCKS.get(canonical)
    if not topics:
        return ()
    q = query.lower()
    for topic in topics:
        if any(m in q for m in topic["markers"]):
            return topic["pages"]
    return ()


def _cross_topic_other(query):
    """Продукт для тематического дополнения в зависимости от темы и продукта
    доминирующего пула (parts_resource <-> parts_intellect)."""
    q = query.lower()
    if any(t in q for t in _CROSS_PRODUCT_TOPICS):
        return _CROSS_TOPIC_OTHER["печать чеков"]
    return {}


def _related_query(query):
    """Запрос для поиска «родственных материалов» в related_docs.

    Для кросс-продуктовых тем (печать чеков) полный шумный запрос
    («... resource b принимать оплаты на сайте?») выдаёт в FTS чужеродные
    страницы («Создание нового заказа клиента») — связанные материалы ищем
    по самой теме.
    """
    q = query.lower()
    if any(t in q for t in _CROSS_PRODUCT_TOPICS):
        return "печать чеков"
    return query

# ---------------------------------------------------------------------------
# Адаптивные заголовки блоков («модуль · действие»)
# ---------------------------------------------------------------------------
# Из «сырых» названий страниц генерируем пару (module, action): функционал и
# действие, которое он описывает. Заголовок блока и карточки строится как
# «Продукт · Модуль · Действие». Остальные поля (module/action/role/step)
# проходят в структуру ответа и уже там адаптируются интерфейсами.
_ACTION_PREFIX = (
    ("Настройка подключения и ", "подключение и настройка"),
    ("Настройка подключения ", "подключение"),
    ("Настройка ", "настройка"),
    ("Настройки ", "настройка"),
    ("Подключение и настройка ", "подключение и настройка"),
    ("Подключение ", "подключение"),
    ("Прием ", "приём"),
    ("Печать ", "печать"),
    ("Процесс ", "процесс"),
    ("Утилита для работы с ", "утилита"),
    ("Поступление ", "поступление"),
    ("Оплата ", "оплата"),
    ("Оформление ", "оформление"),
    ("Создание ", "создание"),
    ("Формирование ", "формирование"),
    ("Восстановление ", "восстановление"),
    ("Импорт ", "импорт"),
    ("Экспорт ", "экспорт"),
)

# Приведение падежных форм модуля к начальной (только частые обороты).
_MODULE_FIXES = {
    "онлайн-кассы": "онлайн-касса",
    "способов оплаты": "способы оплаты",
    "онлайн платежей": "онлайн-платежи",
    "подключения ККТ": "ККТ",
    "POS-терминалом": "POS-терминал",
    "эквайринга": "эквайринг",
    "печати чеков": "печать чеков",
    "принтера чеков": "принтер чеков",
    "графиков поставок": "графики поставок",
    "заказа клиента": "заказ клиента",
}

# Исключения по пути страницы — там, где исходное название не подходит как
# заголовок блока (общие «Процесс …», «Настройка подключения …» и т.п.).
# Значение — естественный заголовок, который показывается после названия
# продукта. Добавляем только точечно.
_HEADER_EXCEPTIONS = {
    "parts-intellect-guide__protsess_pechati_chekov_v_programme.htm.md":
        "Печать чеков в программе",
    "parts-intellect-guide__sozdanie_novogo_zakaza_klienta.htm.md":
        "Создание нового заказа",
    "parts-resource-guide__pechat_cheka_avansa_pri_oplate_v_onlajn.htm.md":
        "Печать чека аванса при оплате в онлайн",
    "parts-intellect-guide__nastrojka_podklyucheniya_ehkvajringa.htm.md":
        "Настройка подключения эквайринга",
}


def _fix_module(module):
    return _MODULE_FIXES.get(module, module)


def _header_parts(block):
    """Возвращает (module, action) для блока по названию страницы.

    Метаданные (module/action) оставлены в ответе для интерфейсов, но
    заголовок блока строится из естественного названия страницы.
    """
    title = (block.get("title") or "").strip()
    exc = _HEADER_EXCEPTIONS.get(block.get("path") or "")
    if exc:
        return title, None
    for prefix, action in _ACTION_PREFIX:
        if title.startswith(prefix):
            module = _fix_module(title[len(prefix):].strip())
            return module, action
    return title or None, None


def enrich_block_headings(blocks):
    """Проставляет блокам module/action/display_title/role/step.

    display_title — естественное название страницы (с точечными
    исключениями по пути), используется в интерфейсах; module/action —
    справочные метаданные для API-потребителей. docs-блоки получают
    порядковый номер шага в карточке (1..N по всем источникам docs),
    остальные источники — свой тип в роли без шага.
    """
    step_n = 0
    for b in blocks:
        if not isinstance(b, dict):
            continue
        if b.get("source") == "docs":
            step_n += 1
            b["role"] = "step"
            b["step"] = step_n
        else:
            b["role"] = b.get("source", "docs")
            b["step"] = None
        module, action = _header_parts(b)
        b["module"] = module
        b["action"] = action
        b["display_title"] = _HEADER_EXCEPTIONS.get(b.get("path") or "",
                                                    b.get("title") or module or "")

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

    # Тематически расширенные строки (cross_expand), например страницы «печати
    # чеков» другого продукта, всегда в хвосте пула и за max_docs обрезаться
    # не должны — иначе карточка этого продукта не соберётся вовсе.
    docs_listed = list(docs)
    docs_core = [r for r in docs_listed if not r.get("cross_expand")][:max_docs]
    docs_merged = docs_core + [r for r in docs_listed if r.get("cross_expand")]

    for i, r in enumerate(docs_merged):
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


# ---------------------------------------------------------------------------
# Выбор первичного блока ответа
# ---------------------------------------------------------------------------

_STOP_CHARS_RE = re.compile(r"[\W_]+", re.UNICODE)


def _query_significant_stems(query, product=None):
    """Значимые стемы запроса: без стоп-слов и имён продуктов.

    Нужны для проверки, действительно ли кандидат в primary-block отвечает
    на запрос (документация иногда отдаёт мусорную страницу, где совпал
    только общий токен «загрузка», а тема запроса — сертификаты/маркировка).
    """
    try:
        from search import normalize_terms, terms
        stems = normalize_terms(terms(query))
    except Exception:
        stems = []
    drop = set()
    for prod, names in _SOL_PRODUCT_NAME_STEMS.items():
        drop |= names
    droppable = ((product or "").replace("-", "_"),)
    out = []
    for s in stems:
        if s in drop or s in droppable:
            continue
        out.append(s)
    return list(dict.fromkeys(out))


def _rare_stems(stems):
    """«Редкие» (специфичные) стемы запроса: небольшой df в индексе.

    Общие токены («загрузка» df≈240, «сделать» df≈60) есть во многих
    страницах и не различают тему; редкие («сертификат» df=2, «ПИоТ» df=4)
    именно её и задают. По ним и решаем, отвечает ли doc на запрос.
    """
    try:
        from search import _term_doc_frequency as _df
    except Exception:
        return stems
    out = []
    for s in stems:
        try:
            if 0 < _df(s) <= 20:
                out.append(s)
        except Exception:
            continue
    return out


def _block_covers_stems(block, stems):
    """Сколько значимых стемов запроса покрывает блок (0..len(stems)).

    Документация иногда отдаёт страницу, где совпал только общий токен
    («загрузка»), а тема запроса (сертификаты/маркировка) в тексте отсутствует.
    Решение, покрывающее больше значимых стемов, релевантнее такого doc.
    """
    if not stems:
        return 0
    text = (block.get("title") or "").lower()
    text += " " + ((block.get("content_full") or block.get("content") or "")).lower()
    n = 0
    for s in stems:
        if re.search(r"(?<![a-zа-яё0-9])" + re.escape(s) + r"[a-zа-яё]*", text):
            n += 1
    return n


_PAYMENT_TOPIC_HINTS = ("касс", "оплат", "чек", "онлайн", "эквайринг")


def _is_part_resource_payment_doc(block):
    """Страница документации Parts.Resource про онлайн-оплату/кассу.

    Платёжные страницы Parts.Resource («Настройка онлайн-кассы», «Онлайн-кассы»,
    «Способы оплаты») не содержат слово «эквайринг» (оно из Parts.Intellect), из-за
    чего правило rare-подмены doc→solution считало их «мусором» и заголовок ответа
    уезжал к карточке решения по банку-эквайеру. Такие страницы — прямой
    документированный ответ на платёжный запрос в Parts.Resource, их не вытесняем.
    """
    if (block.get("product") or "") != "parts-resource-guide":
        return False
    text = ((block.get("title") or "") + " " + ((block.get("content_full") or block.get("content") or ""))).lower()
    return any(h in text for h in _PAYMENT_TOPIC_HINTS)


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
        # Позиция группы среди ответов определяется лучшей карточкой. Когда в
        # группе есть документация, решения/CRM её не «обгоняют» (иначе тема
        # «печать чеков» при добавлении карточки Parts.Intellect из-за решения
        # «настроить кассу» вытеснила бы Parts.Resource из top-1).
        if n_docs:
            max_score = max(r.get("score", 0) for r in items
                            if r.get("source") == "docs")
        else:
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
        sig_stems = _query_significant_stems(query, prod)
        primary_block = None
        for src in ("docs", "solution", "crm"):
            lst = _best(pools[src], None)
            if not lst:
                continue
            # Документация-«мусор» (совпал только общий токен вроде «загрузка»,
            # а темы запроса в странице нет) не должна закрывать релевантное
            # решение. Меняем doc на решение только когда в запросе есть
            # «редкий» специфичный стем (сертификат/ПИоТ/SSL), который
            # решение покрывает, а doc — нет, И doc-страница слабая
            # (отрицательный гибридный скор — «мусор», а не релевантный ответ).
            if src == "docs":
                doc_raw = lst[0].get("raw_score", 0)
                rare = _rare_stems(sig_stems)
                sol = _best(pools["solution"], None)
                if rare and doc_raw < 0 and sol and not _is_part_resource_payment_doc(lst[0]):
                    sol_rare = _block_covers_stems(sol[0], rare)
                    doc_rare = _block_covers_stems(lst[0], rare)
                    if sol_rare >= len(rare) > doc_rare:
                        primary_block = sol[0]
                        break
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

        # Полноценный последовательный ответ: если в запросе несколько подвопросов
        # (печать чеков + оплаты на сайте + ККТ/POS-терминал), добавляем подблоки
        # с раскрытием остальных частей, чтобы ответ был исчерпывающим, а не
        # ссылкой на related. Страницы — из приоритетного списка карточки
        # продукта (см. _TOPIC_FOLLOWUP_BLOCKS), не более 2 подблоков.
        followup_pages = []
        if primary_block.get("source") == "docs":
            followup_pages = _topic_followup_pages(query, prod)
        if followup_pages:
            added_paths = {b.get("path") for b in blocks}
            for page in followup_pages:
                if len(blocks) >= 4:
                    break
                match = next((r for r in pools.get("docs", [])
                              if r.get("page") == page
                              and r.get("product") == primary_block.get("product")
                              and r.get("path") != primary_block.get("path")), None)
                if not match:
                    try:
                        match = _get_docs_source().get_by_path(
                            f"{primary_block.get('product')}__{page}")
                    except Exception as e:
                        print(f"[docs] подблок {page}: {e}", file=sys.stderr)
                        match = None
                if not match or match.get("path") in added_paths:
                    continue
                m = dict(match)
                if m.get("content_full"):
                    m["content"] = m.pop("content_full")
                blocks.append(m)
                added_paths.add(match.get("path"))

        # Адаптивные заголовки: «модуль · действие» + роль и номер шага по
        # карточке (данные для интерфейсов, скрытый порядок не меняется).
        enrich_block_headings(blocks)

        if blocks:
            if prod != "general":
                answer_title = PRODUCT_DISPLAY_NAME.get(prod, prod)
            elif blocks[0].get("display_title"):
                answer_title = blocks[0]["display_title"]
            else:
                answer_title = blocks[0].get("title", "")
            answer = {
                "title": answer_title,
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
                        # (собраны поиском, но не вошли в блок, включая
                        # фрагменты-дополнения вроде «Правил… по СНО»).
                        block_paths = {d.get("path") for d in blocks}
                        sec_docs = [r for r in pools.get("docs", [])
                                    if r.get("path") != b.get("path")
                                    and "," not in (r.get("product") or "")
                                    and r.get("path") not in block_paths]
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
                        for rd in _docs_fts_related(_related_query(query), b.get("product"),
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

def cross_search(query, max_answers=2, limit_per_source=5):
    """Единый поиск из 3 источников.

    Возвращает dict с ключами:
      - answers: list[Answer] — структурированные ответы
      - intent: str — определённый интент
      - product: str|None — определённый продукт
      - latency_ms: int — время выполнения

    Результаты НЕ кэшируются в LRU: деградированный ответ (гибрид не успел за
    таймаут и результаты взяты из FTS-only fallback) нельзя замораживать —
    идентичный запрос под нагрузкой должен пересчитываться заново. При тёплом
    гибриде запрос занимает доли секунды, поэтому после потери кэша API не
    деградирует.
    """
    _ensure_index_fresh()
    t0 = time.time()
    result = _cross_search_impl(query, max_answers, limit_per_source)
    result.pop("_degraded", None)
    latency_ms = int((time.time() - t0) * 1000)
    return result


_LAST_IDX_MTIME = None


def _ensure_index_fresh():
    """Сброс кэша заголовков страниц при пересборке индекса.

    _page_title без инвалидации отдавал бы устаревшие данные до
    перезапуска/вытеснения. Индекс (cache/kb_index.db) пересобирается
    build_index по WatchPaths — сверяем mtime на каждый запрос.
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
            asks_changes = _asks_for_changes(query)
            rows = ds.search(query, None, limit=max(limit_per_source, DOCS_MERGE_TOP))
            if asks_changes:
                # Для вопросов про изменения/версии добавляем changelog-страницы,
                # которые могли выпасть из пула из-за автодетекта продукта:
                #   «Parts.Intellect» → parts-intellect-guide → changelog исключён.
                # Подмешиваем changelog-продукт (-changes) отдельным поиском.
                try:
                    from search import detect_product_name as _dpn
                    detected = _dpn(query)
                except Exception:
                    detected = None
                if detected and detected.endswith("-guide"):
                    changes_product = detected[:-len("-guide")] + "-changes"
                    try:
                        add = ds.search(query, changes_product, limit=20)
                        seen = {(r.get("product"), r.get("page")) for r in rows}
                        for r in add:
                            if (r.get("product"), r.get("page")) not in seen:
                                rows.append(r)
                    except Exception as e:
                        print(f"[docs] changelog-дополнение: {e}", file=sys.stderr)
                # Для вопросов «когда/в какой версии реализовано» changelog —
                # это и есть прямой ответ, а не справочная страница. Ставим
                # перечисленные версии в топ пула, чтобы они не отставали от
                # guide-страниц по позиционному скору merge_results.
                if _changes_are_answer(query):
                    rows = _rank_changelog(rows, query)
                return rows
            # Подавляем changelog-шум: страницы *-changes попадают в топ для
            # обычных запросов («как направить seo» → «Версия 6.64»).
            # Кросс-продуктовые «comma»-строки НЕ вырезаем из пула: их удаление
            # сдвигает позиционные скоры и ломает порядок групп (например, у
            # «печать чеков» Sync обгонял Parts.Resource в top-2). Отсеиваем их
            # при компоновке блоков (см. compose_answers), сохраняя ранги.
            filtered = [r for r in rows
                        if not ("-changes" in (r.get("product") or ""))]
            rows = filtered or rows

            # Тематическая экспансия: детектированный feature-продукт не сужает
            # тему — страницы «график поставок»/«печать чеков» есть и в
            # Parts.Resource, и в Parts.Intellect. Дополняем пул так, чтобы у
            # остальных продуктов появились свои карточки ниже lead-карточки.
            dominant = None
            if rows:
                counts = {}
                for r in rows:
                    c = _normalize_product("docs", r.get("product"))
                    counts[c] = counts.get(c, 0) + 1
                dominant = max(counts, key=counts.get)

            def _append_unseen(add, flag=None):
                seen = {(r.get("product"), r.get("page")) for r in rows}
                for r in add:
                    if "-changes" in (r.get("product") or ""):
                        continue
                    if (r.get("product"), r.get("page")) in seen:
                        continue
                    if dominant is not None \
                            and _normalize_product("docs", r.get("product")) == dominant:
                        continue
                    if flag:
                        r["cross_expand"] = flag
                    rows.append(r)

            # Feature-продукт: широкий поиск по запросу (без авто-детекта).
            if (product or None) in _FEATURE_CROSS_PRODUCTS:
                try:
                    wide = ds.search(query, None,
                                     limit=max(limit_per_source, DOCS_MERGE_TOP),
                                     _skip_auto_product=True)
                except Exception as e:
                    print(f"[docs] расширение пула: {e}", file=sys.stderr)
                    wide = []
                _append_unseen(wide)

            # Темы, пересекающие продукты, широким поиском по шумному запросу не
            # находятся (для «печати чеков» выдаёт «Создание нового заказа
            # клиента»), поэтому ищем по самой теме в «другом» продукте.
            # Темы, пересекающие продукты, широким поиском по шумному запросу не
            # находятся (для «печати чеков» выдаёт «Создание нового заказа
            # клиента»), поэтому ищем по самой теме в «другом» продукте.
            other = _cross_topic_other(query)
            if other:
                other_pid = other.get(dominant)
                if other_pid:
                    try:
                        add = ds.search("печать чеков", other_pid, limit=4)
                    except Exception as e:
                        print(f"[docs] тема другого продукта: {e}", file=sys.stderr)
                        add = []
                    # Флаг cross_expand: merge_results не обрезает эти строки по
                    # max_docs (они всегда идут в хвосте пула), а compose не даёт
                    # решению «обогнать» карточку документации по теме.
                    _append_unseen(add, flag="chek")
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
    degraded_docs = False
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
                    # Служебный флаг деградации — для решения о кэшировании,
                    # в строки-блоки попадать не должен.
                    degraded_docs = any(r.get("_degraded") for r in doc_results)
                    for r in doc_results:
                        r.pop("_degraded", None)
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
        # Деградация: хотя бы один docs-пул получен через FTS-only fallback
        # (гибрид не успел за таймаут). Такой результат кэшировать нельзя.
        "_degraded": degraded_docs,
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
