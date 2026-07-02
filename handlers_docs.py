"""Google Drive · Google Docs read/write/search/stats.

Edits go through the Docs API directly (batchUpdate) — every successful call
IS the live document, immediately. No local draft, no save step, no session
to manage (unlike an embedded editor with autosave)."""
from __future__ import annotations

import logging

from imperal_sdk.chat.action_result import ActionResult

from app import chat
from providers.google_api import docs_batch_update, docs_get, document_end_index, walk_document_text
from providers.helpers import _active_account, _find_picked_file
from providers.text_windows import grep_lines, line_window
from schemas import AppendTextParams, FileIdParams, OverwriteTextParams, ReadRangeParams, ReplaceTextParams, SearchParams
from schemas_sdl import DocStats, EditResult, SearchResults, TextWindow, build_doc_stats, build_edit_result, build_search_results, build_text_window

log = logging.getLogger("doc_reader")


# ─── impl_* business logic ────────────────────────────────────────────── #


async def _get_full_text(ctx, file_id: str) -> str:
    acc = await _active_account(ctx)
    await _find_picked_file(ctx, file_id)
    resp = await docs_get(ctx, acc, file_id)
    resp.raise_for_status()
    return walk_document_text(resp.json())


async def impl_read_document_text(ctx, file_id: str, offset: int, limit: int | None) -> tuple[str, bool, int]:
    text = await _get_full_text(ctx, file_id)
    return line_window(text, offset, limit)


async def impl_search_in_document(ctx, file_id: str, query: str, case_sensitive: bool) -> list[tuple[int, str]]:
    text = await _get_full_text(ctx, file_id)
    return grep_lines(text, query, case_sensitive)


async def impl_get_document_stats(ctx, file_id: str) -> tuple[int, int, int]:
    text = await _get_full_text(ctx, file_id)
    char_count = len(text)
    word_count = len(text.split())
    paragraph_count = len([p for p in text.split("\n") if p.strip()])
    return char_count, word_count, paragraph_count


async def impl_replace_text_in_document(ctx, file_id: str, find_text: str, replace_text: str, match_case: bool) -> int:
    acc = await _active_account(ctx)
    await _find_picked_file(ctx, file_id)
    requests = [{
        "replaceAllText": {
            "containsText": {"text": find_text, "matchCase": match_case},
            "replaceText": replace_text,
        }
    }]
    resp = await docs_batch_update(ctx, acc, file_id, requests)
    resp.raise_for_status()
    replies = resp.json().get("replies", [])
    occurrences = replies[0].get("replaceAllText", {}).get("occurrencesChanged", 0) if replies else 0
    if occurrences == 0:
        raise RuntimeError(f"No occurrences of {find_text!r} found — nothing was changed. Check the exact text with read_document_text first.")
    return occurrences


async def impl_append_text_to_document(ctx, file_id: str, text: str) -> None:
    acc = await _active_account(ctx)
    await _find_picked_file(ctx, file_id)
    requests = [{"insertText": {"endOfSegmentLocation": {}, "text": text}}]
    resp = await docs_batch_update(ctx, acc, file_id, requests)
    resp.raise_for_status()


async def impl_overwrite_document_text(ctx, file_id: str, content: str) -> None:
    acc = await _active_account(ctx)
    await _find_picked_file(ctx, file_id)
    doc_resp = await docs_get(ctx, acc, file_id)
    doc_resp.raise_for_status()
    end_index = document_end_index(doc_resp.json())

    requests = []
    if end_index > 1:
        requests.append({"deleteContentRange": {"range": {"startIndex": 1, "endIndex": end_index - 1}}})
    if content:
        requests.append({"insertText": {"location": {"index": 1}, "text": content}})
    if not requests:
        return
    resp = await docs_batch_update(ctx, acc, file_id, requests)
    resp.raise_for_status()


# ─── @chat.function wrappers ──────────────────────────────────────────── #


@chat.function(
    "read_document_text", action_type="read",
    data_model=TextWindow,
    description="Read a Google Doc's text content. Returns line-numbered text (like a source file). Omit limit to read the whole document — use offset/limit only for very long documents.",
)
async def fn_read_document_text(ctx, params: ReadRangeParams) -> ActionResult:
    try:
        numbered, has_more, total_lines = await impl_read_document_text(ctx, params.file_id, params.offset, params.limit)
        return ActionResult.success(
            data=build_text_window(params.file_id, numbered, params.offset, has_more, total_lines),
            summary=f"{total_lines} line(s) total" + (", more available" if has_more else ""),
        )
    except Exception as e:
        return ActionResult.error(str(e), retryable=False)


@chat.function(
    "search_in_document", action_type="read",
    data_model=SearchResults,
    description="Search a Google Doc's text for a substring. Returns matching lines with line numbers — use this instead of read_document_text when you only need to locate something in a long document.",
)
async def fn_search_in_document(ctx, params: SearchParams) -> ActionResult:
    try:
        matches = await impl_search_in_document(ctx, params.file_id, params.query, params.case_sensitive)
        return ActionResult.success(
            data=build_search_results(params.file_id, params.query, matches),
            summary=f"{len(matches)} matching line(s)." if matches else "No matches found.",
        )
    except Exception as e:
        return ActionResult.error(str(e), retryable=False)


@chat.function(
    "get_document_stats", action_type="read",
    data_model=DocStats,
    description="Get exact character/word/paragraph counts for a Google Doc, computed precisely (not estimated). Use this for questions like 'how many words/characters does this document have' instead of counting from read_document_text output.",
)
async def fn_get_document_stats(ctx, params: FileIdParams) -> ActionResult:
    try:
        char_count, word_count, paragraph_count = await impl_get_document_stats(ctx, params.file_id)
        return ActionResult.success(
            data=build_doc_stats(params.file_id, char_count, word_count, paragraph_count),
            summary=f"{char_count} characters, {word_count} words, {paragraph_count} paragraphs.",
        )
    except Exception as e:
        return ActionResult.error(str(e), retryable=False)


@chat.function(
    "replace_text_in_document", action_type="write", event="file.edited",
    data_model=EditResult,
    description="Find-and-replace exact text in a Google Doc — changes the real document immediately. Fails with a clear error if find_text has no matches (check the exact wording with read_document_text first).",
)
async def fn_replace_text_in_document(ctx, params: ReplaceTextParams) -> ActionResult:
    try:
        occurrences = await impl_replace_text_in_document(ctx, params.file_id, params.find_text, params.replace_text, params.match_case)
        return ActionResult.success(
            data=build_edit_result(params.file_id, occurrences_changed=occurrences),
            summary=f"Replaced {occurrences} occurrence(s).",
            refresh_panels=["doc_files"],
        )
    except Exception as e:
        return ActionResult.error(str(e), retryable=False)


@chat.function(
    "append_text_to_document", action_type="write", event="file.edited",
    data_model=EditResult,
    description="Add text to the end of a Google Doc — changes the real document immediately.",
)
async def fn_append_text_to_document(ctx, params: AppendTextParams) -> ActionResult:
    try:
        await impl_append_text_to_document(ctx, params.file_id, params.text)
        return ActionResult.success(data=build_edit_result(params.file_id), summary="Text appended.", refresh_panels=["doc_files"])
    except Exception as e:
        return ActionResult.error(str(e), retryable=False)


@chat.function(
    "overwrite_document_text", action_type="write", event="file.edited",
    data_model=EditResult,
    description="Replace a Google Doc's entire content with new text — changes the real document immediately. Use for a full rewrite; use replace_text_in_document for a targeted edit instead.",
)
async def fn_overwrite_document_text(ctx, params: OverwriteTextParams) -> ActionResult:
    try:
        await impl_overwrite_document_text(ctx, params.file_id, params.content)
        return ActionResult.success(data=build_edit_result(params.file_id), summary="Document overwritten.", refresh_panels=["doc_files"])
    except Exception as e:
        return ActionResult.error(str(e), retryable=False)
