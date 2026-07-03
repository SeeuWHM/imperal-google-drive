"""Google Drive · doc-extractor engine client — the single CONTENT plane.

Every file (Google-native OR binary) becomes stored text + chunks + embeddings
in the shared engine, partitioned source="gdrive" and scoped to the user's
imperal_id (fail-closed). The EXTENSION owns the lifecycle (connect, quota,
evict); the engine is a dumb, self-healing content cache exposing exactly five
operations: ingest / read / search / overview / delete.

No embedding ever runs on the read path — indexing happens once, in the
background, at ingest. Reads come straight from stored text: no Drive
round-trip, no re-extract, no re-embed.
"""
from __future__ import annotations

import logging

from .helpers import DOC_EXTRACTOR_URL

log = logging.getLogger("doc_reader")

SOURCE = "gdrive"

_DOCUMENTS_URL = f"{DOC_EXTRACTOR_URL}/v1/documents"
_SEARCH_URL = f"{DOC_EXTRACTOR_URL}/v1/search"

# Engine statuses that mean "content is available to read/search".
READY_STATES = ("processed", "cached")


def imperal_id(ctx) -> str:
    """Canonical user id scoping ALL engine storage. Missing → hard error: we
    must never ingest/read under an unscoped or wrong identity."""
    user = getattr(ctx, "user", None)
    uid = getattr(user, "imperal_id", None) if user else None
    if not uid:
        raise RuntimeError("no user context (imperal_id) — cannot scope file storage")
    return uid


async def _send(ctx, method: str, url: str, **kwargs):
    """One retry on transient 5xx / network error — absorbs the platform's
    'first call fails, retry works' infra transients. Real 4xx are returned
    as-is for the caller to interpret (e.g. 404 → self-heal re-ingest)."""
    call = getattr(ctx.http, method)
    last: Exception | None = None
    for _ in range(2):
        try:
            resp = await call(url, **kwargs)
        except Exception as e:  # noqa: BLE001 - network/timeout → retry once
            last = e
            continue
        if resp.status_code >= 500:
            last = RuntimeError(f"engine returned {resp.status_code}")
            continue
        return resp
    raise last if last else RuntimeError("engine request failed")


async def ingest(ctx, *, fetch_url: str, auth: str, content_key: str, filename: str) -> dict:
    """Hand the engine a URL it fetches ITSELF (media URL for binaries, export
    URL for Google-native) + a transient bearer token + a change key. The
    engine downloads, extracts, stores the text and embeds it. Idempotent by
    (source, imperal_id, content_key): an unchanged file is a fast cache hit
    with no re-download/re-embed. Returns the DocumentOut dict."""
    resp = await _send(ctx, "post", _DOCUMENTS_URL, data={
        "source": SOURCE,
        "imperal_id": imperal_id(ctx),
        "url": fetch_url,
        "auth": auth,
        "content_key": content_key,
        "filename": filename,
    }, timeout=120)
    resp.raise_for_status()
    docs = ((resp.json() or {}).get("data") or {}).get("documents") or []
    if not docs:
        raise RuntimeError("engine returned no document")
    return docs[0]


async def read_text(ctx, document_id: int, offset: int = 0, limit: int = 40_000) -> dict:
    """Windowed plain text from the engine's stored blob — no Drive, no
    re-extract. Returns {text, offset, limit, total_chars, truncated}. Raises
    on 404/409 (gone/cold) so the caller can self-heal by re-ingesting."""
    resp = await _send(ctx, "get", f"{_DOCUMENTS_URL}/{document_id}/text", params={
        "source": SOURCE, "imperal_id": imperal_id(ctx), "offset": offset, "limit": limit,
    }, timeout=60)
    resp.raise_for_status()
    return (resp.json() or {}).get("data") or {}


async def search(ctx, query: str, k: int = 6) -> list[dict]:
    """Semantic RAG over THIS user's gdrive chunks only — top-K most relevant
    chunks (not whole files). Returns [{document_id, filename, seq, text, score}]."""
    resp = await _send(ctx, "post", _SEARCH_URL, json={
        "source": SOURCE, "imperal_id": imperal_id(ctx), "query": query, "k": k,
    }, timeout=60)
    resp.raise_for_status()
    return ((resp.json() or {}).get("data") or {}).get("hits") or []


async def overview(ctx, document_id: int) -> dict:
    """Cheap recall — metadata + preview, no full read. Returns DocumentOut."""
    resp = await _send(ctx, "get", f"{_DOCUMENTS_URL}/{document_id}", params={
        "source": SOURCE, "imperal_id": imperal_id(ctx),
    }, timeout=30)
    resp.raise_for_status()
    return (resp.json() or {}).get("data") or {}


async def delete(ctx, document_id: int) -> bool:
    """Evict a document from the engine (PG cascade + NC blob). Used by
    disconnect and by lazy cold-eviction. 404 = already gone → treat as done."""
    resp = await _send(ctx, "delete", f"{_DOCUMENTS_URL}/{document_id}", params={
        "source": SOURCE, "imperal_id": imperal_id(ctx),
    }, timeout=30)
    if resp.status_code == 404:
        return False
    resp.raise_for_status()
    return True
