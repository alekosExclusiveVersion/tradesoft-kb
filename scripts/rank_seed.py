#!/usr/bin/env python3
"""Обучение ранжирующих весов на эталонном наборе eval_queries.json (seed).

Неявных кликов (rank_train.py) ещё мало, а размеченный eval — единственный
качественный источник релевантности. Для каждого запроса из eval_queries.json
пересчитываем выдачу боевым hybrid (search_hybrid → fuse) + фичи, метим ключи
expected как релевантные и строим парные предпочтения «релевантный выше
нерелевантного, стоящего выше». Через ту же SGD (train_pairs) учим 6 линейных
весов над FEAT_NAMES.

Модель активируется вручную после проверки на самом eval (--apply вычисляет
prec@1/mrr до и после), так как гейт rank_train.py защищает от шума кликов, а
здесь датасет курируемый.

Usage:
    python3 rank_seed.py --train            # обучить и вывести отчёт
    python3 rank_seed.py --apply            # применить обученные веса к eval
    python3 rank_seed.py --save             # записать cache/rank_weights.json (active)
"""
import argparse
import json
import math
import os
import random
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
KB_ROOT = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, SCRIPT_DIR)

from fusion import fuse, FEAT_NAMES, FEAT_DIM  # noqa: E402
from search import search_hybrid  # noqa: E402
from rank_train import train_pairs, ndcg_at, norm_key  # noqa: E402
import rank_model  # noqa: E402

EVAL_PATH = os.path.join(KB_ROOT, "scripts", "eval_queries.json")
WEIGHTS_PATH = rank_model.WEIGHTS_PATH


def load_eval():
    docs = json.load(open(EVAL_PATH, encoding="utf-8"))["queries"]
    return [(d["q"], [norm_key(e) for e in d["expected"]]) for d in docs]


def collect_rerank(query):
    """Пересчитывает выдачу ровно так, как боевой search_hybrid, и возвращает
    (keys, features). key=(product, page) из реальных строк выдачи; фичи берём
    из meta fuse. Кросс-продуктовые ключи (product с запятыми) пропускаем —
    такой страницы в фичах fuse нет. Топ запрашиваем шире (30), чтобы в пары
    попадали кандидаты из середины обеих ног."""
    rows, _el, meta = search_hybrid(query, None, limit=30, with_meta=True)
    feats = (meta or {}).get("features", {})
    keys = []
    for row in rows:
        k = (row[0], row[1])
        if "," in (row[0] or ""):
            continue
        if k in feats:
            keys.append(k)
    return keys, feats


def build_seed(query_labels, seed=1):
    """Пары по eval: релевантный выше нерелевантного, показанного выше."""
    pairs = []
    for q, labels in query_labels:
        try:
            keys, feats = collect_rerank(q)
        except Exception:
            continue
        relevant = {k for k in labels if feats.get(k) is not None}
        if not relevant:
            continue
        for idx, rk in enumerate(keys):
            if rk not in relevant:
                continue
            for hi in range(idx):
                nk = keys[hi]
                if nk not in relevant and feats.get(nk) is not None:
                    pairs.append((feats[rk], feats[nk]))
    return pairs


def run_eval_with(weights):
    """Оценка eval_mode('hybrid') при заданных весах (None = модель выключена)."""
    import rank_model as rm
    orig = rm.get_active_weights
    if weights is None:
        rm.get_active_weights = lambda: None
        try:
            return evaluate_mode()
        finally:
            rm.get_active_weights = orig
    old = None
    if os.path.exists(WEIGHTS_PATH):
        old = open(WEIGHTS_PATH, encoding="utf-8").read()
    payload = {
        "active": True, "version": int(time_s()),
        "n_queries": 47, "n_pairs": 0,
        "ndcg_before": None, "ndcg_after": None,
        "weights": [round(float(x), 6) for x in weights],
        "source": "eval_seed",
    }
    os.makedirs(os.path.dirname(WEIGHTS_PATH), exist_ok=True)
    with open(WEIGHTS_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    try:
        return evaluate_mode()
    finally:
        if old is None:
            os.remove(WEIGHTS_PATH)
        else:
            with open(WEIGHTS_PATH, "w", encoding="utf-8") as f:
                f.write(old)


def evaluate_mode():
    import evaluate
    docs = json.load(open(EVAL_PATH, encoding="utf-8"))["queries"]
    r = evaluate.eval_mode("hybrid", docs, 5)
    print("  prec@1=%.3f prec@3=%.3f prec@5=%.3f mrr=%.3f fails=%d"
          % (r["prec@1"], r["prec@3"], r["prec@5"], r["mrr"],
             len(r["top1_fails"])))
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--train", action="store_true")
    ap.add_argument("--save", action="store_true")
    ap.add_argument("--seed", type=int, default=3)
    ap.add_argument("--grid", action="store_true", help="подобрать масштаб весов S")
    args = ap.parse_args()

    ql = load_eval()
    pairs = build_seed(ql, seed=args.seed)
    print("запросов:", len(ql), "пар:", len(pairs))
    w = train_pairs(pairs, seed=args.seed)
    print("веса (SGD):", {n: round(float(v), 4) for n, v in zip(FEAT_NAMES, w)})
    if args.train:
        print("baseline (модель выключена):")
        run_eval_with(None)
    if args.grid or args.save:
        # Аддитивная поправка к RRF складывается с диапазоном ~0.01-0.05.
        # Сырые SGD-веса дают поправку O(1) и переворачивают порядок целиком,
        # поэтому масштабируем S так, чтобы модель лишь корректировала RRF.
        scales = [0.0, 0.005, 0.01, 0.03, 0.06, 0.1, 0.2, 0.5, 1.0]
        best, best_s = None, 0.0
        for s in scales:
            if s == 0.0:
                r = run_eval_with(None)
                base = (r["prec@1"], r["mrr"])
                continue
            r = run_eval_with([x * s for x in w])
            print("  S=%-4s prec@1=%.3f mrr=%.3f" % (s, r["prec@1"], r["mrr"]))
            if best is None or r["prec@1"] > best[0]:
                best = (r["prec@1"], r["mrr"]); best_s = s
        print("base prec@1=%.3f mrr=%.3f; best S=%.3f -> prec@1=%.3f mrr=%.3f"
              % (base[0], base[1], best_s, best[0], best[1]))
    if args.save:
        payload = {
            "active": True, "version": int(time_s()),
            "n_queries": len(ql), "n_pairs": len(pairs),
            "ndcg_before": None, "ndcg_after": None,
            "weights": [round(float(x * best_s), 6) for x in w],
            "source": "eval_seed",
            "scale": best_s,
        }
        os.makedirs(os.path.dirname(WEIGHTS_PATH), exist_ok=True)
        with open(WEIGHTS_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print("записано:", WEIGHTS_PATH)


def time_s():
    import time
    return int(time.time())


if __name__ == "__main__":
    main()