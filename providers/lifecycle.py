"""Google Drive · file lifecycle — the connector-side brain.

Owns the picked-file store record and its state machine so the engine can stay
a dumb cache. ONE authority (this module) means panel + Postgres + Nextcloud
never drift: the same code that forgets a file also deletes its engine doc.

Record shape (docreader_picked_files):
    file_id, name, mime_type, size_bytes, account_email   # picked metadata
    status         pending | indexing | ready | failed | cold
    document_id    engine doc id once indexed (None otherwise)
    content_key    Drive change key of the indexed version
    last_access_at epoch seconds — drives cold-eviction
    error          last failure reason (when status=failed)

No embedding runs on the read path: index_record() does the heavy ingest (in
the background, at pick time / self-heal), reads just fetch stored text.
"""
from __future__ import annotations

import asyncio
import logging
import time

from . import extractor, file_types
from .google_api import drive_get_metadata
from .helpers import (
    FILES_COLLECTION,
    GOOGLE_FOLDER_MIME,
    _account_email,
    _active_account,
    _all_accounts,
    _all_picked_files,
    _find_picked_file,
)
from .token_refresh import _refresh_token_if_needed

log = logging.getLogger("doc_reader")

# ── States ────────────────────────────────────────────────────────────────────
PENDING, INDEXING, READY, FAILED, COLD = "pending", "indexing", "ready", "failed", "cold"


class NotReadyError(RuntimeError):
    """Raised when a read/search hits a file that isn't indexed yet. Signals the
    handler to kick background indexing and ask the user to retry — reads NEVER
    trigger a heavy synchronous ingest on the request path (that was the timeout)."""

# ── Policy (config-in-code — one line each to tune) ───────────────────────────
TTL_DAYS = 14
COLD_AFTER_S = TTL_DAYS * 86_400
MAX_DOCS = 200
MAX_BYTES = 1024 * 1024 * 1024          # 1 GiB per account
MAX_PER_PICK = 25
WARN_FILE_BYTES = 50 * 1024 * 1024      # 50 MiB — soft warn on a single big file
_INDEX_CONCURRENCY = 4                  # parallel ingests (gentle on the 2-worker engine)


def _now() -> float:
    return time.time()


def _save_fields(rec: dict) -> dict:
    """Store payload = record minus the transient doc_id key."""
    return {k: v for k, v in rec.items() if k != "doc_id"}


async def set_fields(ctx, rec: dict, **fields) -> dict:
    """Update record fields in place AND persist (if it has a store id)."""
    rec.update(fields)
    doc_id = rec.get("doc_id")
    if doc_id:
        await ctx.store.update(FILES_COLLECTION, doc_id, _save_fields(rec))
    return rec


async def touch(ctx, rec: dict) -> dict:
    """Mark accessed now — keeps the file warm so it isn't cold-evicted."""
    return await set_fields(ctx, rec, last_access_at=_now())


# ── Quota ─────────────────────────────────────────────────────────────────────


async def quota_state(ctx, account_email: str) -> tuple[int, int]:
    """(doc_count, total_bytes) currently held for this account."""
    files = await _all_picked_files(ctx, account_email)
    return len(files), sum(int(f.get("size_bytes") or 0) for f in files)


async def check_quota(ctx, account_email: str, adding: int = 1, adding_bytes: int = 0) -> None:
    """Raise a user-facing error if adding would exceed the per-account caps."""
    count, total = await quota_state(ctx, account_email)
    if count + adding > MAX_DOCS:
        raise RuntimeError(
            f"File limit reached ({MAX_DOCS} files). Remove some files before adding more."
        )
    if total + adding_bytes > MAX_BYTES:
        gb = MAX_BYTES / (1024 ** 3)
        raise RuntimeError(
            f"Storage limit reached ({gb:.0f} GB). Remove some files before adding more."
        )


# ── Indexing (heavy path — background / self-heal) ────────────────────────────


async def _drive_meta(ctx, acc: dict, file_id: str) -> dict:
    resp = await drive_get_metadata(ctx, acc, file_id)
    resp.raise_for_status()
    return resp.json()


async def index_record(ctx, acc: dict, rec: dict) -> dict:
    """Ingest this file into the engine (extract+store+embed), idempotent by
    content_key. Sets status=ready + document_id + content_key on success, or
    status=failed + error (and re-raises). Safe to call repeatedly."""
    acc = await _refresh_token_if_needed(ctx, acc)
    file_id = rec["file_id"]
    mime = rec.get("mime_type") or ""
    await set_fields(ctx, rec, status=INDEXING, error=None)
    try:
        meta = await _drive_meta(ctx, acc, file_id)
        key = file_types.content_key(mime, meta)
        url = file_types.build_fetch_url(file_id, mime)
        doc = await extractor.ingest(
            ctx, fetch_url=url, auth=acc["access_token"],
            content_key=key, filename=rec.get("name") or file_id,
        )
    except Exception as e:  # noqa: BLE001 - record the failure, then surface it
        await set_fields(ctx, rec, status=FAILED, error=str(e))
        raise
    if doc.get("status") not in extractor.READY_STATES:
        reason = doc.get("error") or f"could not index this file ({doc.get('error_code')})"
        await set_fields(ctx, rec, status=FAILED, error=reason)
        raise RuntimeError(reason)
    await set_fields(
        ctx, rec, status=READY, document_id=doc.get("document_id"),
        content_key=key, error=None, last_access_at=_now(),
    )
    return rec


async def ensure_ready(ctx, acc: dict, rec: dict) -> int:
    """Return a usable engine document_id if the file is READY. If not, raise
    NotReadyError — reads must NEVER trigger a heavy synchronous ingest (that
    caused the kernel timeout). Indexing runs in the background: index_pending,
    kicked at pick time and on the first not-ready access."""
    if rec.get("status") == READY and rec.get("document_id"):
        return rec["document_id"]
    raise NotReadyError(
        f"'{rec.get('name') or rec.get('file_id')}' is still being prepared "
        "(indexing in progress) — ask again in a moment."
    )


async def index_pending(ctx) -> dict:
    """Background job: index every not-yet-ready file of the ACTIVE account, in
    PARALLEL (bounded by _INDEX_CONCURRENCY). Idempotent (content_key cache) —
    safe to run repeatedly/concurrently; a cached file costs almost nothing.
    One bad file never stops the batch."""
    acc = await _active_account(ctx)
    acc = await _refresh_token_if_needed(ctx, acc)  # refresh once, shared across the batch
    email = _account_email(acc)
    targets = [
        r for r in await _all_picked_files(ctx, email)
        if r.get("mime_type") != GOOGLE_FOLDER_MIME  # folders aren't readable files
        and not (r.get("status") == READY and r.get("document_id"))
    ]
    sem = asyncio.Semaphore(_INDEX_CONCURRENCY)

    async def _one(rec) -> bool:
        async with sem:
            try:
                await index_record(ctx, acc, rec)
                return True
            except Exception:  # noqa: BLE001
                return False

    results = await asyncio.gather(*(_one(r) for r in targets))
    indexed = sum(1 for ok in results if ok)
    return {"indexed": indexed, "failed": len(results) - indexed}


# ── Records ───────────────────────────────────────────────────────────────────


async def resolve_record(ctx, acc: dict, file_id: str) -> dict:
    """The persistent store record for a readable file. If the file is readable
    only via a granted folder (no own record yet), materialise one so it's
    cached and counted like any picked file. Raises if not accessible."""
    picked = await _find_picked_file(ctx, file_id)  # raises with a clear reason if not granted
    if picked.get("doc_id"):
        return picked
    created = await ctx.store.create(FILES_COLLECTION, {
        "file_id": file_id, "name": picked.get("name"), "mime_type": picked.get("mime_type"),
        "size_bytes": picked.get("size_bytes", 0), "account_email": _account_email(acc),
        "status": PENDING, "document_id": None, "last_access_at": _now(),
    })
    rec = dict(created.data)
    rec["doc_id"] = created.id
    return rec


# ── Eviction ──────────────────────────────────────────────────────────────────


async def evict_cold(ctx) -> int:
    """Delete engine docs (PG+NC) for files untouched beyond TTL; KEEP the panel
    record (status→cold). Self-heals on next access. Returns evicted count.
    Best-effort — a failed delete just leaves the file warm for next sweep."""
    cutoff = _now() - COLD_AFTER_S
    evicted = 0
    for rec in await _all_picked_files(ctx):
        if rec.get("status") != READY or not rec.get("document_id"):
            continue
        if (rec.get("last_access_at") or 0) >= cutoff:
            continue
        try:
            await extractor.delete(ctx, rec["document_id"])
        except Exception:  # noqa: BLE001 - eviction never breaks the caller
            continue
        await set_fields(ctx, rec, status=COLD, document_id=None)
        evicted += 1
    return evicted


# ── Forget (single authority — panel + engine together, no drift) ─────────────


async def forget_file(ctx, file_id: str) -> None:
    """Remove a file from the panel AND delete its engine doc (PG+NC). One
    authority → no drift. Engine delete is best-effort; the local record is
    always removed. Raises if the file isn't connected."""
    files = await _all_picked_files(ctx)
    match = next((f for f in files if f.get("file_id") == file_id), None)
    if not match:
        raise RuntimeError(f"File {file_id!r} is not in the connected files list.")
    await _forget_one(ctx, match)


async def _forget_one(ctx, f: dict) -> None:
    doc_id = f.get("document_id")
    if doc_id:
        try:
            await extractor.delete(ctx, doc_id)  # engine (PG+NC), best-effort
        except Exception:  # noqa: BLE001
            pass
    await ctx.store.delete(FILES_COLLECTION, f["doc_id"])


async def forget_files(ctx, file_ids: list[str]) -> int:
    """BULK disconnect: remove many files (panel records + engine docs) in
    parallel. Unknown ids are skipped. Returns the count removed."""
    by_id = {f["file_id"]: f for f in await _all_picked_files(ctx)}
    targets = [by_id[fid] for fid in file_ids if fid in by_id]
    await asyncio.gather(*(_forget_one(ctx, f) for f in targets))
    return len(targets)


async def forget_account_files(ctx, account_email: str) -> int:
    """Delete every engine doc + store record for an account's files (used on
    account disconnect), in parallel. Returns the count removed."""
    files = await _all_picked_files(ctx, account_email)
    await asyncio.gather(*(_forget_one(ctx, f) for f in files))
    return len(files)


async def list_entries(ctx) -> list[dict]:
    """The active account's connected entries — files AND folders — each tagged
    is_folder, for the panel and the list_files tool. [] if no account."""
    accounts = await _all_accounts(ctx)
    if not accounts:
        return []
    acc = await _active_account(ctx)
    entries = await _all_picked_files(ctx, _account_email(acc))
    for e in entries:
        e["is_folder"] = e.get("mime_type") == GOOGLE_FOLDER_MIME
    return entries
