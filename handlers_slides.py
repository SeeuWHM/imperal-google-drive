"""Google Drive · Google Slides — read-only, via Drive's export-to-text.

Slides' native API (presentations.get) returns a deeply nested structure
(slide -> pageElement -> shape -> text -> textRun) — walking that ourselves
would just reimplement what Drive's files.export?mimeType=text/plain
already does in one call. Read-only by design: Slides does have its own
batchUpdate for edits, but that wasn't asked for here.
"""
from __future__ import annotations

import logging

from imperal_sdk.chat.action_result import ActionResult

from app import chat
from providers.google_api import drive_export_text
from providers.helpers import _active_account, _find_picked_file
from providers.text_windows import grep_lines, line_window
from schemas import ReadRangeParams, SearchParams
from schemas_sdl import SearchResults, TextWindow, build_search_results, build_text_window

log = logging.getLogger("doc_reader")


# ─── impl_* business logic ────────────────────────────────────────────── #


async def _get_full_text(ctx, file_id: str) -> str:
    acc = await _active_account(ctx)
    await _find_picked_file(ctx, file_id)
    resp = await drive_export_text(ctx, acc, file_id)
    resp.raise_for_status()
    return resp.content.decode("utf-8", errors="replace")


async def impl_read_presentation_text(ctx, file_id: str, offset: int, limit: int | None) -> tuple[str, bool, int]:
    text = await _get_full_text(ctx, file_id)
    return line_window(text, offset, limit)


async def impl_search_in_presentation(ctx, file_id: str, query: str, case_sensitive: bool) -> list[tuple[int, str]]:
    text = await _get_full_text(ctx, file_id)
    return grep_lines(text, query, case_sensitive)


# ─── @chat.function wrappers ──────────────────────────────────────────── #


@chat.function(
    "read_presentation_text", action_type="read",
    data_model=TextWindow,
    description="Read a Google Slides presentation's text content (all slides, exported as plain text by Google Drive). Read-only — Slides editing isn't supported. Returns line-numbered text. Omit limit to read the whole thing.",
)
async def fn_read_presentation_text(ctx, params: ReadRangeParams) -> ActionResult:
    try:
        numbered, has_more, total_lines = await impl_read_presentation_text(ctx, params.file_id, params.offset, params.limit)
        return ActionResult.success(
            data=build_text_window(params.file_id, numbered, params.offset, has_more, total_lines),
            summary=f"{total_lines} line(s) total" + (", more available" if has_more else ""),
        )
    except Exception as e:
        return ActionResult.error(str(e), retryable=False)


@chat.function(
    "search_in_presentation", action_type="read",
    data_model=SearchResults,
    description="Search a Google Slides presentation's text for a substring. Returns matching lines with line numbers.",
)
async def fn_search_in_presentation(ctx, params: SearchParams) -> ActionResult:
    try:
        matches = await impl_search_in_presentation(ctx, params.file_id, params.query, params.case_sensitive)
        return ActionResult.success(
            data=build_search_results(params.file_id, params.query, matches),
            summary=f"{len(matches)} matching line(s)." if matches else "No matches found.",
        )
    except Exception as e:
        return ActionResult.error(str(e), retryable=False)
