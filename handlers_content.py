"""CONTENT plane — read_files / file_overview / search_files.

UNIVERSAL & list-based: each tool takes file_ids and handles 1..N the same way,
fanning out in parallel inside (the kernel runs tool calls sequentially, so bulk
must parallelize here). Thin wrappers over providers/content_ops (logic + tests
live there). All content from the engine cache.
"""
from __future__ import annotations

import logging

from imperal_sdk.chat.action_result import ActionResult

from app import chat
from handlers_index import kick_index
from providers import content_ops
from schemas import OverviewParams, ReadFilesParams, SearchFilesParams
from schemas_sdl import (
    FileOverviewList,
    FileTextList,
    SearchResults,
    build_file_overview_list,
    build_file_text_list,
    build_search_results,
)

log = logging.getLogger("doc_reader")


@chat.function(
    "read_files", action_type="read", data_model=FileTextList,
    description=(
        "Read the text of ONE OR MANY Drive files at once — pass a single file_id or several and they "
        "are read in PARALLEL. Works for any type (Doc/Sheet/Slides/PDF/PPTX/DOCX/XLSX/text). One file → "
        "full window; several → a preview of each. A file still indexing comes back marked 'preparing' "
        "(indexing is kicked automatically — ask again shortly). Use offset/limit to page a long single file."
    ),
)
async def fn_read_files(ctx, params: ReadFilesParams) -> ActionResult:
    try:
        results = await content_ops.read_files(ctx, params.file_ids, params.offset, params.limit)
        if any(r.get("status") == "preparing" for r in results):
            await kick_index(ctx)   # start background indexing for the not-ready ones
        ok = sum(1 for r in results if r.get("status") == "ok")
        prep = sum(1 for r in results if r.get("status") == "preparing")
        summary = f"{ok}/{len(results)} file(s) read" + (f", {prep} still preparing" if prep else "") + "."
        return ActionResult.success(data=build_file_text_list(results), summary=summary)
    except Exception as e:
        return ActionResult.error(str(e), retryable=False)


@chat.function(
    "file_overview", action_type="read", data_model=FileOverviewList,
    description=(
        "Quick, cheap overview of ONE OR MANY files at once (type, size, indexing status, short preview) — "
        "pass one or several file_ids. Use to decide which files to actually read."
    ),
)
async def fn_file_overview(ctx, params: OverviewParams) -> ActionResult:
    try:
        results = await content_ops.file_overview(ctx, params.file_ids)
        return ActionResult.success(data=build_file_overview_list(results), summary=f"{len(results)} file(s).")
    except Exception as e:
        return ActionResult.error(str(e), retryable=False)


@chat.function(
    "search_files", action_type="read", data_model=SearchResults,
    description=(
        "Search the user's Drive files. Omit file_ids to search ALL of them at once by MEANING (semantic — "
        "one pass over everything, token-cheap) — this is the way to 'study all my files'. Give file_ids to "
        "grep exact text across those specific files (in parallel). Prefer this over reading whole files when "
        "you just need to find something."
    ),
)
async def fn_search_files(ctx, params: SearchFilesParams) -> ActionResult:
    try:
        data = await content_ops.search_files(ctx, params.query, file_ids=(params.file_ids or None))
        n = len(data["results"])
        return ActionResult.success(
            data=build_search_results(data),
            summary=f"{n} result(s) ({data['mode']})." if n else "No matches found.",
        )
    except Exception as e:
        return ActionResult.error(str(e), retryable=False)
