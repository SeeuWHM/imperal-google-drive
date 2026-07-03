"""Google Drive · CONTENT plane — read_file / search_files / file_overview.

Thin @chat.function wrappers over providers/content_ops (all logic + tests live
there). ONE read tool, ONE search tool, ONE overview tool — identical for every
file type (Doc, Sheet, Slides, PDF, PPTX, DOCX, XLSX, text). All content comes
from the engine cache; no live Drive download on read.
"""
from __future__ import annotations

import logging

from imperal_sdk.chat.action_result import ActionResult

from app import chat
from handlers_index import kick_index
from providers import content_ops, lifecycle
from schemas import FileIdParams, ReadFileParams, SearchFilesParams
from schemas_sdl import (
    FileOverview,
    FileText,
    SearchResults,
    build_file_overview,
    build_file_text,
    build_search_results,
)

log = logging.getLogger("doc_reader")


@chat.function(
    "read_file", action_type="read", data_model=FileText,
    description=(
        "Read the text of ANY connected Drive file — Google Doc/Sheet/Slides or "
        "PDF/PPTX/DOCX/XLSX/plain text. Returns a windowed slice (character offset/limit). "
        "For a long file, prefer search_files to jump to the relevant part instead of reading it all."
    ),
)
async def fn_read_file(ctx, params: ReadFileParams) -> ActionResult:
    try:
        data = await content_ops.read_file(ctx, params.file_id, params.offset, params.limit)
        more = " — more available (use offset to continue)" if data["has_more"] else ""
        return ActionResult.success(
            data=build_file_text(data),
            summary=f"{data['returned_chars']} of {data['total_chars']} char(s){more}.",
        )
    except lifecycle.NotReadyError as e:
        await kick_index(ctx)   # start (or continue) background indexing off the request path
        return ActionResult.error(str(e), retryable=False)   # show the "preparing, ask again" message
    except Exception as e:
        return ActionResult.error(str(e), retryable=False)


@chat.function(
    "search_files", action_type="read", data_model=SearchResults,
    description=(
        "Search the user's connected Drive files. Omit file_id to search across ALL files by "
        "meaning (semantic — returns the most relevant snippets, token-cheap). Give file_id to "
        "find an exact substring within ONE file. Use this instead of read_file when you only "
        "need to locate something."
    ),
)
async def fn_search_files(ctx, params: SearchFilesParams) -> ActionResult:
    try:
        data = await content_ops.search_files(ctx, params.query, file_id=(params.file_id or None))
        n = len(data["results"])
        return ActionResult.success(
            data=build_search_results(data),
            summary=f"{n} result(s) ({data['mode']})." if n else "No matches found.",
        )
    except lifecycle.NotReadyError as e:
        await kick_index(ctx)
        return ActionResult.error(str(e), retryable=False)
    except Exception as e:
        return ActionResult.error(str(e), retryable=False)


@chat.function(
    "file_overview", action_type="read", data_model=FileOverview,
    description=(
        "A quick, cheap look at one file — its type, size, indexing status and a short preview — "
        "without reading the whole thing. Use to answer 'what is this file' before deciding to read it."
    ),
)
async def fn_file_overview(ctx, params: FileIdParams) -> ActionResult:
    try:
        data = await content_ops.file_overview(ctx, params.file_id)
        return ActionResult.success(
            data=build_file_overview(data),
            summary=f"{data.get('name')} — {data.get('status')}.",
        )
    except Exception as e:
        return ActionResult.error(str(e), retryable=False)
