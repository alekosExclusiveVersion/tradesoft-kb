#!/usr/bin/env python3
"""Unified Search API — единый API для кроссплатформенного поиска.

Предоставляет REST API для поиска по 3 источникам (docs/solutions/CRM)
с структурированными ответами.

Эндпоинты:
  GET /api/answer?q=...&max=2&product=auto
  GET /api/health
  GET /api/meta

Запуск:
  python3 api_server.py [--port 8055]
"""
import json
import os
import sys
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
KB_ROOT = os.path.dirname(SCRIPT_DIR)

sys.path.insert(0, SCRIPT_DIR)
from cross_search import cross_search
from intent import detect_intent, detect_product
from search import _canonical_query
import freshness
from access_log import AccessLogger, gen_session_id

PORT = int(os.environ.get("CROSS_SEARCH_PORT", 8055))

_access = AccessLogger()


class APIHandler(BaseHTTPRequestHandler):
    """HTTP handler для Unified Search API."""

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path == "/api/answer":
            self._handle_answer(params)
        elif path == "/api/page":
            self._handle_page(params)
        elif path == "/api/health":
            self._handle_health()
        elif path == "/api/meta":
            self._handle_meta()
        elif path == "/" or path == "/index.html":
            self._serve_static("unified.html", "text/html")
        else:
            self._send_json(404, {"error": "Not found"})

    def _handle_answer(self, params):
        """Поисковый запрос → структурированные ответы."""
        q = params.get("q", [""])[0].strip()
        if not q:
            self._send_json(400, {"error": "Missing 'q' parameter"})
            return

        max_answers = int(params.get("max", ["2"])[0])

        t0 = time.time()
        try:
            result = cross_search(q, max_answers=max_answers, limit_per_source=5)
        except Exception as e:
            print(f"[api] error: {e}", file=sys.stderr)
            import traceback
            traceback.print_exc(file=sys.stderr)
            self._send_json(500, {
                "error": str(e),
                "query": q,
                "latency_ms": int((time.time() - t0) * 1000),
            })
            return

        latency_ms = int((time.time() - t0) * 1000)

        self._log_search(q, result, latency_ms)

        response = {
            "query": q,
            "intent": result["intent"],
            "product": result["product"],
            "product_display": result.get("product_display"),
            "answers": result["answers"],
            "counts": result["counts"],
            "latency_ms": latency_ms,
        }

        self._send_json(200, response)

    def _log_search(self, q, result, latency_ms):
        """Пишет запрос в search_events (kb_access.db) для eval-conveyera."""
        try:
            canonical, corrected = _canonical_query(q)
            top = []
            for a in result.get("answers", []):
                for b in a.get("blocks", []):
                    p = b.get("path") or b.get("pp")
                    if p and p not in top:
                        top.append(p)
            _access.log_search(
                session_id=gen_session_id(), ip=self.client_address[0],
                q_raw=q, q_canonical=canonical,
                corrected_type="layout" if corrected else None,
                product_detected=result.get("product"),
                product_filter=None,
                n_results=len(top),
                top10_pp=json.dumps(top[:10], ensure_ascii=False),
                n_fts=0, n_vec=0, fts_ms=0.0, vec_ms=0.0, embed_ms=0.0,
                total_ms=float(latency_ms), vec_source=None,
                boost=0, rare=None, api_intent=result.get("intent"))
        except Exception as e:
            print(f"[api] log_search error: {e}", file=sys.stderr)

    def _handle_page(self, params):
        """Полный контент документа-страницы из kb_index.db.

        Параметры: path="product__page.htm.md" или product=...&page=...
        """
        import sqlite3
        path = params.get("path", [""])[0].strip()
        if path:
            product, _, page = path.partition("__")
        else:
            product = params.get("product", [""])[0].strip()
            page = params.get("page", [""])[0].strip()
        if not page:
            self._send_json(400, {"error": "Missing 'path' or 'page'"})
            return
        db = os.path.join(KB_ROOT, "cache", "kb_index.db")
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                if product:
                    row = con.execute(
                        "SELECT title, content FROM pages WHERE product=? AND page=? "
                        "ORDER BY chunk LIMIT 1", (product, page)).fetchone()
                else:
                    row = con.execute(
                        "SELECT title, content FROM pages WHERE page=? "
                        "ORDER BY chunk LIMIT 1", (page,)).fetchone()
            finally:
                con.close()
        except Exception as e:
            self._send_json(500, {"error": str(e)})
            return
        if not row:
            self._send_json(404, {"error": "Page not found"})
            return
        self._send_json(200, {
            "path": path or (product + "__" + page),
            "product": product,
            "page": page,
            "title": row[0],
            "content": row[1],
        })

    def _handle_health(self):
        """Health check (+ признаки устаревания стемминг-индекса)."""
        health = {
            "ok": True,
            "service": "cross-search-api",
            "port": PORT,
        }
        try:
            fp, meta, mtime, reasons = freshness.status()
            stale = bool(reasons)
            health["stale"] = stale
            health["index_mtime"] = mtime
            health["meta"] = {
                "fingerprint": (fp or "")[:16] + "…",
                "built_at": (meta or {}).get("built_at"),
                "index_mtime": (meta or {}).get("index_mtime"),
            }
            if reasons:
                health["reasons"] = reasons
        except Exception as e:
            health["stale"] = None
            health["reasons"] = [f"ошибка проверки: {e}"]
        self._send_json(200, health)

    def _handle_meta(self):
        """Мета-данные API."""
        from intent import Intent
        self._send_json(200, {
            "service": "cross-search-api",
            "version": "0.1.0",
            "intents": list(Intent.ORDER.keys()),
            "intent_order": Intent.ORDER,
            "sources": ["docs", "solutions", "crm"],
        })

    def _send_json(self, status, data):
        """Отправить JSON ответ."""
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, filename, content_type):
        """Отдать статический файл."""
        templates_dir = os.path.join(SCRIPT_DIR, "templates")
        filepath = os.path.join(templates_dir, filename)
        if not os.path.exists(filepath):
            self._send_json(404, {"error": "File not found"})
            return
        with open(filepath, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Pragma", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        """Кастомное логирование."""
        if "/api/" in str(args[0]):
            sys.stderr.write(f"[{self.log_date_time_string()}] {format % args}\n")


def _warmup():
    """Однократный поисковый запрос до начала обслуживания.

    Источники cross_search инициализируются лениво; первый запрос под
    одновременной нагрузкой (corp-домен) может завершиться деградированным
    результатом, который затем вечно отдаётся из LRU-кэша. Прогрев выполняет
    инициализацию в одиночном потоке до приёма внешних запросов.
    """
    try:
        cross_search("прогрев кэша", max_answers=1, limit_per_source=5)
    except Exception as e:
        print(f"[warmup] error: {e}", file=sys.stderr)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Unified Search API")
    parser.add_argument("--port", type=int, default=PORT, help="Порт")
    args = parser.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), APIHandler)
    _warmup()
    print(f"Unified Search API запущен на http://127.0.0.1:{args.port}")
    print(f"Эндпоинты:")
    print(f"  GET /api/answer?q=...&max=2")
    print(f"  GET /api/health")
    print(f"  GET /api/meta")
    sys.stdout.flush()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановка...")
        server.shutdown()


if __name__ == "__main__":
    main()
