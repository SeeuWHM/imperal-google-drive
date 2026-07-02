"""Doc Reader · Connect, Picker, file list, and disconnect handlers."""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets as _secrets
from urllib.parse import urlencode

from imperal_sdk.chat.action_result import ActionResult

from app import chat
from cache_models import PendingPickerSession
from providers.helpers import (
    FILES_COLLECTION,
    PICKER_CLAIM_URL,
    PICKER_PAGE_URL,
    PICKER_STAGE_TOKEN_URL,
    _active_account,
    _all_accounts,
    _remove_picked_file,
    reconcile_picked_files,
)
from providers.token_refresh import _refresh_token_if_needed
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

_CACHE_KEY = "pending_picker_session"
_SESSION_TTL_SECONDS = 280  # ctx.cache hard cap is 300s; matches the file-relay's own TTL on doc-extractor-service


def _sign_session(secret: str, session: str) -> str:
    return hmac.new(secret.encode(), session.encode(), hashlib.sha256).hexdigest()


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
    """Builds the Picker page URL. The page does NOT run its own Google
    login — confirmed empirically that a drive.file grant obtained via a
    separate client-side OAuth is invisible to this extension's server-side
    refresh-token grant later (different grant lineage, even same
    client_id/user/scope). Instead: refresh OUR stored token, stage it
    (HMAC-signed, 30s TTL) for the page to fetch once, so picked files end
    up under the SAME lineage this extension already reads with."""
    api_key = await ctx.secrets.get("google_picker_api_key")
    hmac_secret = await ctx.secrets.get("picker_hmac_secret")
    if not api_key or not hmac_secret:
        raise RuntimeError(
            "Doc Reader's Picker API Key / HMAC secret are not configured yet "
            "(google_picker_api_key / picker_hmac_secret app secrets)."
        )

    acc = await _active_account(ctx)  # raises clearly if connect_google_docs hasn't run yet
    acc = await _refresh_token_if_needed(ctx, acc)

    session = _secrets.token_urlsafe(24)
    sig = _sign_session(hmac_secret, session)

    resp = await ctx.http.post(
        PICKER_STAGE_TOKEN_URL,
        json={"session": session, "access_token": acc["access_token"], "sig": sig},
        timeout=10,
    )
    resp.raise_for_status()

    await ctx.cache.set(_CACHE_KEY, PendingPickerSession(token=session), ttl_seconds=_SESSION_TTL_SECONDS)
    query = urlencode({"api_key": api_key, "session": session, "sig": sig})
    return f"{PICKER_PAGE_URL}?{query}"


async def _claim_pending_picker_session(ctx) -> int:
    """Best-effort: if the user has an open picker session, ask
    doc-extractor-service's relay whether files were staged for it, and if
    so register them and clear the session. Silent no-op if there's nothing
    pending or the relay call fails — this runs as a side-effect of listing
    files, it must never break that read."""
    try:
        pending = await ctx.cache.get(_CACHE_KEY, PendingPickerSession)
    except Exception:
        return 0
    if not pending:
        return 0

    try:
        resp = await ctx.http.get(f"{PICKER_CLAIM_URL}/{pending.token}", timeout=10)
        resp.raise_for_status()
        body = resp.json()
        files = (body.get("data") or {}).get("files") if body.get("success") else None
    except Exception:
        return 0

    if not files:
        return 0  # not picked yet — leave the session cached, TTL handles cleanup

    existing = {f.get("file_id") for f in await _all_picked_files_raw(ctx)}
    added = 0
    for f in files:
        if f.get("file_id") in existing:
            continue
        await ctx.store.create(FILES_COLLECTION, {
            "file_id": f.get("file_id"), "name": f.get("name"),
            "mime_type": f.get("mime_type"), "size_bytes": f.get("size_bytes", 0),
        })
        added += 1
    try:
        await ctx.cache.delete(_CACHE_KEY)
    except Exception:
        pass
    return added


async def _all_picked_files_raw(ctx) -> list[dict]:
    from providers.helpers import _all_picked_files
    return await _all_picked_files(ctx)


async def impl_list_connected_files(ctx) -> list[dict]:
    """Claims any pending Picker session first (so a file the user just
    picked shows up immediately), then live-reconciles against Google — a
    file the user deleted or unshared from the app on Google's side will
    already be pruned from the result."""
    await _claim_pending_picker_session(ctx)
    accounts = await _all_accounts(ctx)
    if not accounts:
        return []
    acc = await _active_account(ctx)
    return await reconcile_picked_files(ctx, acc)


async def impl_register_picked_files(ctx, files: list) -> int:
    """Manual fallback path — used only if the picker page's automatic
    staging call failed and it fell back to showing copy-paste JSON."""
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
    description="Get a link to the Google Picker page where the user selects which Drive files Doc Reader may access. Requires connect_google_docs to have been completed first. Opens in a new browser tab. Picked files are usually registered automatically — just call list_connected_files afterwards to check.",
)
async def fn_open_file_picker(ctx, params: EmptyParams) -> ActionResult:
    try:
        url = await impl_open_file_picker(ctx)
        return ActionResult.success(data=build_picker_link(url), summary="Picker link ready — open it and pick files, then check list_connected_files.")
    except Exception as e:
        return ActionResult.error(str(e), retryable=False)


@chat.function(
    "register_picked_files", action_type="write", event="file.connected",
    data_model=EditResult,
    description="Manual fallback: register files from the JSON the Picker page showed, ONLY if it displayed a copy-paste box instead of confirming automatically (rare — means the automatic path failed). Skips files already registered.",
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
    description="List the Google Drive files the user has picked for Doc Reader to access (name, mime type, last modified). Does not return file content. Automatically picks up files just selected in the Picker, and drops files the user deleted or unshared on Google's side.",
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
