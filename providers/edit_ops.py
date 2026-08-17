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
    drive_export_text,
    drive_upload_media,
    sheets_append_values,
    sheets_get_metadata,
    sheets_get_values,
    sheets_update_values,
    slides_batch_update,
    walk_document_text,
)
from .helpers import GOOGLE_DOC_MIME, GOOGLE_SLIDE_MIME, _active_account
from .spreadsheet_math import compute_aggregate

log = logging.getLogger("doc_reader")

_WRITABLE_PREFIXES = ("text/",)
_WRITABLE_EXACT = {"application/json", "application/xml", "application/x-yaml"}


def _is_writable_text(mime: str) -> bool:
    return mime.startswith(_WRITABLE_PREFIXES) or mime in _WRITABLE_EXACT


class AmbiguousReplaceError(RuntimeError):
    """Raised when find_text matches more than once and the caller did not
    explicitly pass replace_all=True. Carries the real count + short context
    snippets so the caller can either narrow find_text or confirm the bulk
    replace on purpose — never both silently."""
    def __init__(self, find_text: str, count: int, snippets: list[str]):
        self.find_text = find_text
        self.count = count
        self.snippets = snippets
        shown = "; ".join(f"…{s}…" for s in snippets[:5])
        more = f" (+{count - 5} more)" if count > 5 else ""
        super().__init__(
            f"{find_text!r} matches {count} places in this document, not 1 — "
            f"replacing it would change all {count} at once, which is very likely "
            f"NOT what was intended: {shown}{more}. Either make find_text more "
            "specific (include surrounding words so it's unique), or pass "
            "replace_all=true if you really do mean to change every occurrence."
        )


def _count_occurrences(haystack: str, needle: str, match_case: bool) -> int:
    if not needle:
        return 0
    if match_case:
        return haystack.count(needle)
    return haystack.lower().count(needle.lower())


def _context_snippets(haystack: str, needle: str, match_case: bool, radius: int = 24) -> list[str]:
    """Short 'before…match…after' windows around each hit — lets the caller
    (and the user, in the error message) SEE why a find_text was ambiguous
    without re-reading the whole file."""
    hay = haystack if match_case else haystack.lower()
    ndl = needle if match_case else needle.lower()
    out, start = [], 0
    while True:
        i = hay.find(ndl, start)
        if i == -1:
            break
        lo, hi = max(0, i - radius), min(len(haystack), i + len(needle) + radius)
        out.append(haystack[lo:hi].replace("\n", " "))
        start = i + len(needle)
    return out


async def _live_document_text(ctx, acc: dict, file_id: str, mime: str) -> str:
    """The CURRENT text of the live Doc/Slides file, straight from the Google
    API — never the (possibly stale) engine cache — so the preflight count in
    edit_document always reflects reality at the moment of the write."""
    if mime == GOOGLE_SLIDE_MIME:
        resp = await drive_export_text(ctx, acc, file_id, "text/plain")
        resp.raise_for_status()
        return resp.text()
    doc_resp = await docs_get(ctx, acc, file_id)
    doc_resp.raise_for_status()
    return walk_document_text(doc_resp.json())


# NOTE: edit_ops itself never re-indexes — that lives one layer up, in the SDK
# handlers (handlers_index.py). Single-file edits AWAIT it (await_reindex) so
# the reply only comes back once the cache is fresh; multi-file bulk paths
# still fire it in the BACKGROUND (kick_reindex/kick_bulk_reindex) so a large
# batch can't hang the panel. See handlers_index.py's docstrings for why.


# ── Google Docs / Slides ───────────────────────────────────────────────────────


async def edit_document(ctx, file_id: str, op: str, *, find_text: str | None = None,
                        replace_text: str | None = None, match_case: bool = False,
                        text: str | None = None, content: str | None = None,
                        replace_all: bool = False) -> dict:
    """op = replace | append | overwrite. Changes the live Google Doc or Google
    Slides presentation (routed by the picked file's mime type), then
    re-ingests. `replace` raises if find_text has no match (nothing changed),
    and ALSO raises (AmbiguousReplaceError) if it matches more than once
    UNLESS replace_all=True — see module docstring for why."""
    acc = await _active_account(ctx)
    rec = await lifecycle.resolve_record(ctx, acc, file_id)  # auth + record for re-ingest
    mime = rec.get("mime_type") or ""

    if mime == GOOGLE_SLIDE_MIME:
        return await _edit_slides(ctx, acc, file_id, op, find_text=find_text,
                                  replace_text=replace_text, match_case=match_case,
                                  replace_all=replace_all)

    if mime and mime != GOOGLE_DOC_MIME:
        raise RuntimeError(
            f"Cannot edit {rec.get('name', file_id)!r} as a Google Doc — its type ({mime}) "
            "isn't a Google Doc. Use edit_spreadsheet for Sheets, write_text_file for plain-text "
            "files, or edit_document (replace only) for Google Slides."
        )

    if op == "replace":
        if not find_text:
            raise ValueError("find_text is required for op=replace")
        live_text = await _live_document_text(ctx, acc, file_id, GOOGLE_DOC_MIME)
        pre_count = _count_occurrences(live_text, find_text, match_case)
        if pre_count == 0:
            raise RuntimeError(
                f"No occurrences of {find_text!r} found — nothing was changed. "
                "Check the exact wording with read_file first."
            )
        if pre_count > 1 and not replace_all:
            raise AmbiguousReplaceError(
                find_text, pre_count, _context_snippets(live_text, find_text, match_case),
            )
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
        if occ != pre_count:
            # The live document moved between our preflight read and the write
            # itself (a genuine race, e.g. a concurrent edit) — occurrencesChanged
            # is the actual authority, but this must never be silently swallowed.
            log.warning(
                "edit_document replace: preflight counted %d occurrence(s) of %r but "
                "Google reported %d changed (file %s) — live doc changed mid-request",
                pre_count, find_text, occ, file_id,
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

    return result


async def _edit_slides(ctx, acc: dict, file_id: str, op: str, *, find_text: str | None,
                       replace_text: str | None, match_case: bool,
                       replace_all: bool = False) -> dict:
    """Google Slides only supports `replace` (find-and-replace across every
    slide) — the Slides API has no single linear body to append to or
    overwrite the way Docs does (a presentation is a tree of slides/shapes),
    so append/overwrite aren't meaningfully implementable the same way."""
    if op != "replace":
        raise RuntimeError(
            f"Google Slides only supports op='replace' (find-and-replace across every slide) — "
            f"'{op}' has no equivalent for a presentation (no single body to append to or "
            "overwrite). Edit slide text with find_text/replace_text instead."
        )
    if not find_text:
        raise ValueError("find_text is required for op=replace")
    live_text = await _live_document_text(ctx, acc, file_id, GOOGLE_SLIDE_MIME)
    pre_count = _count_occurrences(live_text, find_text, match_case)
    if pre_count == 0:
        raise RuntimeError(
            f"No occurrences of {find_text!r} found on any slide — nothing was changed. "
            "Check the exact wording with read_file first."
        )
    if pre_count > 1 and not replace_all:
        raise AmbiguousReplaceError(
            find_text, pre_count, _context_snippets(live_text, find_text, match_case),
        )
    requests = [{"replaceAllText": {
        "containsText": {"text": find_text, "matchCase": match_case},
        "replaceText": replace_text or "",
    }}]
    resp = await slides_batch_update(ctx, acc, file_id, requests)
    resp.raise_for_status()
    replies = resp.json().get("replies", [])
    occ = replies[0].get("replaceAllText", {}).get("occurrencesChanged", 0) if replies else 0
    if occ == 0:
        raise RuntimeError(
            f"No occurrences of {find_text!r} found on any slide — nothing was changed. "
            "Check the exact wording with read_file first."
        )
    if occ != pre_count:
        log.warning(
            "edit_document replace (slides): preflight counted %d occurrence(s) of %r but "
            "Google reported %d changed (file %s) — live deck changed mid-request",
            pre_count, find_text, occ, file_id,
        )
    return {"op": "replace", "occurrences": occ}


# ── Google Sheets ─────────────────────────────────────────────────────────────


async def edit_spreadsheet(ctx, file_id: str, cell_range: str, values: list) -> dict:
    """Write a 2D array into an A1 range on the live sheet, then re-ingest."""
    acc = await _active_account(ctx)
    rec = await lifecycle.resolve_record(ctx, acc, file_id)
    resp = await sheets_update_values(ctx, acc, file_id, cell_range, values)
    resp.raise_for_status()
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


async def _first_sheet_name(ctx, acc, file_id: str) -> str:
    resp = await sheets_get_metadata(ctx, acc, file_id)
    resp.raise_for_status()
    sheets = resp.json().get("sheets", [])
    return sheets[0]["properties"]["title"] if sheets else "Sheet1"


async def get_spreadsheet_info(ctx, file_id: str) -> list[dict]:
    """Tab names + dimensions — needed before addressing a range by name or
    deciding where to append."""
    acc = await _active_account(ctx)
    await lifecycle.resolve_record(ctx, acc, file_id)  # auth
    resp = await sheets_get_metadata(ctx, acc, file_id)
    resp.raise_for_status()
    out = []
    for s in resp.json().get("sheets", []):
        props = s.get("properties", {})
        grid = props.get("gridProperties", {})
        out.append({"name": props.get("title", "?"),
                    "row_count": grid.get("rowCount", 0),
                    "column_count": grid.get("columnCount", 0)})
    return out


async def read_spreadsheet_range(ctx, file_id: str, cell_range: str) -> list[list]:
    """Raw cell values for an A1 range (structured, not the text dump)."""
    acc = await _active_account(ctx)
    await lifecycle.resolve_record(ctx, acc, file_id)  # auth
    resp = await sheets_get_values(ctx, acc, file_id, cell_range)
    resp.raise_for_status()
    return resp.json().get("values", [])


async def append_spreadsheet_rows(ctx, file_id: str, rows: list, cell_range: str = "") -> int:
    """Append rows AFTER the existing data — no need to compute the target row.
    Defaults to the first sheet."""
    acc = await _active_account(ctx)
    await lifecycle.resolve_record(ctx, acc, file_id)  # auth
    target = cell_range or await _first_sheet_name(ctx, acc, file_id)
    resp = await sheets_append_values(ctx, acc, file_id, target, rows)
    resp.raise_for_status()
    return len(rows)


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
    return {"saved": True}
