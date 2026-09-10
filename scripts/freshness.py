#!/usr/bin/env python3
"""Проверка актуальности стемминг-слоя поиска (search.py + stem.py + pymorphy3).

Индекс kb_index.db (FTS5) хранит готовые stems из `stems_fts`. Если меняется
механизм стемминга (stem.py или версия pymorphy3), а индекс остаётся старым —
поиск начинает дрейфовать: стемы в индексе посчитаны старым алгоритмом, а
запросы стемлятся новым. Поэтому после таких изменений нужен `--rebuild`.

Отпечаток (fingerprint) = sha256(содержимое stem.py + версия pymorphy3 + версия
Python). Корректное состояние = fingerprint в cache/build_meta.json совпадает с
текущим и index_mtime равен mtime базы. build_meta.json пишет сам build_index.py
после пересборки; --fix делает всё необходимое здесь.

Примеры:
  python3 freshness.py --check   # 0 = ок, 1 = нужно пересобрать, 2 = ошибка
  python3 freshness.py --fix     # пересоберёт индекс при необходимости
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
KB_ROOT = os.path.dirname(SCRIPT_DIR)
STEM_SRC = os.path.join(SCRIPT_DIR, "stem.py")
DB_PATH = os.path.join(KB_ROOT, "cache", "kb_index.db")
META_PATH = os.path.join(KB_ROOT, "cache", "build_meta.json")


def build_fingerprint():
    h = hashlib.sha256()
    try:
        with open(STEM_SRC, "rb") as f:
            h.update(f.read())
    except OSError:
        h.update(b"<no stem.py>")
    try:
        from importlib.metadata import version
        h.update(b"pymorphy3=" + version("pymorphy3").encode())
    except Exception:
        h.update(b"pymorphy3=none")
    h.update(("python=" + sys.version.split()[0]).encode())
    return h.hexdigest()


def read_meta():
    try:
        with open(META_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_meta(fingerprint, db_mtime):
    data = {
        "fingerprint": fingerprint,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "index_mtime": db_mtime,
    }
    tmp = META_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, META_PATH)
    return data


def index_mtime():
    try:
        return os.path.getmtime(DB_PATH)
    except OSError:
        return None


def status():
    fp = build_fingerprint()
    meta = read_meta()
    mtime = index_mtime()
    reasons = []
    if mtime is None:
        reasons.append("индекс kb_index.db отсутствует")
    elif meta is None:
        reasons.append("build_meta.json отсутствует")
    else:
        if meta.get("fingerprint") != fp:
            reasons.append("изменился механизм стемминга — нужна пересборка индекса")
        if meta.get("index_mtime") != mtime:
            reasons.append("индекс обновлён, но build_meta.json устарел")
    return fp, meta, mtime, reasons


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="только проверить и выйти (0 ок / 1 устарел / 2 ошибка)")
    ap.add_argument("--fix", action="store_true",
                    help="пересобрать индекс при несовпадении fingerprint")
    args = ap.parse_args()

    fp, meta, mtime, reasons = status()
    cur = build_fingerprint()
    print(f"fingerprint  : {cur[:16]}…")
    print(f"индекс mtime : {mtime}")
    print(f"build_meta   : {meta}")

    if args.fix:
        rebuild_needed = (mtime is None or meta is None
                          or meta.get("fingerprint") != fp)
        print()
        if rebuild_needed:
            print("Запуск build_index.py --rebuild …")
            subprocess.run(
                [sys.executable, os.path.join(SCRIPT_DIR, "build_index.py"),
                 "--rebuild"],
                check=True, cwd=SCRIPT_DIR)
            mtime = index_mtime()
            write_meta(fp, mtime)
            print("build_meta.json записан")
        else:
            write_meta(fp, mtime)
            print("Изменений стемминга нет, мета обновлена")

    reasons = status()[3]
    print()
    if reasons:
        for r in reasons:
            print(f"УСТАРЕЛО: {r}")
        return 1
    print("Всё актуально")
    return 0


if __name__ == "__main__":
    sys.exit(main())