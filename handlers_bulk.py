"""Google Drive · Panel bulk file actions — multi-select toolbar dispatcher.

The files List in the panel is selectable (checkboxes on hover / ctrl-click
equivalent) with a bulk_actions toolbar. The SDK injects the selected item
ids as 'message_ids' (a fixed renderer contract, same field name every other
extension's bulk list uses — see mail-client's inbox bulk bar). This module
is the single dispatcher for both bulk verbs so the panel only needs one
ui.Call target, matching the mail-client pattern (mail_action).

Billing note: both bulk actions are ONE tool call regardless of how many
files are selected (pricing_model: per_action, like every other connector in
this portfolio) — selecting 20 files and hitting "Remove" cost the same as
selecting 1. The heavy work (many small file ops) happens server-side inside
the single call, fanned out in parallel exactly like read_files already does.
"""
from __future__ import annotations

import logging

from imperal_sdk.chat.action_result import ActionResult

from app import chat
from handlers_index import kick_bulk_reindex
from providers import lifecycle
from schemas import FileBulkActionParams
from schemas_sdl import BulkFileActionResult, build_bulk_file_action_result

log = logging.getLogger("doc_reader")


@chat.function(
    "file_bulk_action", action_type="write", event="file.bulk_action",
    data_model=BulkFileActionResult,
    description=(
        "Panel UI bulk dispatcher for the multi-selected files in the file list — called by the "
        "panel's bulk toolbar, not LLM chat. From chat use disconnect_files() or index_files() instead."
    ),
)
async def fn_file_bulk_action(ctx, params: FileBulkActionParams) -> ActionResult:
    ids = params.message_ids
    if not ids:
        return ActionResult.error("No files selected.", retryable=False)
    try:
        if params.action == "remove":
            affected = await lifecycle.forget_files(ctx, ids)
            return ActionResult.success(
                data=build_bulk_file_action_result("remove", affected),
                summary=f"Removed {affected} file(s).",
                refresh_panels=["doc_files"],
            )
        else:  # retry_index
            # Fire-and-forget (2026-08-16): ingest() now polls the engine up
            # to 90s PER FILE for a terminal status (fixed the "could not
            # index this file (None)" race) — running that synchronously here
            # made a multi-file retry take up to minutes, which is exactly
            # what made the panel feel hung. The click now returns instantly;
            # the real indexed/failed result is delivered as a follow-up chat
            # message + a doc_files panel refresh once the job finishes.
            await kick_bulk_reindex(ctx, ids)
            return ActionResult.success(
                data=build_bulk_file_action_result("retry_index", 0, 0),
                summary=f"Re-indexing {len(ids)} file(s) in the background — you'll get an update shortly.",
            )
    except Exception as e:
        return ActionResult.error(str(e), retryable=False)
