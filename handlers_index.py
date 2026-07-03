"""Google Drive · background indexing — index_files (@background, long_running).

Ingest (extract + embed) runs OFF the chat request path: the kernel's
ctx.background_task gives up to 1800s, so a big file never hits the 180s tool
deadline. Reads stay instant (they read the already-built cache). Logic lives
in providers/lifecycle (index_pending); this is the thin @chat.function tool.
"""
from __future__ import annotations

import logging

from imperal_sdk.chat.action_result import ActionResult

from app import chat
from providers import lifecycle
from schemas import EmptyParams
from schemas_sdl import IndexResult, build_index_result

log = logging.getLogger("doc_reader")


@chat.function(
    "index_files", action_type="read", background=True, long_running=True,
    data_model=IndexResult,
    description=(
        "Index the user's picked Drive files (extract text + build the search index) so they can be "
        "read and searched. Runs in the background — you'll get a message when it's done. Call after "
        "picking new files, or if a file reports it's still being prepared."
    ),
)
async def fn_index_files(ctx, params: EmptyParams) -> ActionResult:
    res = await lifecycle.index_pending(ctx)
    parts = [f"{res['indexed']} file(s) ready"]
    if res["failed"]:
        parts.append(f"{res['failed']} failed")
    return ActionResult.success(
        data=build_index_result(res["indexed"], res["failed"]),
        summary=", ".join(parts) + ".",
        refresh_panels=["doc_files"],
    )
