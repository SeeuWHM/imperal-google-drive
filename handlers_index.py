"""Google Drive · background indexing — index_files tool + fire-and-forget kicks.

Ingest (extract + embed) runs OFF the chat request path via ctx.background_task
(kernel, up to 1800s), so reads never block and a big file never hits the 180s
deadline. CRITICAL: a background coro MUST return an ActionResult (else the
kernel logs a critical audit + delivers a fallback error to chat) — so the kick
helpers wrap lifecycle results in ActionResult here, in the SDK layer.
"""
from __future__ import annotations

import logging

from imperal_sdk.chat.action_result import ActionResult

from app import chat
from providers import lifecycle
from providers.helpers import _active_account
from schemas import EmptyParams
from schemas_sdl import IndexResult, build_index_result

log = logging.getLogger("doc_reader")


# ── background job coroutines (MUST return ActionResult) ──────────────────────


async def _index_pending_job(ctx) -> ActionResult:
    res = await lifecycle.index_pending(ctx)
    tail = f" ({res['failed']} failed)" if res["failed"] else ""
    return ActionResult.success(
        data=build_index_result(res["indexed"], res["failed"]),
        summary=f"✅ Indexed {res['indexed']} file(s){tail}.",
        refresh_panels=["doc_files"],
    )


async def _reindex_one_job(ctx, file_id: str) -> ActionResult:
    try:
        acc = await _active_account(ctx)
        rec = await lifecycle.resolve_record(ctx, acc, file_id)
        await lifecycle.index_record(ctx, acc, rec)
        return ActionResult.success(data=build_index_result(1, 0), summary="Re-indexed after edit.")
    except Exception as e:  # noqa: BLE001
        return ActionResult.error(f"re-index failed: {e}", retryable=True)


# ── fire-and-forget kicks (call from any handler) ─────────────────────────────


async def kick_index(ctx) -> None:
    """Start background indexing of all pending files (no-op if ctx has no spawn
    hook, e.g. dev/test)."""
    try:
        await ctx.background_task(_index_pending_job(ctx), long_running=True, name="gdrive-index")
    except Exception as e:  # noqa: BLE001
        log.warning("could not start background index: %s", e)


async def kick_reindex(ctx, file_id: str) -> None:
    """Refresh ONE file's cache in the background after an edit — keeps edits
    fast; the old cache stays readable until the re-ingest replaces it."""
    try:
        await ctx.background_task(_reindex_one_job(ctx, file_id), long_running=True, name="gdrive-reindex")
    except Exception as e:  # noqa: BLE001
        log.warning("could not start background re-index: %s", e)


# ── @chat.function ────────────────────────────────────────────────────────────


@chat.function(
    "index_files", action_type="read", background=True, long_running=True,
    data_model=IndexResult,
    description=(
        "Index the user's picked Drive files (extract text + build the search index) so they can be "
        "read and searched. Runs in the background and indexes files in parallel — you'll get a message "
        "when done. Call after picking new files, or if a file reports it's still being prepared."
    ),
)
async def fn_index_files(ctx, params: EmptyParams) -> ActionResult:
    return await _index_pending_job(ctx)
