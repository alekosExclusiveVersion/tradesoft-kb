#!/usr/bin/env python3
"""Логирование обращений к eval_server + аналитика корректности выдачи.

Хранит в SQLite (cache/kb_access.db) три категории данных:
  - access_log  — каждый API-запрос (пат, query, IP, сессия, статус, latency);
  - sessions    — cookie-сессии + дневные (ip,ua,date) агрегаты;
  - search_events — по каждому поисковому запросу: выдача (топ-N ключи),
                    диагностика ног (FTS/vector, время, уверенность);
  - events      — поведенческие действия пользователя (click / open / track).

Запись — асинхронная через очередь и отдельный поток-writer, чтобы не
блокировать ответы поиска. Чтение (аналитика /api/access) — синхронное,
с защитой loopback/внутренний-IP/токен на стороне eval_server.

Приватность: храним только IP + User-Agent + cookie-session-id + ключи
страниц (product__page). Тела запросов/сниппеты НЕ сохраняются.
"""
import json
import os
import queue
import sqlite3
import threading
import time
import uuid

try:
    from config import (
        ACCESS_DB_PATH,
        ACCESS_RETENTION_DAYS,
        ACCESS_SESSION_DAYS,
        access_is_enabled,
    )
except Exception:  # fallback, чтобы модуль можно было тестировать отдельно
    ACCESS_DB_PATH = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "cache", "kb_access.db")
    ACCESS_RETENTION_DAYS = 90
    ACCESS_SESSION_DAYS = 30

    def access_is_enabled():
        return bool(ACCESS_DB_PATH) and ACCESS_DB_PATH.lower() not in ("none", "off")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS access_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    ip TEXT,
    session_id TEXT,
    user_agent TEXT,
    path TEXT,
    query_raw TEXT,
    status INTEGER,
    latency_ms REAL,
    n_results INTEGER
);
CREATE INDEX IF NOT EXISTS idx_access_ts ON access_log(ts);
CREATE INDEX IF NOT EXISTS idx_access_sid ON access_log(session_id);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT NOT NULL,
    ip TEXT,
    user_agent TEXT,
    day TEXT,
    first_ts TEXT,
    last_ts TEXT,
    requests INTEGER DEFAULT 1,
    PRIMARY KEY (session_id, day)
);

CREATE TABLE IF NOT EXISTS search_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    session_id TEXT,
    ip TEXT,
    q_raw TEXT,
    q_canonical TEXT,
    corrected_type TEXT,
    product_detected TEXT,
    product_filter TEXT,
    n_results INTEGER,
    top10_pp TEXT,
    n_fts INTEGER,
    n_vec INTEGER,
    fts_ms REAL,
    vec_ms REAL,
    embed_ms REAL,
    total_ms REAL,
    vec_source TEXT,
    boost INTEGER,
    rare TEXT,
    api_intent INTEGER
);
CREATE INDEX IF NOT EXISTS idx_search_ts ON search_events(ts);
CREATE INDEX IF NOT EXISTS idx_search_q ON search_events(q_raw);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    session_id TEXT,
    ip TEXT,
    type TEXT,
    search_event_id INTEGER,
    rank INTEGER,
    pp TEXT,
    product TEXT,
    page TEXT,
    q TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);
"""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()) + \
        f".{int(time.time() * 1000) % 1000:03d}"


def _day(ts: str | None = None) -> str:
    return (ts or _now())[:10]


def gen_session_id() -> str:
    return uuid.uuid4().hex


class AccessLogger:
    """Потокобезопасный асинхронный writer в SQLite."""

    def __init__(self, db_path: str | None = None):
        self.enabled = access_is_enabled()
        self.db_path = db_path or (ACCESS_DB_PATH if self.enabled else None)
        self._q: "queue.Queue[tuple]" = queue.Queue()
        self._lock = threading.Lock()
        self._conn = None
        self._thread = None
        self._stopping = False
        # Кэш id последнего search_event по сессии — чтобы события кликов/открытий
        # получить привязку к выдаче (самообучающееся ранжирование).
        self._seid: dict[str, int] = {}
        if self.enabled and self.db_path:
            os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
            self._conn = sqlite3.connect(
                self.db_path, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
            self._thread = threading.Thread(
                target=self._writer_loop, daemon=True, name="access-log-writer")
            self._thread.start()
            self._prune_locked()

    # ------------------------------------------------------------------ API
    def log_access(self, *, ip, session_id, user_agent, path, query_raw,
                   status, latency_ms, n_results):
        if not self.enabled:
            return
        self._q.put(("access", (ip, session_id, user_agent, path, query_raw,
                                status, latency_ms, n_results)))

    def log_search(self, *, session_id, ip, q_raw, q_canonical, corrected_type,
                   product_detected, product_filter, n_results, top10_pp,
                   n_fts, n_vec, fts_ms, vec_ms, embed_ms, total_ms,
                   vec_source, boost, rare, api_intent):
        if not self.enabled:
            return
        self._q.put(("search", (session_id, ip, q_raw, q_canonical,
                                corrected_type, product_detected,
                                product_filter, n_results,
                                json.dumps(top10_pp or [], ensure_ascii=False),
                                n_fts, n_vec, fts_ms, vec_ms, embed_ms,
                                total_ms, vec_source, boost,
                                json.dumps(rare or [], ensure_ascii=False),
                                1 if api_intent else 0)))

    def log_event(self, *, session_id, ip, type_, search_event_id=None,
                  rank=None, pp=None, product=None, page=None, q=None):
        if not self.enabled:
            return
        self._q.put(("event", (session_id, ip, type_, search_event_id,
                               rank, pp, product, page, q)))

    def last_search_event_id(self, session_id):
        """id последнего search_event сессии (для привязки кликов/открытий)."""
        return self._seid.get(session_id)

    def touch_session(self, *, session_id, ip, user_agent):
        """Обновляет/создаёт cookie-сессию (intent-кэш на сутки)."""
        if not self.enabled:
            return
        self._q.put(("touch", (session_id, ip, user_agent)))

    # ------------------------------------------------------------ writer
    def _writer_loop(self):
        if not self._conn:
            return
        while True:
            try:
                item = self._q.get(timeout=1.0)
            except queue.Empty:
                if self._stopping and self._q.empty():
                    break
                continue
            try:
                self._write(item)
            except Exception:
                pass
            finally:
                self._q.task_done()

    def _write(self, item):
        kind, data = item
        c = self._conn
        try:
            with self._lock:
                if kind == "access":
                    ip, sid, ua, path, qr, st, lat, nr = data
                    c.execute(
                        "INSERT INTO access_log(ts,ip,session_id,user_agent,path,"
                        "query_raw,status,latency_ms,n_results) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (_now(), ip, sid, ua, path, qr, st, lat, nr))
                elif kind == "search":
                    (sid, ip, q_raw, q_canon, ctype, pdet, pfil, nres,
                     top10, nfts, nvec, ftsms, vecms, embms, totms,
                     vsrc, boost, rare, api) = data
                    c.execute(
                        "INSERT INTO search_events(ts,session_id,ip,q_raw,"
                        "q_canonical,corrected_type,product_detected,"
                        "product_filter,n_results,top10_pp,n_fts,n_vec,fts_ms,"
                        "vec_ms,embed_ms,total_ms,vec_source,boost,rare,"
                        "api_intent) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (_now(), sid, ip, q_raw, q_canon, ctype, pdet, pfil,
                         nres, top10, nfts, nvec, ftsms, vecms, embms, totms,
                         vsrc, boost, rare, api))
                    self._seid[sid] = c.execute("SELECT last_insert_rowid()").fetchone()[0]
                elif kind == "event":
                    sid, ip, typ, seid, rank, pp, prod, page, q = data
                    c.execute(
                        "INSERT INTO events(ts,session_id,ip,type,search_event_id,"
                        "rank,pp,product,page,q) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (_now(), sid, ip, typ, seid, rank, pp, prod, page, q))
                elif kind == "touch":
                    sid, ip, ua = data
                    self._upsert_session(c, sid, ip, ua)
                c.commit()
        except Exception:
            try:
                c.rollback()
            except Exception:
                pass
            raise

    def _upsert_session(self, c, sid, ip, ua):
        d = _day()
        # Дневная «активность-сессия» (ip,ua,date) — на тот случай, если
        # пользователь очистил cookie: реальная личность не задвоится.
        self._bump(c, sid, ip, ua, d)
        daykey = f"{ip}|{ua}|{d}"
        self._bump(c, daykey, ip, ua, d)

    def _bump(self, c, sid, ip, ua, d):
        row = c.execute(
            "SELECT first_ts,last_ts,requests FROM sessions "
            "WHERE session_id=? AND day=?",
            (sid, d)).fetchone()
        if row:
            c.execute(
                "UPDATE sessions SET last_ts=?, requests=? WHERE session_id=? AND day=?",
                (_now(), int(row[2]) + 1, sid, d))
        else:
            c.execute(
                "INSERT INTO sessions(session_id,ip,user_agent,day,first_ts,"
                "last_ts,requests) VALUES(?,?,?,?,?,?,1)",
                (sid, ip, ua, d, _now(), _now()))

    def _prune_locked(self):
        if not self.enabled or not ACCESS_RETENTION_DAYS:
            return
        try:
            cutoff = time.strftime(
                "%Y-%m-%dT%H:%M:%S",
                time.localtime(time.time() - ACCESS_RETENTION_DAYS * 86400))
            with self._lock:
                for table in ("access_log", "search_events", "events"):
                    try:
                        self._conn.execute(f"DELETE FROM {table} WHERE ts < ?",
                                           (cutoff,))
                    except Exception:
                        pass
                try:
                    self._conn.execute(
                        "DELETE FROM sessions WHERE last_ts < ?", (cutoff,))
                except Exception:
                    pass
                self._conn.commit()
        except Exception:
            pass

    def close(self):
        self._stopping = True
        if self._thread:
            self._thread.join(timeout=5)
        if self._conn:
            try:
                self._conn.commit()
                self._conn.close()
            except Exception:
                pass
            self._conn = None


# Единственный глобальный логгер, переиспользуемый eval_server'ом.
_logger: AccessLogger | None = None


def get_logger() -> AccessLogger:
    global _logger
    if _logger is None:
        _logger = AccessLogger()
    return _logger


# ---------------------------------------------------------------------------
# Аналитика (чтение) — вызывается из /api/access.
# ---------------------------------------------------------------------------
def _ro_conn():
    return sqlite3.connect(f"file:{ACCESS_DB_PATH}?mode=ro", uri=True)


def summary(hours: int = 24) -> dict:
    since = time.strftime("%Y-%m-%dT%H:%M:%S",
                          time.localtime(time.time() - hours * 3600))
    c = _ro_conn()
    try:
        req = c.execute(
            "SELECT COUNT(*) FROM access_log WHERE ts>=?", (since,)).fetchone()[0]
        searches = c.execute(
            "SELECT COUNT(*) FROM search_events WHERE ts>=?", (since,)).fetchone()[0]
        sessions = c.execute(
            "SELECT COUNT(DISTINCT session_id) FROM sessions WHERE last_ts>=?",
            (since,)).fetchone()[0]
        ips = c.execute(
            "SELECT COUNT(DISTINCT ip) FROM access_log WHERE ts>=?",
            (since,)).fetchone()[0]
        clicks = c.execute(
            "SELECT COUNT(*) FROM events WHERE type IN ('click','open') AND ts>=?",
            (since,)).fetchone()[0]
        opens = c.execute(
            "SELECT COUNT(*) FROM events WHERE type='open' AND ts>=?",
            (since,)).fetchone()[0]
        return {"since_hours": hours, "requests": req, "searches": searches,
                "sessions": sessions, "unique_ips": ips, "events": clicks,
                "document_opens": opens}
    finally:
        c.close()


def top_pages(hours: int = 24, limit: int = 20) -> list:
    since = time.strftime("%Y-%m-%dT%H:%M:%S",
                          time.localtime(time.time() - hours * 3600))
    c = _ro_conn()
    try:
        rows = c.execute(
            "SELECT COALESCE(pp,'(все)') k, COUNT(*) n FROM events "
            "WHERE type IN ('click','open') AND ts>=? AND pp IS NOT NULL "
            "GROUP BY pp ORDER BY n DESC LIMIT ?", (since, limit)).fetchall()
        return [{"page": k, "count": n} for k, n in rows]
    finally:
        c.close()


def top_queries(hours: int = 24, limit: int = 20) -> list:
    since = time.strftime("%Y-%m-%dT%H:%M:%S",
                          time.localtime(time.time() - hours * 3600))
    c = _ro_conn()
    try:
        rows = c.execute(
            "SELECT q_canonical q, COUNT(*) n, "
            "SUM(CASE WHEN n_results=0 THEN 1 ELSE 0 END) empty "
            "FROM search_events WHERE ts>=? AND q_canonical IS NOT NULL "
            "GROUP BY q_canonical ORDER BY n DESC LIMIT ?",
            (since, limit)).fetchall()
        return [{"query": q, "count": n, "empty_results": bool(e)} for q, n, e in rows]
    finally:
        c.close()


def top_products(hours: int = 24, limit: int = 20) -> list:
    since = time.strftime("%Y-%m-%dT%H:%M:%S",
                          time.localtime(time.time() - hours * 3600))
    c = _ro_conn()
    try:
        rows = c.execute(
            "SELECT product, COUNT(*) n FROM events "
            "WHERE type IN ('click','open') AND ts>=? AND product IS NOT NULL "
            "GROUP BY product ORDER BY n DESC LIMIT ?", (since, limit)).fetchall()
        return [{"product": p, "count": n} for p, n in rows]
    finally:
        c.close()


def sessions(hours: int = 24, limit: int = 30) -> list:
    since = time.strftime("%Y-%m-%dT%H:%M:%S",
                          time.localtime(time.time() - hours * 3600))
    c = _ro_conn()
    try:
        rows = c.execute(
            "SELECT ip, user_agent, day, COUNT(*) requests, "
            "MIN(first_ts) first, MAX(last_ts) last FROM sessions "
            "WHERE last_ts>=? GROUP BY ip, user_agent, day "
            "ORDER BY last DESC LIMIT ?", (since, limit)).fetchall()
        return [{"ip": ip, "user_agent": ua, "day": d, "requests": n,
                 "first": f, "last": l} for ip, ua, d, n, f, l in rows]
    finally:
        c.close()


def success_metrics(hours: int = 24) -> dict:
    """Оценка корректности выдачи по поведению пользователей.

    «Успех» сессии = есть хотя бы одно открытие документа. Глубина до первого
    клика — через сколько рангов пользователю понадобилось искать.
    """
    since = time.strftime("%Y-%m-%dT%H:%M:%S",
                          time.localtime(time.time() - hours * 3600))
    c = _ro_conn()
    try:
        opens = c.execute(
            "SELECT COUNT(DISTINCT session_id) FROM events "
            "WHERE type='open' AND ts>=?", (since,)).fetchone()[0]
        total = c.execute(
            "SELECT COUNT(DISTINCT session_id) FROM events WHERE ts>=?",
            (since,)).fetchone()[0]
        depth_rows = c.execute(
            "SELECT rank FROM events WHERE type IN ('click','open') "
            "AND ts>=? AND rank IS NOT NULL", (since,)).fetchall()
        depth = [r[0] for r in depth_rows]

        ctr = {}
        if depth:
            ctr["avg_depth_to_click"] = round(sum(depth) / len(depth), 2)
            ctr["share_rank1"] = round(
                sum(1 for d in depth if d == 1) / len(depth), 3)
        no_click = c.execute(
            "SELECT COUNT(*) FROM search_events WHERE ts>=? AND n_results>0",
            (since,)).fetchone()[0]

        return {"sessions_with_events": total,
                "sessions_with_open": opens,
                # сессии с открытием документа = «успех» (приблизительно)
                "open_rate": round(opens / total, 3) if total else None,
                "click_depth": ctr,
                # запросов с результатами, где НЕ было ни одного клика/открытия
                "queries_with_results_no_click": no_click,
                }
    finally:
        c.close()


def unsatisfied(hours: int = 24, limit: int = 50) -> list:
    """«Неудовлетворённый спрос»: запросы, где не было результата либо не было
    взаимодействия (клик/открытие документа).

    Каждая строка — канонический запрос. Считаются:
      - total        — сколько раз искали;
      - zero_results — сколько раз вернулось 0 результатов;
      - no_interact  — сколько раз результат был, но не последовало ни клика,
                       ни открытия документа (возможная неудовлетворённость);
      - unsatisfied  — сумма первых двух;
      - product      — последний детектированный продукт (может быть None);
      - last_ts      — время последнего запроса.
    """
    since = time.strftime("%Y-%m-%dT%H:%M:%S",
                          time.localtime(time.time() - hours * 3600))
    c = _ro_conn()
    try:
        rows = c.execute(
            "SELECT se.q_canonical q, COUNT(*) total, "
            "SUM(CASE WHEN se.n_results=0 THEN 1 ELSE 0 END) zero_results, "
            "SUM(CASE WHEN f.clicked IS NULL THEN 1 ELSE 0 END) no_interact, "
            "MAX(se.product_detected) product, MAX(se.ts) last_ts "
            "FROM search_events se "
            "LEFT JOIN ("
            "  SELECT DISTINCT search_event_id, 1 clicked "
            "  FROM events WHERE type IN ('open','click') AND ts>=? ) f "
            "  ON f.search_event_id = se.id "
            "WHERE se.ts>=? AND se.q_canonical IS NOT NULL "
            "GROUP BY se.q_canonical "
            "ORDER BY (SUM(CASE WHEN se.n_results=0 "
            "           OR f.clicked IS NULL THEN 1 ELSE 0 END)) DESC, "
            "         COUNT(*) DESC "
            "LIMIT ?", (since, since, limit)).fetchall()
        out = []
        for q, total, zero, no_int, prod, last_ts in rows:
            unsatisfied = (zero or 0) + (no_int or 0)
            out.append({
                "query": q, "total": total,
                "zero_results": zero or 0, "no_interact": no_int or 0,
                "unsatisfied": unsatisfied,
                "product": prod, "last_ts": last_ts,
            })
        return out
    finally:
        c.close()


def raw(hours: int = 24, limit: int = 50) -> list:
    since = time.strftime("%Y-%m-%dT%H:%M:%S",
                          time.localtime(time.time() - hours * 3600))
    c = _ro_conn()
    try:
        rows = c.execute(
            "SELECT ts,ip,session_id,path,query_raw,n_results,status "
            "FROM access_log WHERE ts>=? ORDER BY rowid DESC LIMIT ?",
            (since, limit)).fetchall()
        return [{"ts": t, "ip": i, "session": s, "path": p, "q": q,
                 "n": n, "status": st} for t, i, s, p, q, n, st in rows]
    finally:
        c.close()
