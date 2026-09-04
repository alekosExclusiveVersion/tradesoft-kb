#!/usr/bin/env python3
"""Гибридный поиск (страница) + API.

Сервер на stdlib. Отдаёт scripts/hybrid_page.html на корне и API
(внутренний /api/compare для диагностики выдачи).

  python3 eval_server.py [--port 8055]
"""
import argparse
import hmac
import json
import os
import re
import socket
import sys
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

HYBRID_PAGE_PATH = os.path.join(SCRIPT_DIR, "hybrid_page.html")
PRODUCTS_ROOT = os.path.join(os.path.dirname(SCRIPT_DIR), "products")

from search import (  # noqa: E402
    PRODUCTS, clean_snippet, load_chunks, search, search_hybrid, terms,
    vector_main, detect_product_name, _layout_variant, _result_coverage,
    _canonical_query, web_payment_product,
)
import embed  # noqa: E402
from config import get_embed_host  # noqa: E402
from config import ACCESS_TOKEN, access_is_enabled  # noqa: E402
import access_log as _al  # noqa: E402
from typesense_client import get_config  # noqa: E402
import sqlite3  # noqa: E402

from search import DB_PATH  # noqa: E402


def service_status() -> tuple[bool, bool]:
    """(typesense_up, ollama_up)."""
    ts_up = False
    try:
        req = urllib.request.urlopen(
            get_config()["typesense_host"] + "/health", timeout=2
        )
        ts_up = req.status == 200
    except Exception:
        ts_up = False
    return ts_up, embed.ollama_is_available()


def preview_for(product: str, page: str) -> str:
    """Короткий preview текста страницы для векторных результатов без сниппета."""
    try:
        content = load_chunks(product, page, max_chars=600)
    except Exception:
        return ""
    return clean_snippet(content)


def pt(text: str) -> str:
    """Инлайн-разметка одной строки (уже escaped)."""
    t = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    t = re.sub(r"[*_]{2,}", "", t)
    return t.strip()


READ_ALSO_RE = re.compile(r"^(читайте также|смотрите также|см\.\s*также)\b", re.I)


def md_to_html(text: str, product: str | None = None) -> str:
    """Markdown → HTML для читаемого отображения полного текста страницы.

    Скриншоты ![](images/... ) превращаются в <img> с отдачей файла через
    /api/image (файлы лежат в products/<product>/images/<rel>).
    """
    import html as htmlmod
    t = htmlmod.escape(text or "", quote=False)

    def img_repl(m):
        alt, rel = m.group(1), m.group(2).strip()
        if not product or not rel:
            return ""
        rel_enc = urllib.parse.quote(rel, safe="/")
        prod_enc = urllib.parse.quote(product, safe="")
        return (f'<img class="shot" loading="lazy" '
                f'src="/api/image?product={prod_enc}&amp;rel={rel_enc}" alt="{alt}">')

    t = re.sub(r"!\[([^\]]*)\]\(([^)]*)\)", img_repl, t)
    out = []
    ul_open = False
    related = False

    def close_list():
        nonlocal ul_open
        if ul_open:
            out.append("</ul>")
            ul_open = False

    def close_related():
        nonlocal related
        if related:
            close_list()
            out.append("</div>")
            related = False

    def related_item(display_txt: str, raw: str):
        """Пункт «Читайте также» как кликабельная ссылка."""
        nonlocal ul_open
        if not ul_open:
            out.append('<ul class="ra">')
            ul_open = True
        attr = raw.replace('"', "&quot;")
        out.append(f'<li><a href="#" class="more" data-more="{attr}">{display_txt}</a></li>')

    lines = t.splitlines()
    i = 0
    n = len(lines)
    while i < n:
        s = lines[i].strip()
        if not s:
            if ul_open:
                close_related()
            i += 1
            continue
        # -- GFM-таблица: строка начинается с '|' и за ней строки таблицы ----
        if s.startswith("|"):
            block = []
            while i < n and lines[i].strip().startswith("|"):
                block.append(lines[i].strip())
                i += 1
            tbl_html = _table_to_html(block)
            if tbl_html:
                close_related()
                close_list()
                out.append(tbl_html)
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", s)
        if m:
            lvl, txt = len(m.group(1)), m.group(2)
            if READ_ALSO_RE.match(re.sub(r"<[^>]+>", "", txt).strip()):
                close_list()
                out.append('<div class="read-also"><div class="ra-title">Читайте также</div>')
                related = True
                i += 1
                continue
            close_related()
            h = 1 if lvl == 1 else 2 if lvl == 2 else 3
            out.append(f"<h{h}>{pt(txt)}</h{h}>")
            i += 1
            continue
        if re.fullmatch(r"[-*_]{3,}", s):
            close_related()
            out.append("<hr>")
            i += 1
            continue
        m = re.match(r"^[-*]\s+(.*)$", s)
        if m:
            item = m.group(1)
            if related:
                related_item(pt(item), item)
            else:
                if not ul_open:
                    out.append("<ul>")
                    ul_open = True
                out.append(f"<li>{pt(item)}</li>")
            i += 1
            continue
        m = re.match(r"^\d+[.)]\s+(.*)$", s)
        if m:
            close_related()
            out.append(f"<ol><li>{pt(m.group(1))}</li></ol>")
            i += 1
            continue
        ptxt = pt(s)
        if not ptxt:
            if ul_open:
                close_related()
            i += 1
            continue
        if related:
            related_item(ptxt, s)
            i += 1
            continue
        if READ_ALSO_RE.match(re.sub(r"<[^>]+>", "", ptxt).strip()):
            close_list()
            out.append('<div class="read-also"><div class="ra-title">Читайте также</div>')
            related = True
            i += 1
            continue
        close_list()
        out.append(f"<p>{ptxt}</p>")
        i += 1
    if related:
        close_related()
    else:
        close_list()
    return "\n".join(out)


def _table_to_html(block: list[str]) -> str:
    """GFM markdown-таблицу (список строк '| ... |') превращает в <table>."""
    rows = []
    for raw in block:
        s = raw.strip()
        if not s.startswith("|") or not s.endswith("|"):
            continue
        body = s[1:-1]
        cells = [c.strip() for c in body.split("|")]
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
            continue  # строка-разделитель
        rows.append(cells)
    if not rows:
        return ""
    cols = max(len(r) for r in rows)
    rows = [r + [""] * (cols - len(r)) for r in rows]
    header = rows[0]
    body = rows[1:]
    out_h = ""
    if not all(c in ("&nbsp;", "&amp;nbsp;") for c in header):
        out_h = "<thead><tr>" + "".join(
            f"<th>{_cell_html(c)}</th>" for c in header
        ) + "</tr></thead>"
    out_b = "<tbody>"
    for row in body:
        if all(c in ("", "&nbsp;", "&amp;nbsp;") for c in row):
            continue
        out_b += "<tr>" + "".join(f"<td>{_cell_html(c)}</td>" for c in row) + "</tr>"
    out_b += "</tbody>"
    return f"<table>{out_h}{out_b}</table>"


def _cell_html(c: str) -> str:
    """HTML ячейки таблицы; пустой плейсхолдер &nbsp; оставляем пустым."""
    if c in ("&nbsp;", "&amp;nbsp;"):
        return "&nbsp;"
    return pt(c)


def page_meta(product: str, page: str) -> tuple[str, str]:
    """(title, path) страницы из БД."""
    if not os.path.exists(DB_PATH):
        return page, ""
    db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        row = db.execute(
            "SELECT title, path FROM pages WHERE product=? AND page=? LIMIT 1",
            (product, page),
        ).fetchone()
    finally:
        db.close()
    return (row[0], row[1]) if row else (page, "")


def run_page(product: str, page: str, max_chars: int) -> dict:
    """Полный текст страницы для «углубления» в результат."""
    title, path = page_meta(product, page)
    content = load_chunks(product, page, max_chars=max_chars or None)
    content_html = md_to_html(content, product)
    related = _related_pages(product, page, limit=4)
    if related:
        items = []
        for rp, rt in related:
            items.append(
                f'<li><a href="#" class="more" data-product="{product}" '
                f'data-page="{rp}" data-title="{_attr(rt)}">{pt(rt)}</a></li>'
            )
        content_html += (
            '<div class="read-also"><div class="ra-title">Читайте также</div>'
            f'<ul class="ra">{"".join(items)}</ul></div>'
        )
    return {
        "ok": True,
        "product": product,
        "product_display": PRODUCTS.get(product, product),
        "page": page,
        "title": title or page,
        "path": path,
        "chars": len(content),
        "truncated": bool(max_chars) and len(content) > max_chars,
        "content": content,
        "content_html": content_html,
    }


def _attr(s: str) -> str:
    """Экранирование значения в кавычках HTML-атрибута."""
    return (s or "").replace("&", "&amp;").replace('"', "&quot;") \
        .replace("<", "&lt;").replace(">", "&gt;")


def _related_pages(product: str, page: str, limit: int = 4) -> list:
    """Связанные темы в том же продукте для секции «Читайте также».

    Ищем по заголовку текущей страницы в рамках product и исключаем саму
    страницу. Возвращает [(page, title)].
    """
    from search import search_hybrid, normalize_terms, terms, STOPWORDS
    title, _ = page_meta(product, page)
    words = [w for w in normalize_terms(terms(title)) if w not in STOPWORDS][:4]
    if not words or (len(words) == 1 and words[0] == "и"):
        qu = title or page
    else:
        qu = " ".join(words)
    out = []
    try:
        res = search_hybrid(qu, product=product, limit=limit + 4)
        hits = res[0] if isinstance(res, tuple) else res
        for r in hits:
            rp, rt = r[1], r[2] or r[1]
            if rp == page:
                continue
            if any(o[0] == rp for o in out):
                continue
            out.append((rp, rt))
            if len(out) >= limit:
                break
    except Exception:
        pass
    return out


def row_json(row, rank: int) -> dict:
    product, page, title, path, snippet, score = row
    text = clean_snippet(snippet) if snippet else preview_for(product, page)
    return {
        "product": product,
        "product_display": PRODUCTS.get(product, product),
        "page": page,
        "title": title or page,
        "path": path,
        "snippet": text,
        "score": float(score) if score is not None else 0.0,
        "rank": rank,
    }


RRF_K = 60  # дублируем из search.py для оффлайн-merge


def _hybrid_from_hits(query, fts_rows, vec_hits, product, limit=5):
    """Доверительное слияние из готовых FTS-строк и Typesense-хитов (без embed).

    Использует общий модуль fusion.fuse — тот же самый, что search_hybrid(),
    чтобы RRF-логика (терм-буст, API-intent, развязка ничьих по vec_score)
    не расходилась между локальным поиском и сервером.
    """
    from search import fallback_like, terms
    from fusion import fuse

    merge_keys, meta = fuse(query, fts_rows, vec_hits, product)
    merged = merge_keys[:limit]
    if product is None and merged:
        deduped = {}
        for key in merged:
            pg = key[1]
            if pg not in deduped:
                deduped[pg] = (key, {key[0]})
            else:
                deduped[pg][1].add(key[0])
        merged = [v[0] for v in deduped.values()]
        page_products = {k: sorted(v[1]) for k, v in deduped.items()}
    else:
        page_products = {}

    vec_keys = {}
    for h in vec_hits:
        vec_keys[(h.get("product"), h.get("page"))] = h

    fts_by = {(r[0], r[1]): r for r in fts_rows}
    out = []
    for key in merged:
        if key in fts_by:
            row = list(fts_by[key])
        elif key in vec_keys:
            h = vec_keys[key]
            row = [h.get("product"), h.get("page"), h.get("title") or "",
                   h.get("path"), "", h.get("_score", 0)]
        else:
            continue
        pg = key[1]
        if pg in page_products and len(page_products[pg]) > 1:
            row = list(row)
            row[0] = ",".join(page_products[pg])
            row[4] = ""
        out.append(tuple(row))
    if not out:
        return fallback_like(terms(query), product, limit), 0.0

    # Улучшаем сниппеты: если FTS вернул обрыв посреди документа (начинается
    # с '…'), перестраиваем связный сниппет по абзацам полного текста.
    # Для дедуплицированных строк (product через запятую) сниппет не строим.
    from search import smart_snippet, normalize_terms
    norm = normalize_terms(terms(query))
    fixed = []
    for (prod, pg, title, path, snip, score) in out:
        if "," in prod:
            fixed.append((prod, pg, title, path, "", score))
            continue
        if snip and not snip.lstrip().startswith("…"):
            fixed.append((prod, pg, title, path, snip, score))
            continue
        s = smart_snippet(prod, pg, norm)
        if s:
            fixed.append((prod, pg, title, path, s, score))
        else:
            fixed.append((prod, pg, title, path, snip, score))
    out = fixed

    #время = только merge + embed, без повторного vector_search
    return out, 0.0


def run_compare(query: str, top: int, product) -> dict:
    ts_up, oll_up = service_status()
    svc_ok = ts_up and oll_up

    # «Оплата/эквайринг на сайте» — тема Parts.Resource. Эмбеддинг однозначно
    # знает ответ (способы оплаты), а FTS5 без стемминга даёт лишь шум. Поэтому
    # направляем поиск в parts-resource-guide и в гибрид берём чисто векторный
    # топ — иначе несколько мусорных FTS-страниц через RRF глушат верные ответы.
    web_payment = False
    if product is None and web_payment_product(query):
        product = "parts-resource-guide"
        web_payment = True

    t0 = time.perf_counter()
    fts_rows, fts_ms = search(terms(query), product, limit=top, snippets=True)

    vec_rows, vec_ms, vec_source = [], 0.0, "unavailable"
    vec_hits_all = []
    if svc_ok:
        try:
            from typesense_client import vector_search
            vec_start = time.perf_counter()
            canonical, _ = _canonical_query(query)
            vec = embed.embed_text(canonical, host=get_embed_host())
            vec_hits_all = vector_search(vec, k=30, product=product)
            vec_ms = (time.perf_counter() - vec_start) * 1000
            vec_source = "full"
            vec_rows = [(
                h.get("product"), h.get("page"), h.get("title") or "",
                h.get("path"), "", h.get("_score", 0))
                for h in vec_hits_all[:top]]
        except Exception:
            vec_source = "unavailable"
            vec_ms = 0.0

    hyb_rows, hyb_ms, hyb_source = [], 0.0, "fallback"
    if web_payment and vec_hits_all:
        # Векторный топ внутри уже ограниченного продукта и есть верный ответ
        hyb_rows = vec_rows
        hyb_ms = vec_ms
        hyb_source = "full"
    elif svc_ok and vec_hits_all:
        try:
            hyb_rows, hyb_ms = _hybrid_from_hits(
                query, fts_rows, vec_hits_all, product, limit=top)
            hyb_source = "full"
        except Exception as e:
            hyb_source = "unavailable"
            hyb_ms = 0.0
    else:
        hyb_rows, hyb_ms = search(terms(query), product, limit=top, snippets=True)

    total_ms = (time.perf_counter() - t0) * 1000

    modes = {
        "fts": {"elapsed_ms": fts_ms, "source": "full",
                "rows": [row_json(r, i) for i, r in enumerate(fts_rows, 1)]},
        "vector": {"elapsed_ms": vec_ms, "source": vec_source,
                   "rows": [row_json(r, i) for i, r in enumerate(vec_rows, 1)]},
        "hybrid": {"elapsed_ms": hyb_ms, "source": hyb_source,
                   "rows": [row_json(r, i) for i, r in enumerate(hyb_rows, 1)]},
    }

    keys = {
        "fts": [f"{r[0]}__{r[1]}" for r in fts_rows],
        "vector": [f"{r[0]}__{r[1]}" for r in vec_rows],
        "hybrid": [f"{r[0]}__{r[1]}" for r in hyb_rows],
    }

    return {
        "ok": True,
        "query": query,
        "top": top,
        "product": product,
        "elapsed_ms_total": total_ms,
        "services": {"typesense": ts_up, "ollama": oll_up},
        "modes": modes,
        "keys": keys,
    }


def run_search_v1(query: str, mode: str = "hybrid", top: int = 10,
                  product: str | None = None) -> dict:
    """Унифицированный поиск для API v1.

    Возвращает: {ok, query, mode, total, elapsed_ms, services, results:[...]}
    mode: hybrid | fts | vector. top: 1..50.
    """
    mode = (mode or "hybrid").lower()
    if mode not in ("hybrid", "fts", "vector"):
        return {"ok": False, "error": f"Неизвестный режим: {mode}"}
    if product is not None and product not in PRODUCTS:
        return {"ok": False, "error": f"Неизвестный продукт: {product}"}
    top = max(1, min(int(top), 50))
    if not query or not query.strip():
        return {"ok": False, "error": "Пустой запрос"}

    ts_up, oll_up = service_status()
    t0 = time.perf_counter()
    try:
        if mode == "fts":
            rows, _ms = search(terms(query), product, limit=top, snippets=True)
            source = "full"
        elif mode == "vector":
            rows, _ms = vector_main(query, product, limit=top,
                                    embed_host=get_embed_host())
            source = "full" if rows else "unavailable"
        else:  # hybrid
            rows, _ms = search_hybrid(query, product, limit=top, snippets=True,
                                      embed_host=get_embed_host())
            source = "full"
    except Exception:
        rows, source = [], "unavailable"

    elapsed_ms = (time.perf_counter() - t0) * 1000

    return {
        "ok": True,
        "query": query,
        "mode": mode,
        "total": len(rows),
        "elapsed_ms": round(elapsed_ms, 1),
        "source": source,
        "services": {"typesense": ts_up, "ollama": oll_up},
        "results": [row_json(r, i) for i, r in enumerate(rows, 1)],
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    # ---------------- логирование обращений ----------------
    # Аккуратная ленивая инициализация аттрибутов на 1-й запрос соединения.
    def _session_id(self):
        if getattr(self, "_sid", None) is None:
            self._sid = None
            self._new_session = False
            try:
                from access_log import gen_session_id
                import re
                raw = self.headers.get("Cookie", "") or ""
                m = re.search(r"(?:^|;\s*)sid=([0-9a-f]+)", raw)
                if m:
                    self._sid = m.group(1)
                else:
                    self._sid = gen_session_id()
                    self._new_session = True
            except Exception:
                self._sid = "sess"
        return self._sid

    def _meta(self):
        ip = ""
        try:
            ip = self.client_address[0] if self.client_address else ""
        except Exception:
            ip = ""
        ua = self.headers.get("User-Agent", "") or ""
        return ip, ua

    def _emit_access(self, status, latency_ms, n_results=None):
        try:
            from access_log import get_logger
            ip, ua = self._meta()
            get_logger().log_access(
                ip=ip, session_id=self._session_id(), user_agent=ua,
                path=self.path.split("?")[0], query_raw=self.path,
                status=status, latency_ms=latency_ms, n_results=n_results)
            get_logger().touch_session(
                session_id=self._session_id(), ip=ip, user_agent=ua)
        except Exception:
            pass

    def _emit_search(self, q_raw, q_canonical, corrected_type, product_detected,
                     product_filter, n_results, top10_pp, n_fts, n_vec,
                     fts_ms, vec_ms, embed_ms, total_ms, vec_source,
                     boost, rare, api_intent):
        try:
            from access_log import get_logger
            ip, ua = self._meta()
            get_logger().log_search(
                session_id=self._session_id(), ip=ip, q_raw=q_raw,
                q_canonical=q_canonical, corrected_type=corrected_type,
                product_detected=product_detected,
                product_filter=product_filter, n_results=n_results,
                top10_pp=top10_pp, n_fts=n_fts, n_vec=n_vec,
                fts_ms=fts_ms, vec_ms=vec_ms, embed_ms=embed_ms,
                total_ms=total_ms, vec_source=vec_source,
                boost=boost, rare=rare, api_intent=api_intent)
        except Exception:
            pass

    def _emit_open(self, product, page, title=None):
        try:
            from access_log import get_logger
            ip, ua = self._meta()
            seid = get_logger().last_search_event_id(self._session_id())
            get_logger().log_event(
                session_id=self._session_id(), ip=ip, type_="open",
                search_event_id=seid,
                pp=f"{product}__{page}", product=product, page=page)
        except Exception:
            pass

    def _emit_click(self, rank, pp, product, page, q):
        try:
            from access_log import get_logger
            ip, ua = self._meta()
            seid = get_logger().last_search_event_id(self._session_id())
            get_logger().log_event(
                session_id=self._session_id(), ip=ip, type_="click",
                search_event_id=seid,
                rank=rank, pp=pp, product=product, page=page, q=q)
        except Exception:
            pass

    def _log_search_from_result(self, query, res, product, used_layout_alt=False):
        """Пишет search_event по ответу run_compare (диагностика выдачи)."""
        try:
            from search import _canonical_query
            rows = res["modes"]["hybrid"]["rows"]
            n_results = len(rows)
            self._last_n = n_results
            top10_pp = [f"{r.get('product')}__{r.get('page')}" for r in rows[:10]]
            fts_ms = res["modes"]["fts"]["elapsed_ms"]
            vec_ms = res["modes"]["vector"]["elapsed_ms"]
            vec_source = res["modes"]["vector"].get("source", "unknown")
            n_fts = len(res["modes"]["fts"]["rows"])
            n_vec = len(res["modes"]["vector"]["rows"])

            canonical, changed = _canonical_query(query)
            if used_layout_alt:
                ctype = "layout"
            elif changed and canonical != query.lower():
                ctype = "typo"
            elif query != query.lower():
                ctype = "case"
            else:
                ctype = "none"

            self._emit_search(
                q_raw=query, q_canonical=canonical, corrected_type=ctype,
                product_detected=detect_product_name(query),
                product_filter=product, n_results=n_results,
                top10_pp=top10_pp, n_fts=n_fts, n_vec=n_vec,
                fts_ms=fts_ms, vec_ms=vec_ms, embed_ms=vec_ms,
                total_ms=res["elapsed_ms_total"], vec_source=vec_source,
                boost=0, rare=[], api_intent=False)
        except Exception:
            pass

    def _send(self, code: int, ctype: str, body: str):
        data = body.encode("utf-8")
        self._last_status = code
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self._cors_headers()
        # Выдаём cookie-сессию на первом ответе соединения.
        if getattr(self, "_new_session", False) and self._sid:
            try:
                from config import ACCESS_SESSION_DAYS
            except Exception:
                ACCESS_SESSION_DAYS = 30
            self.send_header(
                "Set-Cookie",
                f"sid={self._sid}; Path=/; HttpOnly; "
                f"Max-Age={ACCESS_SESSION_DAYS * 86400}")
        self.end_headers()
        self.wfile.write(data)

    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self._send(204, "text/plain", "")

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False, indent=2)
        self._send(code, "application/json; charset=utf-8", body)

    def do_GET(self):
        self._gt0 = time.perf_counter()
        self._last_status = 0
        self._last_n = None
        try:
            self._handle_get()
        except Exception as e:
            try:
                self._json({"ok": False, "error": str(e)}, 500)
            except Exception:
                pass
        finally:
            lat_ms = (time.perf_counter() - self._gt0) * 1000
            self._emit_access(self._last_status, lat_ms,
                              getattr(self, "_last_n", None))

    def _handle_get(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path

        if path in ("/", "/index.html", "/hybrid", "/hybrid.html"):
            if not os.path.exists(HYBRID_PAGE_PATH):
                self._send(500, "text/plain; charset=utf-8",
                           f"Нет файла страницы: {HYBRID_PAGE_PATH}")
                return
            with open(HYBRID_PAGE_PATH, encoding="utf-8") as f:
                self._send(200, "text/html; charset=utf-8", f.read())
            return

        if path == "/api/hybrid":
            qs = urllib.parse.parse_qs(parsed.query)
            query = (qs.get("q", [""])[0] or "").strip()
            if not query:
                self._json({"ok": False, "error": "Пустой запрос"}, 400)
                return
            try:
                top = max(1, min(int(qs.get("top", ["10"])[0]), 30))
            except ValueError:
                top = 10
            try:
                product = detect_product_name(query)
                res = run_compare(query, top, product)
                result = {
                    "ok": True,
                    "query": query,
                    "product": product,
                    "top": top,
                    "elapsed_ms_total": res["elapsed_ms_total"],
                    "services": res["services"],
                    "hybrid": res["modes"]["hybrid"],
                }
                # Fallback: если гибрид пуст или слаб (мало терминов в топе), а
                # запрос похож на русский в латинской раскладке — повторить по
                # восстановленной кириллице, если она заметно качественнее.
                hyb_rows = res["modes"]["hybrid"]["rows"]
                prim_cov = _result_coverage(query, hyb_rows) if hyb_rows else 0.0
                if product is None and (not hyb_rows or prim_cov < 0.34):
                    alt = _layout_variant(query)
                    if alt:
                        alt_res = run_compare(alt, top, None)
                        alt_rows = alt_res["modes"]["hybrid"]["rows"]
                        alt_cov = _result_coverage(alt, alt_rows) if alt_rows else 0.0
                        if alt_rows and (not hyb_rows or alt_cov >= prim_cov + 0.34):
                            res = alt_res
                            result["query"] = alt
                            result["hybrid"] = alt_res["modes"]["hybrid"]
                self._log_search_from_result(query, res, product,
                                             result["query"] != query)
                self._json(result)
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
            return

        # ---- API v1 (для интеграции в любые системы) ----
        if path == "/api/v1/products":
            self._json({"ok": True,
                        "products": [{"id": k, "display": v}
                                     for k, v in PRODUCTS.items()]})
            return

        if path == "/api/v1/document":
            qs = urllib.parse.parse_qs(parsed.query)
            product = qs.get("product", [""])[0] or None
            page = qs.get("page", [""])[0] or None
            if not product or not page:
                self._json({"ok": False, "error": "Нужны product и page"}, 400)
                return
            if product not in PRODUCTS:
                self._json({"ok": False, "error": f"Неизвестный продукт: {product}"}, 400)
                return
            try:
                chars = max(0, min(int(qs.get("chars", ["30000"])[0]), 200000))
            except ValueError:
                chars = 30000
            try:
                _p = run_page(product, page, chars)
                self._emit_open(product, page)
                self._json(_p)
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
            return

        if path == "/api/v1/search":
            qs = urllib.parse.parse_qs(parsed.query)
            query = (qs.get("q", [""])[0] or "").strip()
            mode = qs.get("mode", ["hybrid"])[0] or "hybrid"
            try:
                top = max(1, min(int(qs.get("top", ["10"])[0]), 50))
            except ValueError:
                top = 10
            product = qs.get("product", [""])[0] or None
            r = run_search_v1(query, mode, top, product)
            self._last_n = r.get("total", 0)
            try:
                from search import _canonical_query
                canonical, changed = _canonical_query(query)
                self._emit_search(
                    q_raw=query, q_canonical=canonical,
                    corrected_type="typo" if changed else "none",
                    product_detected=detect_product_name(query),
                    product_filter=product, n_results=r.get("total", 0),
                    top10_pp=[f"{x.get('product')}__{x.get('page')}"
                              for x in (r.get("results") or [])[:10]],
                    n_fts=0, n_vec=0, fts_ms=0.0, vec_ms=0.0, embed_ms=0.0,
                    total_ms=r.get("elapsed_ms", 0.0),
                    vec_source=r.get("source", "unknown"),
                    boost=0, rare=[], api_intent=False)
            except Exception:
                pass
            self._json(r)
            return

        if path == "/api/v1/health":
            ts_up, oll_up = service_status()
            self._json({
                "ok": True,
                "services": {"typesense": ts_up, "ollama": oll_up},
                "db": os.path.exists(DB_PATH),
            })
            return

        if path == "/api/compare":
            qs = urllib.parse.parse_qs(parsed.query)
            query = (qs.get("q", [""])[0] or "").strip()
            if not query:
                self._json({"ok": False, "error": "Пустой запрос"}, 400)
                return
            try:
                top = max(1, min(int(qs.get("top", ["5"])[0]), 20))
            except ValueError:
                top = 5
            product = qs.get("product", [""])[0] or None
            if product is not None and product not in PRODUCTS:
                self._json({"ok": False, "error": f"Неизвестный продукт: {product}"}, 400)
                return
            try:
                _r = run_compare(query, top, product)
                self._log_search_from_result(query, _r, product, False)
                self._json(_r)
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
            return

        if path == "/api/page":
            qs = urllib.parse.parse_qs(parsed.query)
            product = qs.get("product", [""])[0] or None
            page = qs.get("page", [""])[0] or None
            if not product or not page:
                self._json({"ok": False, "error": "Нужны product и page"}, 400)
                return
            if product not in PRODUCTS:
                self._json({"ok": False, "error": f"Неизвестный продукт: {product}"}, 400)
                return
            try:
                chars = max(0, min(int(qs.get("chars", ["30000"])[0]), 200000))
            except ValueError:
                chars = 30000
            try:
                _p = run_page(product, page, chars)
                self._emit_open(product, page)
                self._json(_p)
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
            return

        if path == "/api/image":
            qs = urllib.parse.parse_qs(parsed.query)
            product = qs.get("product", [""])[0] or None
            rel = qs.get("rel", [""])[0] or None
            if not product or not rel:
                self._send(400, "text/plain; charset=utf-8", "Нужны product и rel")
                return
            root = os.path.join(PRODUCTS_ROOT, product, "images")
            if rel.startswith("images/"):
                rel = rel[len("images/"):]
            candidate = os.path.realpath(os.path.join(root, rel))
            if not candidate.startswith(os.path.realpath(root) + os.sep):
                self._send(403, "text/plain; charset=utf-8", "Forbidden")
                return
            if not os.path.isfile(candidate):
                self._send(404, "text/plain; charset=utf-8", "Not found")
                return
            ctype = {
                ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".gif": "image/gif", ".svg": "image/svg+xml", ".webp": "image/webp",
            }.get(os.path.splitext(candidate)[1].lower(), "application/octet-stream")
            try:
                with open(candidate, "rb") as f:
                    data = f.read()
            except OSError:
                self._send(500, "text/plain; charset=utf-8", "Read error")
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "public, max-age=3600")
            self.end_headers()
            self.wfile.write(data)
            return

        if path == "/api/access":
            self._handle_access(parsed)
            return

        if path == "/api/rank":
            self._handle_rank(parsed)
            return

        self._send(404, "text/plain; charset=utf-8", "Not found")

    # ------------------------------------------------------------
    # POST /api/track — клик по результату (для оценки качества выдачи)
    # ------------------------------------------------------------
    def do_POST(self):
        self._gt0 = time.perf_counter()
        self._last_status = 0
        self._last_n = None
        try:
            parsed = urllib.parse.urlsplit(self.path)
            path = parsed.path
            if path == "/api/track":
                self._handle_track(parsed)
            else:
                self._json({"ok": False, "error": "unknown endpoint"}, 404)
        except Exception as e:
            try:
                self._json({"ok": False, "error": str(e)}, 500)
            except Exception:
                pass
        finally:
            lat_ms = (time.perf_counter() - self._gt0) * 1000
            self._emit_access(self._last_status, lat_ms, None)

    def _handle_track(self, parsed):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        body = b""
        if length > 0:
            body = self.rfile.read(length)
        payload = {}
        if body:
            try:
                payload = json.loads(body.decode("utf-8")) or {}
            except Exception:
                payload = {}
        rank = payload.get("rank")
        pp = payload.get("pp") or ""
        q = payload.get("q") or ""
        product, _, page = pp.partition("__")
        try:
            rank = int(rank) if rank is not None else None
        except (TypeError, ValueError):
            rank = None
        self._emit_click(rank, pp, product or None, page or None, q)
        self._json({"ok": True})

    # ------------------------------------------------------------
    # GET /api/rank — статус самообучающейся модели ранжирования
    # ------------------------------------------------------------
    def _handle_rank(self, parsed):
        try:
            from rank_model import dump_status, WEIGHTS_PATH
            import os
            st = dump_status()
            st["weights_path"] = WEIGHTS_PATH
            st["weights_file_exists"] = os.path.exists(WEIGHTS_PATH)
            self._json({"ok": True, "rank": st})
        except Exception as e:
            self._json({"ok": False, "error": str(e)}, 500)

    # ------------------------------------------------------------
    # GET /api/access — аналитика обращений (loopback / LAN / token)
    # ------------------------------------------------------------
    def _handle_access(self, parsed):
        if not access_is_enabled():
            self._json({"ok": False, "error": "access logging disabled"}, 403)
            return
        ip = self._meta()[0]
        ok = False
        try:
            if ip in ("127.0.0.1", "::1", "localhost") or ip in lan_ips():
                ok = True
        except Exception:
            ok = False
        qs = urllib.parse.parse_qs(parsed.query)
        tok = (qs.get("token", [""])[0] or "").strip()
        if not ok and tok and ACCESS_TOKEN and \
                hmac.compare_digest(tok, ACCESS_TOKEN):
            ok = True
        if not ok:
            self._json({"ok": False, "error": "forbidden"}, 403)
            return
        try:
            hours = max(1, min(int(qs.get("hours", ["24"])[0]), 24 * 365))
        except ValueError:
            hours = 24
        op = qs.get("op", ["summary"])[0] or "summary"
        limit = 50
        try:
            limit = max(1, min(int(qs.get("limit", ["50"])[0]), 500))
        except ValueError:
            pass
        handlers = {
            "summary": lambda: _al.summary(hours),
            "top_pages": lambda: _al.top_pages(hours, limit),
            "top_queries": lambda: _al.top_queries(hours, limit),
            "top_products": lambda: _al.top_products(hours, limit),
            "sessions": lambda: _al.sessions(hours, limit),
            "success": lambda: _al.success_metrics(hours),
            "raw": lambda: _al.raw(hours, limit),
        }
        fn = handlers.get(op)
        if fn is None:
            self._json({"ok": False, "error": f"unknown op: {op}"}, 400)
            return
        try:
            data = fn()
        except Exception as e:
            self._json({"ok": False, "error": str(e)}, 500)
            return
        self._json({"ok": True, "op": op, "hours": hours, "data": data})


def lan_ips() -> list[str]:
    """Локальные IPv4-адреса машины (без 127.0.0.1)."""
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.append(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        import subprocess
        for line in subprocess.run(
            ["ipconfig", "getifaddr", "en0"], capture_output=True, text=True
        ).stdout.split():
            if line.strip() and line.strip() not in ips:
                ips.append(line.strip())
    except Exception:
        pass
    return ips or ["127.0.0.1"]


def main():
    ap = argparse.ArgumentParser(description="Страница сравнения выдачи FTS/Vector/Hybrid")
    ap.add_argument("--port", type=int, default=8055)
    ap.add_argument("--host", default="0.0.0.0",
                    help="адрес прослушивания (по умолчанию 0.0.0.0 — доступен в сети)")
    args = ap.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Страница выдачи (сравнение): http://{args.host}:{args.port}/")
    print(f"Страница Hybrid:             http://{args.host}:{args.port}/hybrid")
    if args.host in ("0.0.0.0", ""):
        for ip in lan_ips():
            print(f"  в локальной сети: http://{ip}:{args.port}/")
            print(f"  в локальной сети: http://{ip}:{args.port}/hybrid")
    print("Ctrl+C для остановки")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()