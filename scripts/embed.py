#!/usr/bin/env python3
"""Ollama embedding client.

Generates vectors from text using the configured embedding model.
Supports batch embedding for efficiency.
"""
import json
import urllib.request
import urllib.error

from config import get_config, EMBEDDING_MODEL, EMBEDDING_DIMS

# Max characters passed to the embedding model at once. The embedding model's
# context window is limited (~11000 chars / 4096 tokens); oversized chunks fail
# with HTTP 500. Chunks under this limit are embedded fully (max context kept);
# only oversized ones are truncated to avoid errors.
MAX_EMBED_CHARS = 8000


def embed_text(text: str, model: str = EMBEDDING_MODEL, host: str | None = None) -> list[float]:
    """Generate embedding for a single text.  host override → Ollama по адресу host."""
    if len(text) > MAX_EMBED_CHARS:
        text = text[:MAX_EMBED_CHARS]
    ollama = host or get_config()["ollama_host"]
    url = ollama + "/api/embeddings"
    body = json.dumps({"model": model, "prompt": text}).encode()
    req = urllib.request.Request(url, data=body,
                                headers={"Content-Type": "application/json"})

    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            data = json.loads(r.read())
    except urllib.error.URLError as e:
        raise RuntimeError(f"Ollama embed failed: {e}") from e

    vec = data.get("embedding", [])
    if len(vec) != EMBEDDING_DIMS:
        raise ValueError(f"Expected {EMBEDDING_DIMS}d, got {len(vec)}d")
    return vec


def embed_batch(texts: list[str], model: str = EMBEDDING_MODEL,
                batch_size: int = 8, host: str | None = None) -> list[list[float]]:
    """Generate embeddings for many texts using Ollama's batched /api/embed.

    The batch endpoint is dramatically faster than one /api/embeddings call per
    text (Ollama batches inference server-side). Returns vectors in input order.
    """
    ollama = host or get_config()["ollama_host"]
    url = ollama + "/api/embed"
    all_vectors: list[list[float]] = []

    for i in range(0, len(texts), batch_size):
        batch = [t if len(t) <= MAX_EMBED_CHARS else t[:MAX_EMBED_CHARS]
                 for t in texts[i:i + batch_size]]
        body = json.dumps({"model": model, "input": batch}).encode()
        req = urllib.request.Request(url, data=body,
                                    headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                data = json.loads(r.read())
        except urllib.error.URLError as e:
            raise RuntimeError(f"Ollama embed_batch failed: {e}") from e

        vecs = data.get("embeddings", [])
        for v in vecs:
            if len(v) != EMBEDDING_DIMS:
                raise ValueError(f"Expected {EMBEDDING_DIMS}d, got {len(v)}d")
        all_vectors.extend(vecs)

    return all_vectors


def ollama_is_available(timeout: float = 3.0, host: str | None = None) -> bool:
    """Check if Ollama is reachable."""
    ollama = host or get_config()["ollama_host"]
    try:
        req = urllib.request.Request(ollama + "/api/version")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False
