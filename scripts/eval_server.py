#!/usr/bin/env python3
"""Страница сравнения выдачи: FTS / Vector / Hybrid.

Сервер на stdlib. Раздаёт scripts/eval_page.html и API /api/compare.

  python3 eval_server.py [--port 8055]
"""
import argparse
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

PAGE_PATH = os.path.join(SCRIPT_DIR, "eval_page.html")
HYBRID_PAGE_PATH = os.path.join(SCRIPT_DIR, "hybrid_page.html")
PRODUCTS_ROOT = os.path.join(os.path.dirname(SCRIPT_DIR), "products")

from search import (  # noqa: E402
    PRODUCTS, clean_snippet, load_chunks, search, search_hybrid, terms,
    vector_main,
)
import embed  # noqa: E402
from config import get_embed_host  # noqa: E402
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
        "content_html": md_to_html(content, product),
    }


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
    """RRF-слияние из уже готовых FTS-строк и Typesense-хитов (без повторного embed)."""
    from search import fallback_like, normalize_terms, terms, _discriminators, _page_has_terms

    rrf, order = {}, {}

    def add(key, rank):
        rrf.setdefault(key, 0.0)
        rrf[key] += 1.0 / (RRF_K + rank + 1)
        order.setdefault(key, 0)

    vec_keys = {}
    for i, r in enumerate(fts_rows):
        add((r[0], r[1]), i)
        order[(r[0], r[1])] = i

    # Дискриминативные термины запроса (специфика vs generic-«выгрузка»).
    candidates = list(dict.fromkeys(
        [(r[0], r[1]) for r in fts_rows] +
        [(h.get("product"), h.get("page")) for h in vec_hits]
    ))
    rare = _discriminators(normalize_terms(terms(query)), candidates)

    boost = 12 if rare else 0
    for i, h in enumerate(vec_hits[:15]):
        key = (h.get("product"), h.get("page"))
        vec_keys[key] = h
        eff_rank = i
        if boost:
            eff_rank = max(0, i - boost) if _page_has_terms(key[0], key[1], rare) \
                else i + boost
        add(key, eff_rank)
        if key not in order:
            order[key] = len(fts_rows) + i

    merged = sorted(rrf.items(), key=lambda kv: (-kv[1], order[kv[0]]))

    # Обрезание до релевантных (содержат дискриминативный термин).
    if rare:
        merged = [kv for kv in merged if _page_has_terms(kv[0][0], kv[0][1], rare)]
        if len(merged) < 2:
            merged = sorted(rrf.items(), key=lambda kv: (-kv[1], order[kv[0]]))
    merged = merged[:limit]

    fts_by = {(r[0], r[1]): r for r in fts_rows}
    out = []
    for key, _score in merged:
        if key in fts_by:
            out.append(fts_by[key])
        elif key in vec_keys:
            h = vec_keys[key]
            out.append((h.get("product"), h.get("page"), h.get("title") or "",
                        h.get("path"), "", h.get("_score", 0)))
    if not out:
        return fallback_like(terms(query), product, limit), 0.0
    #время = только merge + embed, без повторного vector_search
    return out, 0.0


def run_compare(query: str, top: int, product) -> dict:
    ts_up, oll_up = service_status()
    svc_ok = ts_up and oll_up

    t0 = time.perf_counter()
    fts_rows, fts_ms = search(terms(query), product, limit=top, snippets=True)

    vec_rows, vec_ms, vec_source = [], 0.0, "unavailable"
    vec_hits_all = []
    if svc_ok:
        try:
            from typesense_client import vector_search
            vec_start = time.perf_counter()
            vec = embed.embed_text(query, host=get_embed_host())
            vec_hits_all = vector_search(vec, k=15, product=product)
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
    if svc_ok and vec_hits_all:
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

    def _send(self, code: int, ctype: str, body: str):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self._cors_headers()
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
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            if not os.path.exists(PAGE_PATH):
                self._send(500, "text/plain; charset=utf-8",
                           f"Нет файла страницы: {PAGE_PATH}")
                return
            with open(PAGE_PATH, encoding="utf-8") as f:
                self._send(200, "text/html; charset=utf-8", f.read())
            return

        if path in ("/hybrid", "/hybrid.html"):
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
                res = run_compare(query, top, None)
                self._json({
                    "ok": True,
                    "query": query,
                    "top": top,
                    "elapsed_ms_total": res["elapsed_ms_total"],
                    "services": res["services"],
                    "hybrid": res["modes"]["hybrid"],
                })
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
                self._json(run_page(product, page, chars))
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
            self._json(run_search_v1(query, mode, top, product))
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
                self._json(run_compare(query, top, product))
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
                self._json(run_page(product, page, chars))
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

        self._send(404, "text/plain; charset=utf-8", "Not found")


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