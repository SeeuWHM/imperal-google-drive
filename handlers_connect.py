"""Doc Reader · Connect, Picker, file list, and disconnect handlers."""
from __future__ import annotations

import logging
from urllib.parse import urlencode

from imperal_sdk.chat.action_result import ActionResult

from app import chat
from providers.helpers import (
    FILES_COLLECTION,
    PICKER_PAGE_URL,
    _active_account,
    _all_accounts,
    _remove_picked_file,
    reconcile_picked_files,
)
from schemas import EmptyParams, FileIdParams, RegisterPickedFilesParams
from schemas_sdl import (
    DocFileList,
    EditResult,
    OAuthConnectResult,
    PickerLinkResult,
    build_doc_file_list,
    build_edit_result,
    build_oauth_connect,
    build_picker_link,
)

log = logging.getLogger("doc_reader")


# ─── impl_* business logic ────────────────────────────────────────────── #


async def impl_connect(ctx) -> tuple[str | None, bool, str | None]:
    accounts = await _all_accounts(ctx)
    if accounts:
        return None, True, None
    url = await ctx.oauth_authorize_url("google-docs")
    return url, False, (
        "Open the link to authorise Google Drive access, then call open_file_picker "
        "to choose which files Doc Reader may see (drive.file scope — only picked files "
        "are accessible, nothing else in the user's Drive)."
    )


async def impl_open_file_picker(ctx) -> str:
    """Builds the Picker page URL. The page itself does its own client-side
    Google auth and shows picked files as copy-paste JSON — there is no
    backend callback (see extensions/doc-reader.md: ctx.as_user() requires
    system context, which a webhook handler doesn't have, so a webhook
    can't safely attribute a pick to a specific user without an unverified
    workaround; the user relaying the result back through chat sidesteps
    that entirely with zero platform assumptions)."""
    client_id = await ctx.secrets.get("google_client_id")
    api_key = await ctx.secrets.get("google_picker_api_key")
    if not client_id or not api_key:
        raise RuntimeError(
            "Doc Reader's Google OAuth Client ID / Picker API Key are not configured yet "
            "(google_client_id / google_picker_api_key app secrets)."
        )
    query = urlencode({"client_id": client_id, "api_key": api_key})
    return f"{PICKER_PAGE_URL}?{query}"


async def impl_list_connected_files(ctx) -> list[dict]:
    """Live-reconciles against Google before returning — a file the user
    deleted or unshared from the app on Google's side will already be
    pruned from the result (see providers.helpers.reconcile_picked_files)."""
    accounts = await _all_accounts(ctx)
    if not accounts:
        return []
    acc = await _active_account(ctx)
    return await reconcile_picked_files(ctx, acc)


async def impl_register_picked_files(ctx, files: list) -> int:
    existing = {f.get("file_id") for f in await reconcile_picked_files(ctx, await _active_account(ctx))}
    added = 0
    for f in files:
        if f.file_id in existing:
            continue
        await ctx.store.create(FILES_COLLECTION, {
            "file_id": f.file_id, "name": f.name, "mime_type": f.mime_type, "size_bytes": f.size_bytes,
        })
        added += 1
    return added


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
    "open_file_picker", action_type="read",
    data_model=PickerLinkResult,
    description="Get a link to the Google Picker page where the user selects which Drive files Doc Reader may access. After picking, the page shows a JSON block — ask the user to paste it back so you can call register_picked_files with it.",
)
async def fn_open_file_picker(ctx, params: EmptyParams) -> ActionResult:
    try:
        url = await impl_open_file_picker(ctx)
        return ActionResult.success(data=build_picker_link(url), summary="Picker link ready — open it, pick files, then paste the result back here.")
    except Exception as e:
        return ActionResult.error(str(e), retryable=False)


@chat.function(
    "register_picked_files", action_type="write", event="file.connected",
    data_model=EditResult,
    description="Register files the user just picked in the Google Picker page — pass the exact 'files' array from the JSON the page displayed. Skips files already registered.",
)
async def fn_register_picked_files(ctx, params: RegisterPickedFilesParams) -> ActionResult:
    try:
        added = await impl_register_picked_files(ctx, params.files)
        return ActionResult.success(
            data=build_edit_result("+".join(f.file_id for f in params.files) or "none"),
            summary=f"{added} new file(s) registered." if added else "No new files (already registered).",
            refresh_panels=["doc_files"],
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
            summary=f"{len(files)} file(s) available." if files else "No files picked yet — call open_file_picker first.",
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
