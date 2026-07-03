"""Google Drive · ACTION-plane operations — the ONLY place file type matters,
because writing can only go through the native Google API.

Every successful edit re-ingests the file into the engine (best-effort) so
read_file / search_files stay fresh. Find-and-replace runs on the LIVE Google
doc (server-side exact match), so a slightly stale cache never corrupts an edit.

SDK-free → fully unit-testable with a fake ctx.
"""
from __future__ import annotations

import logging

from . import lifecycle
from .google_api import (
    docs_batch_update,
    docs_get,
    document_end_index,
    drive_upload_media,
    sheets_get_values,
    sheets_update_values,
)
from .helpers import _active_account
from .spreadsheet_math import compute_aggregate

log = logging.getLogger("doc_reader")

_WRITABLE_PREFIXES = ("text/",)
_WRITABLE_EXACT = {"application/json", "application/xml", "application/x-yaml"}


def _is_writable_text(mime: str) -> bool:
    return mime.startswith(_WRITABLE_PREFIXES) or mime in _WRITABLE_EXACT


async def _reindex(ctx, acc: dict, rec: dict) -> None:
    """Refresh the engine cache after a write — best-effort: the edit already
    succeeded on Google, so a failed re-ingest must not surface as an error."""
    try:
        await lifecycle.index_record(ctx, acc, rec)
    except Exception as e:  # noqa: BLE001
        log.warning("post-edit re-ingest failed for %s: %s", rec.get("file_id"), e)


# ── Google Docs ───────────────────────────────────────────────────────────────


async def edit_document(ctx, file_id: str, op: str, *, find_text: str | None = None,
                        replace_text: str | None = None, match_case: bool = False,
                        text: str | None = None, content: str | None = None) -> dict:
    """op = replace | append | overwrite. Changes the live Google Doc, then
    re-ingests. `replace` raises if find_text has no match (nothing changed)."""
    acc = await _active_account(ctx)
    rec = await lifecycle.resolve_record(ctx, acc, file_id)  # auth + record for re-ingest

    if op == "replace":
        requests = [{"replaceAllText": {
            "containsText": {"text": find_text, "matchCase": match_case},
            "replaceText": replace_text or "",
        }}]
        resp = await docs_batch_update(ctx, acc, file_id, requests)
        resp.raise_for_status()
        replies = resp.json().get("replies", [])
        occ = replies[0].get("replaceAllText", {}).get("occurrencesChanged", 0) if replies else 0
        if occ == 0:
            raise RuntimeError(
                f"No occurrences of {find_text!r} found — nothing was changed. "
                "Check the exact wording with read_file first."
            )
        result = {"op": "replace", "occurrences": occ}

    elif op == "append":
        requests = [{"insertText": {"endOfSegmentLocation": {}, "text": text or ""}}]
        resp = await docs_batch_update(ctx, acc, file_id, requests)
        resp.raise_for_status()
        result = {"op": "append"}

    elif op == "overwrite":
        doc_resp = await docs_get(ctx, acc, file_id)
        doc_resp.raise_for_status()
        end_index = document_end_index(doc_resp.json())
        requests = []
        if end_index > 1:
            requests.append({"deleteContentRange": {"range": {"startIndex": 1, "endIndex": end_index - 1}}})
        if content:
            requests.append({"insertText": {"location": {"index": 1}, "text": content}})
        if requests:
            resp = await docs_batch_update(ctx, acc, file_id, requests)
            resp.raise_for_status()
        result = {"op": "overwrite"}

    else:
        raise ValueError(f"unknown op {op!r} (use replace | append | overwrite)")

    await _reindex(ctx, acc, rec)
    return result


# ── Google Sheets ─────────────────────────────────────────────────────────────


async def edit_spreadsheet(ctx, file_id: str, cell_range: str, values: list) -> dict:
    """Write a 2D array into an A1 range on the live sheet, then re-ingest."""
    acc = await _active_account(ctx)
    rec = await lifecycle.resolve_record(ctx, acc, file_id)
    resp = await sheets_update_values(ctx, acc, file_id, cell_range, values)
    resp.raise_for_status()
    await _reindex(ctx, acc, rec)
    return {"updated": True, "range": cell_range}


async def spreadsheet_compute(ctx, file_id: str, cell_range: str, operation: str) -> dict:
    """Exact sum/count/average/min/max over a range, computed in code (not
    estimated from a text dump). Read-only — no re-ingest."""
    acc = await _active_account(ctx)
    await lifecycle.resolve_record(ctx, acc, file_id)  # auth
    resp = await sheets_get_values(ctx, acc, file_id, cell_range)
    resp.raise_for_status()
    values = resp.json().get("values", [])
    result, count = compute_aggregate(values, operation)
    return {"operation": operation, "range": cell_range, "result": result, "cell_count": count}


# ── Plain text files ──────────────────────────────────────────────────────────


async def write_text_file(ctx, file_id: str, content: str) -> dict:
    """Overwrite a genuinely text-based Drive file (text/JSON/XML/YAML), then
    re-ingest. Refuses binary formats (PDF/DOCX/etc — read-only)."""
    acc = await _active_account(ctx)
    rec = await lifecycle.resolve_record(ctx, acc, file_id)
    mime = rec.get("mime_type") or "text/plain"
    if not _is_writable_text(mime):
        raise RuntimeError(
            f"Cannot overwrite {rec.get('name', file_id)!r} as plain text — its format "
            f"({mime}) is not text-based. Only text/JSON/XML/YAML files are writable."
        )
    resp = await drive_upload_media(ctx, acc, file_id, content.encode("utf-8"), mime_type=mime)
    resp.raise_for_status()
    await _reindex(ctx, acc, rec)
    return {"saved": True}
