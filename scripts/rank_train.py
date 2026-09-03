#!/usr/bin/env python3
"""Самообучающееся ранжирование: обучение весов по неявным сигналам (клики).

Читает search_events + события click/open из логов (kb_access.db), для каждого
запроса с кликом пересчитывает выдачу через те же примитивы, что и боевой поиск
(FTS + vector + fuse), строит парные предпочтения («кликнутого результата выше
некликнутого, который показан выше»), обучает линейные веса над признаками
(FEAT_NAMES fusion.py) методом SGD на логистической парной потере и, если
выполнены пороги гейта и качество на holdout не упало, записывает
cache/rank_weights.json с active=true.

Когда данных мало (см. MIN_QUERIES/MIN_PAIRS) — модель остаётся выключенной
(active=false либо файл не пишется), поведение поиска не меняется.

Usage:
    python3 rank_train.py                    # с порогами по умолчанию
    python3 rank_train.py --force            # записать веса даже без holdout-улучшения
    python3 rank_train.py --dry-run          # только отчёт, ничего не пишет
"""
import argparse
import json
import math
import os
import random
import sqlite3
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
KB_ROOT = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, SCRIPT_DIR)

from access_log import ACCESS_DB_PATH  # noqa: E402
from fusion import FEAT_NAMES, FEAT_DIM, fuse, API_PRODUCTS, CHANGELOG_PRODUCTS  # noqa: E402
from search import (  # noqa: E402
    RRF_TOP, terms, normalize_terms, search,
    detect_products, detect_product_name,
)
import rank_model  # noqa: E402

WEIGHTS_PATH = rank_model.WEIGHTS_PATH


def _log(*a):
    print(f"[{time.strftime('%H:%M:%S')}]", *a, flush=True)


def norm_key(pp: str):
    """pp из top10/событий → (product, page)."""
    product, _, page = (pp or "").rpartition("__")
    return (product, page)


def collect_rerank(query: str):
    """Пересчитывает выдачу как боевой hybrid и возвращает (keys, features).

    features: dict[(product,page) -> list[float]] из meta.
    """
    fts_rows, _ = search(terms(query), None, limit=RRF_TOP, snippets=False)
    vec_rows = []
    try:
        import embed
        from typesense_client import vector_search
        if embed.ollama_is_available(host=None):
            canonical = query.strip().lower()
            vec = embed.embed_text(canonical)
            for h in vector_search(vec, k=RRF_TOP):
                vec_rows.append((
                    h.get("product"), h.get("page"), h.get("title"),
                    h.get("path"), "", h.get("_score", 0),
                ))
    except Exception:
        vec_rows = []
    merge_keys, meta = fuse(query, fts_rows, vec_rows, None)
    return merge_keys, meta.get("features", {})


def ndcg_at(ranking, labels, k=5):
    """NDCG@k по дискретным релевантностям (0/1)."""
    dcg = 0.0
    for rank, key in enumerate(ranking[:k]):
        rel = float(labels.get(key, 0))
        if rel:
            dcg += (2 ** rel - 1) / math.log2(rank + 2)
    idcg = 0.0
    nrel = min(sum(1 for v in labels.values() if v), k)
    for rank in range(nrel):
        idcg += 1.0 / math.log2(rank + 2)
    return dcg / idcg if idcg > 0 else 0.0


def build_dataset(search_events, use_typo_fallback=True):
    """Строит парный датасет.

    Для каждого search_events (id, q, top10_pp) с событием click/open:
      - пересчитываем выдачу и признаки;
      - метим кликнутые/открытые результаты = 1 (по pp из событий);
      - пары: (кликнутый, некликнутый выше него).
    Возвращает (queries, pairs, query_labels, query_rankings, features_all).
    """
    # группируем события по search_event_id
    by_seid: dict[int, list] = {}
    for ev in search_events.get("events", []):
        by_seid.setdefault(ev["search_event_id"], []).append(ev)

    queries = []          # [(seid, query)]
    pairs = []            # (feature_pos, feature_neg)
    query_labels = {}     # seid -> {key: rel}
    query_rankings = {}   # seid -> [keys] (эталонный порядок по current)
    features_all = {}     # seid -> {key: features}

    for se in search_events["searches"]:
        seid = se["id"]
        evs = by_seid.get(seid)
        if not evs:
            continue
        q = se.get("q_canonical") or se.get("q_raw")
        if not q:
            continue
        try:
            keys, feats = collect_rerank(q)
        except Exception:
            continue
        # релевантность из событий
        labels = {}
        top10 = [norm_key(p) for p in se.get("top10_pp", [])]
        for ev in evs:
            if ev["type"] not in ("click", "open"):
                continue
            k = norm_key(ev["pp"])
            labels[k] = labels.get(k, 0) + 1
        relevant = {k for k, v in labels.items() if v > 0}
        if not relevant:
            continue
        # пара: кликнутый vs некликнутый, показанный выше
        for rk_idx, rk in enumerate(keys):
            if rk not in relevant:
                continue
            for hi in range(rk_idx):
                nk = keys[hi]
                if nk not in relevant and feats.get(nk) is not None:
                    pairs.append((feats[rk], feats[nk]))
        queries.append((seid, q))
        query_labels[seid] = labels
        query_rankings[seid] = keys
        features_all[seid] = feats

    return {
        "queries": queries,
        "pairs": pairs,
        "query_labels": query_labels,
        "query_rankings": query_rankings,
        "features": features_all,
    }


def train_pairs(pairs, epochs=12, lr=0.05, seed=1):
    """SGD на логистической парной потере: w·f_pos - w·f_neg -> +inf."""
    rng = random.Random(seed)
    w = [0.0] * FEAT_DIM
    data = [(fp, fn) for fp, fn in pairs if fp is not None and fn is not None]
    for _ in range(epochs):
        rng.shuffle(data)
        for fp, fn in data:
            diff = sum((a - b) * wi for (a, b), wi in zip(zip(fp, fn), w))
            sig = 1.0 / (1.0 + math.exp(-diff))
            grad = [(a - b) * sig for (a, b) in zip(fp, fn)]
            for i in range(FEAT_DIM):
                w[i] += lr * grad[i]
    return w


def main():
    ap = argparse.ArgumentParser(description="Обучение весов ранжирования по кликам")
    ap.add_argument("--force", action="store_true", help="записать веса даже без улучшения NDCG")
    ap.add_argument("--dry-run", action="store_true", help="только отчёт, ничего не писать")
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()

    if not os.path.exists(ACCESS_DB_PATH):
        _log("БД логов не найдена:", ACCESS_DB_PATH)
        sys.exit(1)
    con = sqlite3.connect(f"file:{ACCESS_DB_PATH}?mode=ro", uri=True)

    # search_events только с топом выдачи
    searches = []
    for r in con.execute(
            "SELECT id, q_canonical, q_raw, top10_pp FROM search_events "
            "WHERE top10_pp IS NOT NULL"):
        sid, qc, qr, top = r
        try:
            top10 = json.loads(top) if top else []
        except json.JSONDecodeError:
            top10 = []
        searches.append({"id": sid, "q_canonical": qc, "q_raw": qr, "top10_pp": top10})
    # события
    events = []
    for r in con.execute(
            "SELECT search_event_id, type, pp FROM events "
            "WHERE type IN ('click','open') AND pp IS NOT NULL"):
        events.append({"search_event_id": r[0], "type": r[1], "pp": r[2]})
    con.close()

    ds = build_dataset({"searches": searches, "events": events})
    n_queries = len(ds["queries"])
    n_pairs = len(ds["pairs"])
    _log(f"search_events всего: {len(searches)}, с событиями: {n_queries}, пар: {n_pairs}")

    if n_queries == 0 or n_pairs == 0:
        _log("Недостаточно данных — модель остаётся выключенной.")
        return

    # train/holdout split (по запросам)
    keys_queries = list(ds["query_rankings"].keys())
    rng = random.Random(args.seed)
    rng.shuffle(keys_queries)
    n_hold = max(1, n_queries // 5)
    hold = set(keys_queries[:n_hold])
    train_ids = [k for k in keys_queries if k not in hold]

    def eval_on(ids):
        ndcg_sum = 0.0
        for qid in ids:
            ranking = ds["query_rankings"][qid]
            labels = ds["query_labels"][qid]
            ndcg_sum += ndcg_at(ranking, labels)
        return ndcg_sum / len(ids) if ids else 0.0

    ndcg_before = eval_on(hold)

    # тренируем только на train-запросах
    sel_ids = set(train_ids)
    pairs_train = []
    for i, (qid, _q) in enumerate(ds["queries"]):
        if qid in sel_ids:
            keys = ds["query_rankings"][qid]
            labels = ds["query_labels"][qid]
            feats = ds["features"][qid]
            relevant = {k for k, v in labels.items() if v > 0}
            for rk_idx, rk in enumerate(keys):
                if rk not in relevant:
                    continue
                for hi in range(rk_idx):
                    nk = keys[hi]
                    if nk not in relevant and feats.get(nk) is not None:
                        pairs_train.append((feats[rk], feats[nk]))

    w = train_pairs(pairs_train, seed=args.seed)
    _log("обученные веса:", {n: round(float(v), 4) for n, v in zip(FEAT_NAMES, w)})

    # NDCG after: временно применяем веса и переставляем
    def score_key(feats, weights):
        return {k: sum(float(wi) * float(fi) for wi, fi in zip(weights, feats[k]))
                for k in feats}
    ndcg_after_sum = 0.0
    for qid in hold:
        keys = ds["query_rankings"][qid]
        labels = ds["query_labels"][qid]
        feats = ds["features"][qid]
        s = score_key(feats, w)
        reranked = sorted(keys, key=lambda k: -s.get(k, 0.0))
        ndcg_after_sum += ndcg_at(reranked, labels)
    ndcg_after = ndcg_after_sum / len(hold) if hold else 0.0

    _log(f"NDCG@5 holdout: до={ndcg_before:.4f}, после={ndcg_after:.4f}")

    active = (n_queries >= rank_model.DEFAULT_MIN_QUERIES
              and n_pairs >= rank_model.DEFAULT_MIN_PAIRS
              and (args.force or ndcg_after >= ndcg_before))
    _log(f"Гейт: n_queries={n_queries} (>= {rank_model.DEFAULT_MIN_QUERIES}), "
         f"n_pairs={n_pairs} (>= {rank_model.DEFAULT_MIN_PAIRS}) → active={active}")

    if args.dry_run:
        _log("dry-run: файл не пишется.")
        return

    if not active:
        # не активируем, но пишем отладочную версию без active
        payload = {
            "active": False, "version": 0,
            "n_queries": n_queries, "n_pairs": n_pairs,
            "ndcg_before": round(ndcg_before, 4),
            "ndcg_after": round(ndcg_after, 4),
            "weights": [round(float(x), 6) for x in w],
        }
    else:
        payload = {
            "active": True, "version": int(time.time()),
            "n_queries": n_queries, "n_pairs": n_pairs,
            "ndcg_before": round(ndcg_before, 4),
            "ndcg_after": round(ndcg_after, 4),
            "weights": [round(float(x), 6) for x in w],
        }
    with open(WEIGHTS_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    _log("записано:", WEIGHTS_PATH)


if __name__ == "__main__":
    main()
