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

import asyncio
import logging

from .helpers import DOC_EXTRACTOR_URL

log = logging.getLogger("doc_reader")

SOURCE = "gdrive"

_DOCUMENTS_URL = f"{DOC_EXTRACTOR_URL}/v1/documents"
_FROM_URL_URL = f"{DOC_EXTRACTOR_URL}/v1/documents/from-url"
_SEARCH_URL = f"{DOC_EXTRACTOR_URL}/v1/search"

# Engine statuses that mean "content is available to read/search".
READY_STATES = ("processed", "cached")

# app-secret name declared in app.py — the doc-extractor-service engine has
# required `Authorization: Bearer <token>` on every /v1/documents & /v1/search
# call since 2026-07-19 (server config key api_auth_token_gdrive, independent
# of file-reader's own token). This extension sent NO auth header at all
# until 2026-08-16 — every ingest/read/search/overview/delete call had been a
# hard 401 in prod since that rollout (found via full-repo audit; confirmed
# live: zero source='gdrive' rows ever existed in the engine's documents
# table). Missing/empty means calls will keep failing with a clear 401 error
# instead of silently doing nothing.
DOC_EXTRACTOR_TOKEN_SECRET = "doc_extractor_token"


def imperal_id(ctx) -> str:
    """Canonical user id scoping ALL engine storage. Missing → hard error: we
    must never ingest/read under an unscoped or wrong identity."""
    user = getattr(ctx, "user", None)
    uid = getattr(user, "imperal_id", None) if user else None
    if not uid:
        raise RuntimeError("no user context (imperal_id) — cannot scope file storage")
    return uid


async def _auth_headers(ctx) -> dict:
    """Bearer header for the engine's own auth (SEPARATE from the Drive OAuth
    token passed as the `auth` field in ingest() — that one is handed to the
    engine so IT can fetch the file from Drive; this one authenticates THIS
    EXTENSION to the engine itself). Fails loud and specific rather than
    silently sending no header and getting a bare, unexplained 401."""
    token = await ctx.secrets.get(DOC_EXTRACTOR_TOKEN_SECRET)
    if not token:
        raise RuntimeError(
            "doc-extractor engine token not configured (doc_extractor_token app secret is "
            "missing) — the engine has required auth since 2026-07-19; content calls cannot work "
            "until this secret is set."
        )
    return {"Authorization": f"Bearer {token}"}


async def _send(ctx, method: str, url: str, **kwargs):
    """One retry on transient 5xx / network error — absorbs the platform's
    'first call fails, retry works' infra transients. Real 4xx are returned
    as-is for the caller to interpret (e.g. 404 → self-heal re-ingest).

    IMPORTANT (found 2026-08-16): _auth_headers() used to be fetched ONCE,
    OUTSIDE this retry loop. It calls ctx.secrets.get(), which itself is a
    network round-trip to the platform's own auth-gateway — and that call can
    transiently ReadTimeout just like any other network call (confirmed live:
    "auth-gw unreachable on get(name='doc_extractor_token'): ReadTimeout").
    When it did, the whole engine request died immediately with ZERO retries,
    even though every other transient (5xx, network error on the actual HTTP
    call) already got one. Auth-header fetch now happens INSIDE the loop so a
    one-off auth-gw hiccup gets the same retry-once treatment as everything
    else instead of hard-failing the file."""
    extra_headers = kwargs.pop("headers", None)
    call = getattr(ctx.http, method)
    last: Exception | None = None
    for _ in range(2):
        try:
            headers = await _auth_headers(ctx)
        except Exception as e:  # noqa: BLE001 - auth-gw hiccup → retry once too
            last = e
            continue
        if extra_headers:
            headers = {**headers, **extra_headers}
        try:
            resp = await call(url, headers=headers, **kwargs)
        except Exception as e:  # noqa: BLE001 - network/timeout → retry once
            last = e
            continue
        if resp.status_code >= 500:
            last = RuntimeError(f"engine returned {resp.status_code}")
            continue
        return resp
    raise last if last else RuntimeError("engine request failed")


# Statuses the engine's async drain loop can still move on from — anything
# else (processed | failed | unsupported | cached) is terminal. See
# doc-extractor-service app/schemas.py: "status: pending | processing |
# processed | failed | unsupported | cached".
_IN_PROGRESS_STATES = ("pending", "processing")

# Ingest is now async server-side (2026-08-16: from-url stages the file and
# returns immediately; a separate drain loop does the actual extraction later)
# — polling budget for THIS call to wait for a terminal status before giving
# up and reporting "still pending" back to the caller.
_INGEST_POLL_INTERVAL_S = 2.0
_INGEST_POLL_MAX_S = 90.0


async def ingest(ctx, *, fetch_url: str, auth: str, content_key: str, filename: str) -> dict:
    """Hand the engine a Drive URL (media URL for binaries, export URL for
    Google-native) + a transient bearer token (the caller's own Drive OAuth
    access token) so the ENGINE downloads it itself, over /v1/documents/from-url.

    Why not send the bytes ourselves: ctx.http (imperal_sdk's HTTP client)
    decodes any non-JSON response body via `.text` when the content-type isn't
    JSON — that's LOSSY for arbitrary binary bytes (confirmed: round-tripping a
    PDF/XLSX/image through it corrupts the payload). There is no raw/bytes mode
    on ctx.http, so this extension can never safely hold Drive file bytes
    in-process; the fix is to have the server fetch the URL directly (real
    httpx there, byte-exact). See doc-extractor-service app/schemas.py
    FromUrlRequest / app/documents.py _handle_one_from_url for the server side
    (added 2026-08-16; URL is allowlisted to Google's own API host there).

    IMPORTANT (found 2026-08-16): /v1/documents/from-url is a two-phase async
    endpoint on the server -- it stages the download and returns a `pending`
    row immediately; a SEPARATE drain loop does the real extraction moments
    later. The first response is therefore ALMOST ALWAYS status=pending, with
    no error/error_code yet (they're only set once extraction actually runs).
    Reading that first response as final made index_record() mark the file
    FAILED with "could not index this file (None)" nearly every time, even
    for perfectly fine files -- a pure race, not a real failure. So: if the
    first response isn't already terminal, POLL the document by id via
    overview() until it reaches a terminal status or the poll budget runs out.

    `content_key` is accepted for API-compat with callers but the server keys
    on sha256 of the actual downloaded bytes — dedup still works, just content-
    addressed instead of Drive-revision-addressed (an unchanged file still
    ends up a cache hit once its bytes match what's already stored).
    Returns the DocumentOut dict."""
    resp = await _send(ctx, "post", _FROM_URL_URL, json={
        "source": SOURCE,
        "imperal_id": imperal_id(ctx),
        "url": fetch_url,
        "auth": auth,
        "filename": filename,
    }, timeout=120)
    resp.raise_for_status()
    docs = ((resp.json() or {}).get("data") or {}).get("documents") or []
    if not docs:
        raise RuntimeError("engine returned no document")
    doc = docs[0]
    document_id = doc.get("document_id")
    if doc.get("status") not in _IN_PROGRESS_STATES or not document_id:
        return doc
    waited = 0.0
    while waited < _INGEST_POLL_MAX_S:
        await asyncio.sleep(_INGEST_POLL_INTERVAL_S)
        waited += _INGEST_POLL_INTERVAL_S
        doc = await overview(ctx, document_id)
        if doc.get("status") not in _IN_PROGRESS_STATES:
            return doc
    # Budget exhausted but the engine is still working on it (a big/slow file)
    # — this is NOT a failure, so say so plainly instead of the previous
    # opaque "could not index this file (None)".
    doc["error"] = doc.get("error") or (
        f"still processing after {int(_INGEST_POLL_MAX_S)}s — try again shortly"
    )
    return doc


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
