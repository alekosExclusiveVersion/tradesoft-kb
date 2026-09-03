"""Обновляет scripts/eval_queries.json из реальных запросов в cache/kb_access.db.

Зачем: метрики evaluate.py строятся на заранее зафиксированном наборе запросов.
Чтобы не «переобучаться» на этих 29 запросах и видеть, как поиск ведёт себя на
реальных запросах пользователей — берём свежие запросы из лога обращений.

Процедура (интерактивная, по каждому новому запросу):
  1. Показываем канонический запрос, сколько раз встречался, и топ-5 выдачи.
  2. Спрашиваем: добавить в eval? (y/n/skip-all)
     - y — ввести ожидаемые product__page через пробел (Enter = принять топ-5);
     - n — пропустить;
     - q — выйти и сохранить.
  3. Сохраняет обновлённый eval_queries.json.

Флаги:
  --hours N    смотреть запросы за последние N часов (по умолчанию 24*7)
  --min-count N  учитывать только запросы, встретившиеся ≥ N раз (по умолчанию 1)
  --dry-run    не сохранять, только показать
  --limit N    максимум запросов для рассмотрения
"""
import argparse
import json
import os
import sqlite3
import sys
import time

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_EVAL_PATH = os.path.join(_SCRIPT_DIR, "eval_queries.json")
_DB_PATH = os.path.join(_SCRIPT_DIR, "..", "cache", "kb_access.db")


def load_queries():
    with open(_EVAL_PATH, encoding="utf-8") as f:
        data = json.load(f)
    return data, set(item["q"] for item in data["queries"])


def _looks_like_mojibake(q):
    """Отсеивает «битые» запросы (двойное кодирование UTF-8 → Ð°, â€, 'ï' и пр.).

    Типичный мусор: некорректно декодированные байты вроде «Ð°Ð²ÑÑÐ¾Ð¸Ð¼Ð¾Ð¿ÑÑ».
    Пропускаем такие, чтобы не засорять эталонный набор.
    """
    if not q:
        return True
    moji = set("ÐÂÃÕÊÎÔÙÐ¨Ð©Ð°Ð±Ð²Ð³Ð´ÐµÐ¶Ð·Ð¸Ð¹ÐºÐ»Ð¼Ð½Ð¾Ð¿ÑÑÑÑÑÑÑÑ")
    n_moji = sum(1 for ch in q if ch in moji)
    return n_moji >= 2


def live_queries(hours, min_count):
    since = time.strftime("%Y-%m-%dT%H:%M:%S",
                          time.localtime(time.time() - hours * 3600))
    conn = sqlite3.connect(f"file:{_DB_PATH}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT q_canonical, COUNT(*) c, MAX(n_results) max_n "
            "FROM search_events WHERE ts>=? AND q_canonical IS NOT NULL "
            "AND trim(q_canonical)<>'' "
            "GROUP BY q_canonical HAVING c>=? "
            "ORDER BY c DESC, MAX(ts) DESC",
            (since, min_count)).fetchall()
    finally:
        conn.close()
    return [r for r in rows if not _looks_like_mojibake(r[0])]


def top_results(q, n=5):
    from search import search_hybrid
    try:
        rows, _ = search_hybrid(q, None, limit=n)
        return [f"{r[0]}__{r[1]}" for r in rows]
    except Exception as e:
        return [f"<error: {e}>"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24 * 7)
    ap.add_argument("--min-count", type=int, default=1)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    if not os.path.exists(_DB_PATH):
        print(f"Нет базы обращений: {_DB_PATH}")
        print("Сначала нужно, чтобы сервер (eval_server.py) писал search_events.")
        return 1

    data, existing = load_queries()
    cands = live_queries(args.hours, args.min_count)
    if args.limit:
        cands = cands[:args.limit]

    new_items = []
    added = 0
    skipped = 0
    skip_all = False
    total_new = 0

    for q, cnt, max_n in cands:
        if q in existing:
            continue
        total_new += 1
        if skip_all:
            break
        if cnt == 0:
            continue
        print("=" * 70)
        print(f"Запрос: «{q}»  (встречался {cnt} раз, макс. результатов {max_n})")
        tops = top_results(q)
        print("Топ-5 выдачи:")
        for i, t in enumerate(tops, 1):
            mark = "" if i <= max_n and max_n else ""
            print(f"   {i}. {t}{mark}")
        if args.dry_run:
            print("   [dry-run — не добавляем]")
            continue
        ans = input("   добавить в eval? [y/n/q, enter=y] ").strip().lower()
        if ans == "q":
            break
        if ans in ("n", "no"):
            skipped += 1
            continue
        expected = tops
        custom = input("   ожидаемые product__page (Enter = принять топ-5): ").strip()
        if custom:
            expected = [t for t in custom.split() if t]
        if not expected:
            expected = tops
        new_items.append({"q": q, "expected": expected})
        added += 1
        existing.add(q)

    print("=" * 70)
    print(f"Добавлено: {added}, пропущено: {skipped}, новых кандидатов: {total_new}")

    if args.dry_run:
        print("dry-run — файл не изменён.")
        return 0

    if not new_items:
        print("Нет новых запросов для добавления.")
        return 0

    data["queries"].extend(new_items)
    with open(_EVAL_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"Обновлено: {_EVAL_PATH} (теперь {len(data['queries'])} запросов)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
