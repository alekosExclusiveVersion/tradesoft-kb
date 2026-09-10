#!/usr/bin/env python3
"""Оценка unified cross_search по эталонным запросам.

Для каждого запроса вызывается cross_search (или живой /api/answer) и
проверяется, встречается ли ожидаемая страница документации в ответе:
  - в блоках source=="docs" (path = "product__page.htm.md"),
  - в related_docs (path = "product__page.htm.md"),
  - ожидание "solution<ID>" — в решении блока/related_solutions.

Метрики: prec@1/3/5 по ответам, mrr, avg latency.

Использование:
  .venv/bin/python scripts/evaluate_cross.py [-f file] [--top N]
      [--http] [--base URL] [--json REPORT.json]
"""
import argparse
import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import cross_search  # noqa: E402


def collect_docs_keys(answer):
    """Возвращает (список docs-ключей, список solution-ключей) из ответа."""
    doc_keys, sol_keys = [], []
    for b in answer.get("blocks", []):
        if b.get("source") == "docs" and b.get("path"):
            doc_keys.append(b["path"])
        if b.get("source") == "solution":
            if b.get("path"):
                sol_keys.append(b["path"].replace("solution_", "solution"))
            if b.get("deal_id"):
                sol_keys.append(f"solution{b['deal_id']}")
    for rd in answer.get("related_docs", []):
        if rd.get("path"):
            doc_keys.append(rd["path"])
    for rs in answer.get("related_solutions", []):
        if rs.get("id"):
            sol_keys.append(f"solution{rs['id']}")
    return doc_keys, sol_keys


def hit(expected, docs_keys, sol_keys):
    """True, если любая ожидаемая страница/решение найдено в ответе."""
    return bool(expected & set(docs_keys + sol_keys))


def run_local(query, max_answers, limit):
    try:
        return cross_search.cross_search(query, max_answers=max_answers,
                                         limit_per_source=limit)
    except Exception as e:
        return {"answers": [], "error": str(e), "latency_ms": 0}


def run_http(query, max_answers, limit, base, timeout=30):
    import urllib.parse
    import urllib.request
    url = f"{base}/api/answer?q={urllib.parse.quote(query)}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"answers": [], "error": str(e), "latency_ms": 0}


def eval_file(args):
    fname = args.file if os.path.isabs(args.file) or os.path.exists(args.file) \
        else os.path.join(SCRIPT_DIR, args.file)
    data = json.load(open(fname, encoding="utf-8"))
    queries = data["queries"]
    print(f"Эталон: {fname}, запросов: {len(queries)}\n")

    runner = run_http if args.http else run_local
    top = args.top

    hits1 = hits3 = hits5 = mrr_sum = 0.0
    latencies, fails = [], []
    n = len(queries)

    for item in queries:
        res = runner(item["q"], top, args.limit,
                     args.base) if args.http else runner(item["q"], top, args.limit)
        answer_list = res.get("answers", [])
        lat = res.get("latency_ms")
        if lat is not None:
            latencies.append(lat)

        expected = set(item["expected"])
        positions = []
        for idx, ans in enumerate(answer_list[:top], 1):
            dk, sk = collect_docs_keys(ans)
            if hit(expected, dk, sk):
                positions.append(idx)
        p1 = bool(positions) and positions[0] == 1
        p3 = bool(positions) and positions[0] <= 3
        p5 = bool(positions) and positions[0] <= 5
        mrr = 1.0 / positions[0] if positions else 0.0
        hits1 += p1
        hits3 += p3
        hits5 += p5
        mrr_sum += mrr
        if not p1:
            first = answer_list[0] if answer_list else {}
            top_title = first.get("title", "")
            fails.append((item["q"], top_title[:60]))
            print(f"  NO-TOP1: {item['q'][:55]} | first='{top_title[:45]}'")

    n1 = hits1 / n if n else 0
    n3 = hits3 / n if n else 0
    n5 = hits5 / n if n else 0
    m = mrr_sum / n if n else 0
    avg = (sum(latencies) / len(latencies)) if latencies else 0

    print()
    print(f"  prec@1={n1:.3f}  prec@3={n3:.3f}  prec@5={n5:.3f}  "
          f"mrr={m:.3f}  avg_ms={avg:.0f}  fails={len(fails)}")

    report = {
        "mode": "http" if args.http else "local",
        "n": n,
        "prec@1": n1, "prec@3": n3, "prec@5": n5,
        "mrr": m, "avg_ms": avg, "fails": [f[0] for f in fails],
    }
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=1)
        print(f"  Отчёт: {args.json}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="eval_real.json")
    ap.add_argument("--top", type=int, default=5,
                    help="число ответов для проверки (max_answers)")
    ap.add_argument("--limit", type=int, default=5,
                    help="результаты на источник")
    ap.add_argument("--http", action="store_true",
                    help="вызывать живой /api/answer вместо импорта")
    ap.add_argument("--base", default="http://127.0.0.1:8055")
    ap.add_argument("--json", default="")
    ap.add_argument("--mode", default=None)
    args = ap.parse_args()
    eval_file(args)


if __name__ == "__main__":
    main()