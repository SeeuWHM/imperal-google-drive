"""CONTENT plane — UNIVERSAL, list-based, parallel.

Every content operation takes a LIST of file_ids and handles 1..N uniformly —
one tool, used the same way for a single file or many. The kernel runs tool
calls sequentially, so "study N files at once" MUST fan out INSIDE the tool
(asyncio.gather + Semaphore) — that's the only real parallelism (proven pattern,
mail extension). All content comes from the engine cache (never a live Drive
read); token-bounded. Per-file errors never fail the batch.

SDK-free → fully unit-testable with a fake ctx.
"""
from __future__ import annotations

import asyncio

from . import extractor, lifecycle
from .helpers import _active_account
from .text_windows import grep_lines

DEFAULT_READ_LIMIT = 40_000    # chars for a single-file read window
MULTI_READ_LIMIT = 4_000       # per-file window when reading several at once (gist, token-safe)
MAX_READ_LIMIT = 200_000       # hard ceiling per file
FULLTEXT_LIMIT = 5_000_000     # engine cap for exact in-file grep
DEFAULT_SEARCH_K = 6
MAX_SEARCH_K = 20
_CONCURRENCY = 5               # parallel engine calls per bulk op (self-throttle; ctx.http has no cap)


async def read_files(ctx, file_ids: list[str], offset: int = 0, limit: int | None = None) -> list[dict]:
    """Read 1..N files in parallel. One id → full window; many → a bounded
    preview each (token-safe). Each result carries status ok|preparing|error —
    a not-ready or broken file never fails the others."""
    acc = await _active_account(ctx)
    multi = len(file_ids) > 1
    per = max(1, min(limit or (MULTI_READ_LIMIT if multi else DEFAULT_READ_LIMIT), MAX_READ_LIMIT))
    sem = asyncio.Semaphore(_CONCURRENCY)

    async def _one(fid: str) -> dict:
        async with sem:
            try:
                rec = await lifecycle.resolve_record(ctx, acc, fid)
            except Exception as e:  # noqa: BLE001 - not accessible / not picked
                return {"file_id": fid, "status": "error", "message": str(e), "text": ""}
            try:
                document_id = await lifecycle.ensure_ready(ctx, acc, rec)
            except lifecycle.NotReadyError:
                return {"file_id": fid, "name": rec.get("name"), "status": "preparing", "text": ""}
            try:
                await lifecycle.touch(ctx, rec)
                data = await extractor.read_text(ctx, document_id, offset=max(0, offset), limit=per)
                text = data.get("text", "")
                return {"file_id": fid, "name": rec.get("name"), "text": text,
                        "offset": data.get("offset", 0), "returned_chars": len(text),
                        "total_chars": data.get("total_chars", 0),
                        "has_more": bool(data.get("truncated")), "status": "ok"}
            except Exception as e:  # noqa: BLE001
                return {"file_id": fid, "name": rec.get("name"), "status": "error", "message": str(e), "text": ""}

    return await asyncio.gather(*(_one(f) for f in file_ids))


async def file_overview(ctx, file_ids: list[str]) -> list[dict]:
    """Cheap 'what are these files' for 1..N in parallel — metadata + status,
    plus the engine preview if already indexed. Does not force indexing."""
    acc = await _active_account(ctx)
    sem = asyncio.Semaphore(_CONCURRENCY)

    async def _one(fid: str) -> dict:
        async with sem:
            try:
                rec = await lifecycle.resolve_record(ctx, acc, fid)
            except Exception as e:  # noqa: BLE001
                return {"file_id": fid, "status": "error", "message": str(e)}
            out = {"file_id": fid, "name": rec.get("name"), "mime_type": rec.get("mime_type"),
                   "size_bytes": rec.get("size_bytes"), "status": rec.get("status"), "preview": None}
            if rec.get("status") == lifecycle.READY and rec.get("document_id"):
                try:
                    meta = await extractor.overview(ctx, rec["document_id"])
                    out["preview"] = meta.get("preview")
                    await lifecycle.touch(ctx, rec)
                except Exception:  # noqa: BLE001 - preview is best-effort
                    pass
            return out

    return await asyncio.gather(*(_one(f) for f in file_ids))


async def search_files(ctx, query: str, file_ids: list[str] | None = None, k: int | None = None) -> dict:
    """Two correct modes under one universal tool:
      - no file_ids → SEMANTIC search across ALL indexed files (one engine call
        already studies everything — top-K chunks, the big token saver);
      - file_ids → EXACT substring grep across THOSE files, in PARALLEL."""
    acc = await _active_account(ctx)
    if file_ids:
        sem = asyncio.Semaphore(_CONCURRENCY)

        async def _one(fid: str) -> list[dict]:
            async with sem:
                try:
                    rec = await lifecycle.resolve_record(ctx, acc, fid)
                    document_id = await lifecycle.ensure_ready(ctx, acc, rec)
                    await lifecycle.touch(ctx, rec)
                    data = await extractor.read_text(ctx, document_id, offset=0, limit=FULLTEXT_LIMIT)
                    name = rec.get("name") or fid
                    return [{"label": f"{name} · line {ln}", "text": line}
                            for ln, line in grep_lines(data.get("text", ""), query)]
                except Exception:  # noqa: BLE001 - a not-ready/broken file just contributes nothing
                    return []

        groups = await asyncio.gather(*(_one(f) for f in file_ids))
        results = [hit for g in groups for hit in g]
        return {"query": query, "mode": "exact", "results": results}

    kk = max(1, min(k or DEFAULT_SEARCH_K, MAX_SEARCH_K))
    hits = await extractor.search(ctx, query, k=kk)
    results = [{"label": f"{h.get('filename') or '?'}#{h.get('seq')}",
                "text": h.get("text", ""), "score": h.get("score")} for h in hits]
    return {"query": query, "mode": "semantic", "results": results}
