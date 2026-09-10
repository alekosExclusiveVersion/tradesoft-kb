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

PORT = int(os.environ.get("CROSS_SEARCH_PORT", 8055))


class APIHandler(BaseHTTPRequestHandler):
    """HTTP handler для Unified Search API."""

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path == "/api/answer":
            self._handle_answer(params)
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
        product = params.get("product", ["auto"])[0]
        if product == "auto":
            product = None

        t0 = time.time()
        result = cross_search(q, max_answers=max_answers, limit_per_source=5)
        latency_ms = int((time.time() - t0) * 1000)

        response = {
            "query": q,
            "intent": result["intent"],
            "product": result["product"],
            "answers": result["answers"],
            "counts": result["counts"],
            "latency_ms": latency_ms,
        }

        self._send_json(200, response)

    def _handle_health(self):
        """Health check."""
        self._send_json(200, {
            "ok": True,
            "service": "cross-search-api",
            "port": PORT,
        })

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
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        """Кастомное логирование."""
        if "/api/" in str(args[0]):
            sys.stderr.write(f"[{self.log_date_time_string()}] {format % args}\n")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Unified Search API")
    parser.add_argument("--port", type=int, default=PORT, help="Порт")
    args = parser.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), APIHandler)
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
