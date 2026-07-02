"""Doc Reader · Plain Drive files (not Google-native Docs/Sheets) —
read/write/search.

Read always routes through doc-extractor-service (see
extensions/doc-extractor-service.md) — its magic-byte detection + per-format
extractors handle PDF/DOCX/XLSX/PPTX/text/etc uniformly, so this extension
never needs its own duplicate decode/parse logic. The LLM never sees raw
bytes — only the extracted text, exactly the "goes through the extractor,
then the LLM" routing that was asked for.

Write stays Drive-media-upload directly (the extractor is read-only) and is
guarded: only genuinely plain-text mime types are writable — a PDF/DOCX
can't be "overwritten" as text without destroying the original format.
"""
from __future__ import annotations

import logging

from imperal_sdk.chat.action_result import ActionResult

from app import chat
from providers.google_api import drive_download_media, drive_upload_media
from providers.helpers import DOC_EXTRACTOR_URL, _active_account, _find_picked_file
from providers.text_windows import grep_lines, line_window
from schemas import OverwriteTextParams, ReadRangeParams, SearchParams
from schemas_sdl import EditResult, SearchResults, TextWindow, build_edit_result, build_search_results, build_text_window

log = logging.getLogger("doc_reader")

_WRITABLE_MIME_PREFIXES = ("text/",)
_WRITABLE_MIME_EXACT = {"application/json", "application/xml", "application/x-yaml"}


def _is_writable_as_text(mime_type: str) -> bool:
    return mime_type.startswith(_WRITABLE_MIME_PREFIXES) or mime_type in _WRITABLE_MIME_EXACT


async def _extract_text(ctx, filename: str, data: bytes, mime_type: str) -> str:
    resp = await ctx.http.post(
        f"{DOC_EXTRACTOR_URL}/v1/extract",
        files={"file": (filename or "file", data, mime_type or "application/octet-stream")},
        timeout=60,
    )
    resp.raise_for_status()
    body = resp.json()
    if not body.get("success"):
        raise RuntimeError(body.get("error", {}).get("message", "extraction failed"))
    result = body["data"]
    if not result.get("extraction_acceptable"):
        raise RuntimeError(result.get("extraction_error") or "could not extract text from this file")
    return result["text"]


# ─── impl_* business logic ────────────────────────────────────────────── #


async def impl_read_text_file(ctx, file_id: str, offset: int, limit: int | None) -> tuple[str, bool, int]:
    acc = await _active_account(ctx)
    picked = await _find_picked_file(ctx, file_id)
    resp = await drive_download_media(ctx, acc, file_id)
    resp.raise_for_status()
    text = await _extract_text(ctx, picked.get("name", file_id), resp.content, picked.get("mime_type", ""))
    return line_window(text, offset, limit)


async def impl_write_text_file(ctx, file_id: str, content: str) -> None:
    acc = await _active_account(ctx)
    picked = await _find_picked_file(ctx, file_id)
    mime_type = picked.get("mime_type") or "text/plain"
    if not _is_writable_as_text(mime_type):
        raise RuntimeError(
            f"Cannot overwrite {picked.get('name', file_id)!r} as plain text — its format "
            f"({mime_type}) is not a plain-text type. write_text_file only supports genuinely "
            "text-based files (mime type starting with text/, or JSON/XML/YAML)."
        )
    resp = await drive_upload_media(ctx, acc, file_id, content.encode("utf-8"), mime_type=mime_type)
    resp.raise_for_status()


async def impl_search_in_text_file(ctx, file_id: str, query: str, case_sensitive: bool) -> list[tuple[int, str]]:
    acc = await _active_account(ctx)
    picked = await _find_picked_file(ctx, file_id)
    resp = await drive_download_media(ctx, acc, file_id)
    resp.raise_for_status()
    text = await _extract_text(ctx, picked.get("name", file_id), resp.content, picked.get("mime_type", ""))
    return grep_lines(text, query, case_sensitive)


# ─── @chat.function wrappers ──────────────────────────────────────────── #


@chat.function(
    "read_text_file", action_type="read",
    data_model=TextWindow,
    description="Read a file from the user's Drive that is NOT a Google Doc/Sheet — plain text, PDF, DOCX, XLSX, PPTX, or similar. Text is always extracted first (never raw bytes). Returns line-numbered text. Omit limit to read the whole file.",
)
async def fn_read_text_file(ctx, params: ReadRangeParams) -> ActionResult:
    try:
        numbered, has_more, total_lines = await impl_read_text_file(ctx, params.file_id, params.offset, params.limit)
        return ActionResult.success(
            data=build_text_window(params.file_id, numbered, params.offset, has_more, total_lines),
            summary=f"{total_lines} line(s) total" + (", more available" if has_more else ""),
        )
    except Exception as e:
        return ActionResult.error(str(e), retryable=False)


@chat.function(
    "write_text_file", action_type="write", event="file.edited",
    data_model=EditResult,
    description="Overwrite a plain text file's entire content in the user's Drive. Only works for genuinely text-based files (not PDF/DOCX/etc — those are read-only). Changes the real file immediately.",
)
async def fn_write_text_file(ctx, params: OverwriteTextParams) -> ActionResult:
    try:
        await impl_write_text_file(ctx, params.file_id, params.content)
        return ActionResult.success(data=build_edit_result(params.file_id), summary="File saved.", refresh_panels=["doc_files"])
    except Exception as e:
        return ActionResult.error(str(e), retryable=False)


@chat.function(
    "search_in_text_file", action_type="read",
    data_model=SearchResults,
    description="Search a Drive file (PDF/DOCX/XLSX/PPTX/text) for a substring — text is extracted first, then searched. Returns matching lines with line numbers.",
)
async def fn_search_in_text_file(ctx, params: SearchParams) -> ActionResult:
    try:
        matches = await impl_search_in_text_file(ctx, params.file_id, params.query, params.case_sensitive)
        return ActionResult.success(
            data=build_search_results(params.file_id, params.query, matches),
            summary=f"{len(matches)} matching line(s)." if matches else "No matches found.",
        )
    except Exception as e:
        return ActionResult.error(str(e), retryable=False)
