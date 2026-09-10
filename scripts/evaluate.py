"""Харнесс оценки точности поиска.

Сравнивает режимы fts / vector / hybrid на эталонном наборе запросов
(eval_queries.json). Для каждого запроса считает точность top-1/3/5 (попадание
в любой из expected-ключей «product__page») и MRR, а также среднее время.

Usage:
    python3 scripts/evaluate.py [--top N] [--json REPORT.json] [--mode fts|vector|hybrid]
"""
import argparse
import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from search import search, search_hybrid, vector_main, terms  # noqa: E402


def hit(expected, product, page):
    for p in product.split(","):
        if f"{p}__{page}" in expected:
            return True
    return False


def run_query(query, mode, limit, timeout_ms=15000):
    """Возвращает (keys_top_down, elapsed_ms).

    Выполняет поиск в отдельном потоке с таймаутом: зависший запрос
    (напр. очень длинный эмбеддинг) не должен блокировать весь eval.
    """
    import threading
    holder = [([], None)]

    def _run():
        t0 = __import__("time").perf_counter()
        try:
            if mode == "fts":
                rows, _ = search(terms(query), None, limit=limit)
            elif mode == "vector":
                rows, _ = vector_main(query, None, limit=limit)
            else:
                rows, _ = search_hybrid(query, None, limit=limit)
        except Exception:
            holder[0] = ([], None)
            return
        elapsed = (__import__("time").perf_counter() - t0) * 1000
        keys = [(r[0], r[1]) for r in rows]
        holder[0] = (keys, elapsed)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=timeout_ms / 1000.0)
    keys, elapsed = holder[0]
    if elapsed is None:
        print(f"  [timeout] {query[:60]}", file=sys.stderr)
    return keys, elapsed


def eval_mode(mode, queries, limit):
    tot_t1 = tot_t3 = tot_t5 = tot_mrr = 0.0
    times = []
    n = len(queries)
    fails = []
    for item in queries:
        expected = set(item["expected"])
        keys, el = run_query(item["q"], mode, limit=limit)
        if el is not None:
            times.append(el)
        t1 = any(hit(expected, p, pg) for p, pg in keys[:1])
        t3 = any(hit(expected, p, pg) for p, pg in keys[:3])
        t5 = any(hit(expected, p, pg) for p, pg in keys[:5])
        mrr = 0.0
        for rk, (p, pg) in enumerate(keys[:10], 1):
            if hit(expected, p, pg):
                mrr = 1.0 / rk
                break
        tot_t1 += t1
        tot_t3 += t3
        tot_t5 += t5
        tot_mrr += mrr
        if not t1:
            fails.append((item["q"], [f"{p}__{pg}" for p, pg in keys[:3]]))
    return {
        "mode": mode,
        "n": n,
        "prec@1": tot_t1 / n,
        "prec@3": tot_t3 / n,
        "prec@5": tot_t5 / n,
        "mrr": tot_mrr / n,
        "avg_ms": (sum(times) / len(times)) if times else None,
        "top1_fails": fails,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--json", default="", help="путь для сохранения JSON-отчёта")
    ap.add_argument("--mode", choices=["fts", "vector", "hybrid"], default=None)
    ap.add_argument("--file", default="eval_queries.json",
                    help="файл с эталонными запросами (по умолч. eval_queries.json)")
    args = ap.parse_args()

    qfile = os.path.join(SCRIPT_DIR, args.file)
    data = json.load(open(qfile, encoding="utf-8"))
    queries = data["queries"]
    print(f"Эталон: {args.file}, запросов: {len(queries)}\n")

    modes = [args.mode] if args.mode else ["fts", "vector", "hybrid"]
    results = []
    for mode in modes:
        res = eval_mode(mode, queries, args.top)
        results.append(res)
        print(f"[{mode.upper()}]")
        print(f"  prec@1={res['prec@1']:.3f}  prec@3={res['prec@3']:.3f}  "
              f"prec@5={res['prec@5']:.3f}  mrr={res['mrr']:.3f}  "
              f"avg_ms={res['avg_ms']:.0f}")
        for q, top3 in res["top1_fails"]:
            print(f"    NO-TOP1: {q}  -> {top3}")
        print()

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=1)
        print(f"Отчёт сохранён: {args.json}")


if __name__ == "__main__":
    main()
