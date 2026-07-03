"""Google Drive · CONTENT-plane operations — read / search / overview, UNIFORM
for every file type (Doc, Sheet, Slides, PDF, PPTX, DOCX, XLSX, txt).

All content comes from the engine cache — never a live Drive download on read.
Indexing / self-heal is delegated to lifecycle. Token economy is enforced here:
overview is cheap (preview only), search returns top-K chunks (not whole files),
read is windowed with a hard ceiling so a huge file is never dumped at once.

SDK-free → fully unit-testable with a fake ctx.
"""
from __future__ import annotations

from . import extractor, lifecycle
from .helpers import _active_account
from .text_windows import grep_lines

DEFAULT_READ_LIMIT = 40_000    # chars returned when the caller omits a limit
MAX_READ_LIMIT = 200_000       # hard ceiling — never dump a whole huge file at once
FULLTEXT_LIMIT = 5_000_000     # engine cap — used for exact in-file grep
DEFAULT_SEARCH_K = 6
MAX_SEARCH_K = 20


async def read_file(ctx, file_id: str, offset: int = 0, limit: int | None = None) -> dict:
    """Windowed plain text of ANY file, from the engine cache. Omitting limit
    returns the first DEFAULT_READ_LIMIT chars with has_more — never the whole
    file blindly (that would blow the context and get silently truncated)."""
    acc = await _active_account(ctx)
    rec = await lifecycle.resolve_record(ctx, acc, file_id)
    document_id = await lifecycle.ensure_ready(ctx, acc, rec)
    await lifecycle.touch(ctx, rec)
    win = max(1, min(limit or DEFAULT_READ_LIMIT, MAX_READ_LIMIT))
    data = await extractor.read_text(ctx, document_id, offset=max(0, offset), limit=win)
    text = data.get("text", "")
    return {
        "file_id": file_id,
        "name": rec.get("name"),
        "text": text,
        "offset": data.get("offset", max(0, offset)),
        "returned_chars": len(text),
        "total_chars": data.get("total_chars", 0),
        "has_more": bool(data.get("truncated")),
    }


async def search_files(ctx, query: str, file_id: str | None = None, k: int | None = None) -> dict:
    """Two correct modes under one tool:
      - no file_id → SEMANTIC search across all indexed files (top-K chunks, the
        big token saver);
      - file_id    → EXACT substring grep over that one file's stored text
        (deterministic — engine RAG has no per-doc filter)."""
    acc = await _active_account(ctx)
    if file_id:
        rec = await lifecycle.resolve_record(ctx, acc, file_id)
        document_id = await lifecycle.ensure_ready(ctx, acc, rec)
        await lifecycle.touch(ctx, rec)
        data = await extractor.read_text(ctx, document_id, offset=0, limit=FULLTEXT_LIMIT)
        matches = grep_lines(data.get("text", ""), query)
        results = [{"label": f"line {ln}", "text": line} for ln, line in matches]
        return {"query": query, "file_id": file_id, "mode": "exact", "results": results}

    kk = max(1, min(k or DEFAULT_SEARCH_K, MAX_SEARCH_K))
    hits = await extractor.search(ctx, query, k=kk)
    results = [
        {"label": f"{h.get('filename') or '?'}#{h.get('seq')}",
         "text": h.get("text", ""), "score": h.get("score")}
        for h in hits
    ]
    return {"query": query, "file_id": None, "mode": "semantic", "results": results}


async def file_overview(ctx, file_id: str) -> dict:
    """Cheap 'what is this file' — metadata + status always, plus the engine
    preview if it's already indexed. Does NOT force indexing (stays cheap)."""
    acc = await _active_account(ctx)
    rec = await lifecycle.resolve_record(ctx, acc, file_id)
    out = {
        "file_id": file_id,
        "name": rec.get("name"),
        "mime_type": rec.get("mime_type"),
        "size_bytes": rec.get("size_bytes"),
        "status": rec.get("status"),
        "preview": None,
    }
    if rec.get("status") == lifecycle.READY and rec.get("document_id"):
        try:
            meta = await extractor.overview(ctx, rec["document_id"])
            out["preview"] = meta.get("preview")
        except Exception:  # noqa: BLE001 - preview is best-effort, never fail overview
            pass
        await lifecycle.touch(ctx, rec)
    return out
