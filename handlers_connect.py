"""Doc Reader · Connect, file list, and disconnect handlers."""
from __future__ import annotations

import logging

from imperal_sdk.chat.action_result import ActionResult

from app import chat
from providers.helpers import _active_account, _all_accounts, _remove_picked_file, reconcile_picked_files
from schemas import EmptyParams, FileIdParams
from schemas_sdl import DocFileList, EditResult, OAuthConnectResult, build_doc_file_list, build_edit_result, build_oauth_connect

log = logging.getLogger("doc_reader")


# ─── impl_* business logic ────────────────────────────────────────────── #


async def impl_connect(ctx) -> tuple[str | None, bool, str | None]:
    accounts = await _all_accounts(ctx)
    if accounts:
        return None, True, None
    url = await ctx.oauth_authorize_url("google-docs")
    return url, False, (
        "Open the link to authorise Google Drive access, then use the Google Picker "
        "to choose which files Doc Reader may see (drive.file scope — only picked files "
        "are accessible, nothing else in the user's Drive)."
    )


async def impl_list_connected_files(ctx) -> list[dict]:
    """Live-reconciles against Google before returning — a file the user
    deleted or unshared from the app on Google's side will already be
    pruned from the result (see providers.helpers.reconcile_picked_files)."""
    accounts = await _all_accounts(ctx)
    if not accounts:
        return []
    acc = await _active_account(ctx)
    return await reconcile_picked_files(ctx, acc)


async def impl_disconnect_file(ctx, file_id: str) -> None:
    """Removes the file from Doc Reader's own tracking only. This does NOT
    revoke the underlying Google OAuth grant for that file — Drive has no
    per-file revoke API; the user would need to revoke the whole app's
    access from myaccount.google.com/permissions to do that."""
    await _remove_picked_file(ctx, file_id)


# ─── @chat.function wrappers ──────────────────────────────────────────── #


@chat.function(
    "connect_google_docs", action_type="read",
    data_model=OAuthConnectResult,
    description="Start Google Drive OAuth for Doc Reader — returns an authorisation URL to open in the browser. If already connected, returns that status instead of a new URL.",
)
async def fn_connect_google_docs(ctx, params: EmptyParams) -> ActionResult:
    try:
        url, already, instruction = await impl_connect(ctx)
        return ActionResult.success(
            data=build_oauth_connect(url, already, instruction),
            summary="Already connected." if already else "OAuth URL ready.",
        )
    except Exception as e:
        return ActionResult.error(str(e), retryable=False)


@chat.function(
    "list_connected_files", action_type="read",
    data_model=DocFileList,
    description="List the Google Drive files the user has picked for Doc Reader to access (name, mime type, last modified). Does not return file content. Automatically drops files the user deleted or unshared on Google's side.",
)
async def fn_list_connected_files(ctx, params: EmptyParams) -> ActionResult:
    try:
        files = await impl_list_connected_files(ctx)
        return ActionResult.success(
            data=build_doc_file_list(files),
            summary=f"{len(files)} file(s) available." if files else "No files picked yet — connect and use the Google Picker first.",
        )
    except Exception as e:
        return ActionResult.error(str(e), retryable=False)


@chat.function(
    "disconnect_file", action_type="write", event="file.disconnected",
    data_model=EditResult,
    description="Remove a file from Doc Reader's connected-files list. The Google file itself is untouched — this only stops Doc Reader from tracking/reading it.",
)
async def fn_disconnect_file(ctx, params: FileIdParams) -> ActionResult:
    try:
        await impl_disconnect_file(ctx, params.file_id)
        return ActionResult.success(data=build_edit_result(params.file_id), summary="File removed from Doc Reader.", refresh_panels=["doc_files"])
    except Exception as e:
        return ActionResult.error(str(e), retryable=False)
