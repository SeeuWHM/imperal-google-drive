"""Google Drive · Plain Drive files (not Google-native Docs/Sheets) —
read/write/search.

READ path routes through doc-extractor-service's **server-side url-fetch**:
we hand it the Drive media URL + a fresh drive.file token, and IT downloads
the raw bytes (ctx.http in the extension cannot return raw binary — it decodes
non-JSON bodies to text and mangles them), extracts text, stores it, and
embeds it under source='gdrive' scoped to this user's imperal_id. The LLM
never sees raw bytes — only extracted text (windowed) or, later, relevant
chunks. Extraction+embedding are cached by content hash, so re-reading the
same file costs no re-extraction/re-embedding.

Write stays a direct Drive media upload (extractor is read-only) and is
guarded: only genuinely plain-text mime types are writable.
"""
from __future__ import annotations

import logging

from imperal_sdk.chat.action_result import ActionResult

from app import chat
from providers.google_api import drive_upload_media
from providers.helpers import DOC_EXTRACTOR_URL, DRIVE_API, _active_account, _find_picked_file
from providers.text_windows import grep_lines, line_window
from providers.token_refresh import _refresh_token_if_needed
from schemas import OverwriteTextParams, ReadRangeParams, SearchParams
from schemas_sdl import EditResult, SearchResults, TextWindow, build_edit_result, build_search_results, build_text_window

log = logging.getLogger("doc_reader")

_SOURCE = "gdrive"
_WRITABLE_MIME_PREFIXES = ("text/",)
_WRITABLE_MIME_EXACT = {"application/json", "application/xml", "application/x-yaml"}


def _is_writable_as_text(mime_type: str) -> bool:
    return mime_type.startswith(_WRITABLE_MIME_PREFIXES) or mime_type in _WRITABLE_MIME_EXACT


def _imperal_id(ctx) -> str:
    user = getattr(ctx, "user", None)
    uid = getattr(user, "imperal_id", None) if user else None
    if not uid:
        raise RuntimeError("no user context (imperal_id) — cannot scope file storage")
    return uid


async def _ingest_via_extractor(ctx, file_id: str, picked: dict) -> dict:
    """Have doc-extractor-service fetch the Drive file itself (real bytes),
    extract + store + embed under (source=gdrive, imperal_id). Returns the
    DocumentOut dict; raises a clear reason if the file couldn't be read."""
    acc = await _active_account(ctx)
    acc = await _refresh_token_if_needed(ctx, acc)
    media_url = f"{DRIVE_API}/files/{file_id}?alt=media"
    # content_key lets the extractor skip the download+extract+embed entirely
    # when the file is unchanged — so a re-read (or a retry after a kernel
    # timeout on a big file's first ingest) is an instant cache hit, not
    # another multi-MB download. file_id + size changes whenever the content
    # size changes; cheap (no extra Drive metadata call).
    content_key = f"{file_id}:{picked.get('size_bytes', 0)}"
    resp = await ctx.http.post(
        f"{DOC_EXTRACTOR_URL}/v1/documents",
        data={
            "source": _SOURCE,
            "imperal_id": _imperal_id(ctx),
            "url": media_url,
            "auth": acc["access_token"],
            "filename": picked.get("name", file_id),
            "content_key": content_key,
        },
        timeout=120,
    )
    resp.raise_for_status()
    body = resp.json()
    if not body.get("success"):
        raise RuntimeError(body.get("error", {}).get("message", "ingest failed"))
    doc = body["data"]["documents"][0]
    if doc.get("status") not in ("processed", "cached"):
        # clear, machine-derived reason: unsupported / corrupt / timeout / etc.
        raise RuntimeError(doc.get("error") or f"could not read this file ({doc.get('error_code')})")
    return doc


async def _get_file_text(ctx, file_id: str, picked: dict) -> str:
    doc = await _ingest_via_extractor(ctx, file_id, picked)
    resp = await ctx.http.get(
        f"{DOC_EXTRACTOR_URL}/v1/documents/{doc['document_id']}/text",
        params={"source": _SOURCE, "imperal_id": _imperal_id(ctx), "offset": 0, "limit": 5_000_000},
        timeout=60,
    )
    resp.raise_for_status()
    return (resp.json().get("data") or {}).get("text", "")


# ─── impl_* business logic ────────────────────────────────────────────── #


async def impl_read_text_file(ctx, file_id: str, offset: int, limit: int | None) -> tuple[str, bool, int]:
    picked = await _find_picked_file(ctx, file_id)
    text = await _get_file_text(ctx, file_id, picked)
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
    picked = await _find_picked_file(ctx, file_id)
    text = await _get_file_text(ctx, file_id, picked)
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
