#!/usr/bin/env python3
"""Typesense HTTP client wrapper.

Provides collection management, document upsert, and vector search.
Uses only stdlib (urllib.request) — no external dependencies.
"""
import json
import time
import urllib.request
import urllib.error

from config import get_config, COLLECTION_NAME, EMBEDDING_DIMS

# Прод-прокси перед Typesense изредка рвёт соединение (ConnectionReset /
# RemoteDisconnected). Повторяем идемпотентные запросы несколько раз.
_MAX_RETRIES = 3
_RETRY_DELAY = 0.8



def _request(method: str, path: str, body=None, params=None) -> dict | list | None:
    cfg = get_config()
    url = cfg["typesense_host"] + path
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url += "?" + qs

    headers = {
        "X-TYPESENSE-API-KEY": cfg["typesense_key"],
        "Content-Type": "application/json",
    }

    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    _conn_err = (ConnectionResetError, ConnectionError, TimeoutError,
                 urllib.error.URLError)
    last_err = None
    for attempt in range(_MAX_RETRIES):
        try:
            with urllib.request.urlopen(req) as r:
                if r.status == 204 or r.length == 0:
                    return None
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            err_body = e.read().decode(errors="replace")
            raise RuntimeError(f"Typesense {method} {path} → {e.code}: {err_body}") from e
        except _conn_err as e:
            last_err = e
            if attempt < _MAX_RETRIES - 1:
                time.sleep(_RETRY_DELAY)
                continue
            raise RuntimeError(f"Typesense {method} {path} соединение: {last_err}") from last_err
    raise RuntimeError(f"Typesense {method} {path} не удалось: {last_err}")


def _raw_request(method: str, path: str, data: str | None = None, params=None) -> str | None:
    """Send pre-encoded body (NDJSON input does NOT go through json.dumps).
    Returns the raw response text — the import API answers with one JSON line
    per document, so callers must parse it line by line."""
    cfg = get_config()
    url = cfg["typesense_host"] + path
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url += "?" + qs

    headers = {
        "X-TYPESENSE-API-KEY": cfg["typesense_key"],
        "Content-Type": "text/plain",
    }

    req = urllib.request.Request(url, data=data.encode() if data is not None else None,
                                 headers=headers, method=method)

    try:
        with urllib.request.urlopen(req) as r:
            if r.status == 204 or r.length == 0:
                return None
            return r.read().decode()
    except urllib.error.HTTPError as e:
        err_body = e.read().decode(errors="replace")
        raise RuntimeError(f"Typesense {method} {path} → {e.code}: {err_body}") from e


def ensure_collection(name: str = COLLECTION_NAME) -> None:
    """Create collection if it doesn't exist."""
    try:
        _request("GET", f"/collections/{name}")
        return
    except RuntimeError:
        pass

    schema = {
        "name": name,
        "fields": [
            {"name": "product", "type": "string", "facet": True},
            {"name": "page", "type": "string"},
            {"name": "title", "type": "string"},
            {"name": "path", "type": "string"},
            {"name": "chunk_idx", "type": "int32"},
            {"name": "content", "type": "string"},
            {
                "name": "embedding",
                "type": "float[]",
                "num_dim": EMBEDDING_DIMS,
                "vec_dist_metric": "cosine",
                "index": True,
            },
        ],
        "default_sorting_field": "chunk_idx",
    }
    _request("POST", "/collections", body=schema)
    print(f"Collection '{name}' created")


def delete_collection(name: str = COLLECTION_NAME) -> None:
    """Delete collection if it exists."""
    try:
        _request("DELETE", f"/collections/{name}")
        print(f"Collection '{name}' deleted")
    except RuntimeError:
        pass


def upsert_document(doc: dict, name: str = COLLECTION_NAME) -> None:
    """Upsert a single document."""
    _request("POST", f"/collections/{name}/documents", body=doc)


def upsert_documents(docs: list[dict], name: str = COLLECTION_NAME) -> list[dict]:
    """Batch upsert documents via the NDJSON import endpoint.

    Typesense's /documents/import expects one JSON document per line
    (NDJSON), not a JSON array. Response is also one JSON line per
    document; parsed into a list of {"success": bool, ...}.
    """
    if not docs:
        return []
    ndjson = "\n".join(json.dumps(d, ensure_ascii=False) for d in docs) + "\n"
    raw = _raw_request("POST", f"/collections/{name}/documents/import",
                       data=ndjson, params={"action": "upsert"})
    if not raw:
        return []
    results = []
    for line in raw.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            results.append(json.loads(line))
        except json.JSONDecodeError:
            results.append({"success": False, "error": f"unparseable line: {line[:80]}"})
    return results


def vector_search(query_vector: list[float], k: int = 20,
                  product: str | None = None,
                  name: str = COLLECTION_NAME) -> list[dict]:
    """Vector (semantic) search.

    Returns list of hits with keys: id, product, page, title, path,
    chunk_idx, content, score (distance).
    """
    vec_str = ",".join(f"{v:.8f}" for v in query_vector)
    vector_query = f"embedding:([{vec_str}], k:{k})"

    body: dict = {
        "searches": [{
            "collection": name,
            "q": "*",
            "vector_query": vector_query,
            "per_page": k,
        }]
    }
    if product:
        body["searches"][0]["filter_by"] = f"product:={product}"

    result = _request("POST", "/multi_search", body=body)
    hits = result.get("results", [{}])[0].get("hits", [])
    out = []
    for h in hits:
        doc = h.get("document", {})
        doc["_score"] = h.get("vector_distance", 0)
        out.append(doc)
    return out


def export_doc_ids(name: str = COLLECTION_NAME) -> set[str]:
    """Return the set of all document IDs currently in the collection (via export)."""
    ids: set[str] = set()
    for _ in range(_MAX_RETRIES):
        try:
            raw = _raw_request("GET", f"/collections/{name}/documents/export")
            for line in (raw or "").strip().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    ids.add(json.loads(line).get("id", ""))
                except json.JSONDecodeError:
                    continue
            return ids
        except RuntimeError:
            time.sleep(_RETRY_DELAY)
            continue
    return ids


def collection_stats(name: str = COLLECTION_NAME) -> dict:
    """Return document count and other stats."""
    try:
        col = _request("GET", f"/collections/{name}")
        return {"num_documents": col.get("num_documents", 0)}
    except RuntimeError:
        return {"num_documents": 0}
