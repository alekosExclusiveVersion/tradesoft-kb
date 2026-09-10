#!/usr/bin/env python3
"""Диагностика unified-cross_search по полному набору.

Для каждого запроса сохраняет: expected, top-1 answer (source + titles),
признак «top-1 содержит docs того же продукта» и т.п. — в JSON и краткий отчёт.
"""
import json
import sys
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import cross_search  # noqa: E402

OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/cross_fails.json"

data = json.load(open(os.path.join(SCRIPT_DIR, "eval_queries.json"), encoding="utf-8"))
queries = data["queries"]

res_meta = []
for item in queries:
    q = item["q"]
    exp = item["expected"]
    res = cross_search.cross_search(q, max_answers=5, limit_per_source=5)
    answers = res.get("answers", [])
    first = answers[0] if answers else {}
    blocks = first.get("blocks", [])
    first_srcs = [b.get("source") for b in blocks]
    first_paths = [(b.get("path") or "") for b in blocks]
    # docs, которые «ожидались» на первом месте
    any_exp_in_first = bool(set(exp) & set(first_paths))
    hit_pos = None
    for ai, a in enumerate(answers[:5], 1):
        paths = [b.get("path") for b in a.get("blocks", [])]
        paths += [r.get("path") for r in a.get("related_docs", [])]
        if set(exp) & set(paths):
            hit_pos = ai
            break
    res_meta.append({
        "q": q,
        "expected": exp,
        "first_title": first.get("title") or "",
        "first_blocks": [
            {"src": b.get("source"), "path": (b.get("path") or "")[:72],
             "title": (b.get("title") or "")[:52], "score": b.get("score")}
            for b in blocks[:3]],
        "first_sources": first_srcs,
        "any_exp_in_first": any_exp_in_first,
        "hit_pos": hit_pos,
        "detected_product": res.get("product"),
        "detected_intent": res.get("intent"),
    })

fails = [m for m in res_meta if not m["any_exp_in_first"]]
with open(OUT, "w", encoding="utf-8") as f:
    json.dump(res_meta, f, ensure_ascii=False, indent=1)

import collections
stat = collections.Counter()
for m in fails:
    srcs = tuple(sorted(m["first_sources"]))
    stat[srcs] += 1
print(f"Всего запросов: {len(res_meta)}, top-1 без expected: {len(fails)}")
print("Распределение top-1 по источникам:")
for k, v in stat.most_common():
    print(f"  {k}: {v}")
print(f"\nОтчёт: {OUT}")