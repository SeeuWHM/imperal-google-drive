"""Google Drive · Connect, Picker, file/folder listing, disconnect.

Picker/OAuth/HMAC handoff is unchanged (works). What changed for the unified
model: picked files land with status="pending" and kick BACKGROUND indexing;
list_files shows files AND folders with their status; open_folder drills into a
granted folder; disconnect deletes the engine cache too (via lifecycle, single
authority → no drift).
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets as _secrets
import time
from urllib.parse import urlencode

from imperal_sdk.chat.action_result import ActionResult

from app import chat
from cache_models import PendingPickerSession
from providers import lifecycle
from providers.google_api import drive_list_folder
from providers.helpers import (
    FILES_COLLECTION,
    GOOGLE_FOLDER_MIME,
    PICKER_CLAIM_URL,
    PICKER_PAGE_URL,
    PICKER_STAGE_TOKEN_URL,
    _account_by_email,
    _account_email,
    _active_account,
    _all_picked_files,
)
from providers.token_refresh import _refresh_token_if_needed
from schemas import EmptyParams, FileIdParams, FolderParams, PickFilesParams, RegisterPickedFilesParams
from schemas_sdl import (
    DocFileList,
    EditResult,
    FolderContents,
    OAuthConnectResult,
    PickerLinkResult,
    build_doc_file_list,
    build_edit_result,
    build_folder_contents,
    build_oauth_connect,
    build_picker_link,
)

log = logging.getLogger("doc_reader")

_CACHE_KEY = "pending_picker_session"
_SESSION_TTL_SECONDS = 280  # ctx.cache hard cap is 300s; matches the relay's own TTL


def _sign_session(secret: str, session: str) -> str:
    return hmac.new(secret.encode(), session.encode(), hashlib.sha256).hexdigest()


# ─── impl_* business logic ────────────────────────────────────────────── #


async def impl_connect(ctx) -> tuple[str | None, bool, str | None]:
    url = await ctx.oauth_authorize_url("google")
    return url, False, (
        "Open the link to authorise Google Drive access. You can connect several "
        "Google accounts — each keeps its own separate pool of picked files. After "
        "authorising, call open_file_picker to choose which files/folders Google Drive "
        "may see (drive.file scope — only picked items are accessible, nothing else)."
    )


async def impl_open_file_picker(ctx, account: str = "") -> str:
    """Picker page URL for a specific account. Stages a fresh access token
    (HMAC-signed, short TTL) so the page hands Picker our own drive.file grant
    (setAppId binds it to this project — without it picked files 404)."""
    api_key = await ctx.secrets.get("google_picker_api_key")
    hmac_secret = await ctx.secrets.get("picker_hmac_secret")
    if not api_key or not hmac_secret:
        raise RuntimeError(
            "Google Drive's Picker API Key / HMAC secret are not configured yet "
            "(google_picker_api_key / picker_hmac_secret app secrets)."
        )
    acc = await _account_by_email(ctx, account) if account else await _active_account(ctx)
    acc = await _refresh_token_if_needed(ctx, acc)

    session = _secrets.token_urlsafe(24)
    sig = _sign_session(hmac_secret, session)
    resp = await ctx.http.post(
        PICKER_STAGE_TOKEN_URL,
        json={"session": session, "access_token": acc["access_token"], "sig": sig},
        timeout=10,
    )
    resp.raise_for_status()
    await ctx.cache.set(
        _CACHE_KEY,
        PendingPickerSession(token=session, account_email=_account_email(acc)),
        ttl_seconds=_SESSION_TTL_SECONDS,
    )
    query = urlencode({"api_key": api_key, "session": session, "sig": sig})
    return f"{PICKER_PAGE_URL}?{query}"


async def _claim_pending_picker_session(ctx) -> int:
    """If an open picker session staged files, register them as status=pending
    (bounded by quota) and clear the session. Silent no-op otherwise — it runs
    as a side effect of list_files and must never break that read."""
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
        return 0  # not picked yet — TTL cleans the session up

    account_email = pending.account_email or _account_email(await _active_account(ctx))
    existing = await _all_picked_files(ctx, account_email)
    existing_ids = {f.get("file_id") for f in existing}
    count = len(existing)
    total = sum(int(f.get("size_bytes") or 0) for f in existing)
    added = 0
    for f in files:
        if f.get("file_id") in existing_ids:
            continue
        if count >= lifecycle.MAX_DOCS:
            break  # quota — remaining picks are dropped (register_picked_files reports it)
        sz = int(f.get("size_bytes") or 0)
        if total + sz > lifecycle.MAX_BYTES:
            continue
        await ctx.store.create(FILES_COLLECTION, {
            "file_id": f.get("file_id"), "name": f.get("name"),
            "mime_type": f.get("mime_type"), "size_bytes": sz,
            "account_email": account_email,
            "status": lifecycle.PENDING, "document_id": None, "last_access_at": time.time(),
        })
        count += 1
        total += sz
        added += 1
    try:
        await ctx.cache.delete(_CACHE_KEY)
    except Exception:
        pass
    return added


async def impl_register_picked_files(ctx, files: list) -> int:
    """Manual fallback (picker page showed copy-paste JSON). Quota-checked;
    new files land as status=pending."""
    acc = await _active_account(ctx)
    email = _account_email(acc)
    existing = await _all_picked_files(ctx, email)
    existing_ids = {f.get("file_id") for f in existing}
    count = len(existing)
    total = sum(int(f.get("size_bytes") or 0) for f in existing)
    added = 0
    for f in files:
        if f.file_id in existing_ids:
            continue
        if count >= lifecycle.MAX_DOCS:
            raise RuntimeError(f"File limit reached ({lifecycle.MAX_DOCS}). Remove some files before adding more.")
        sz = f.size_bytes or 0
        if total + sz > lifecycle.MAX_BYTES:
            raise RuntimeError("Storage limit reached (1 GB). Remove some files before adding more.")
        await ctx.store.create(FILES_COLLECTION, {
            "file_id": f.file_id, "name": f.name, "mime_type": f.mime_type,
            "size_bytes": sz, "account_email": email,
            "status": lifecycle.PENDING, "document_id": None, "last_access_at": time.time(),
        })
        count += 1
        total += sz
        added += 1
    return added


async def impl_open_folder(ctx, folder_id: str) -> tuple[str, list[dict]]:
    """List the children of a granted folder (files + subfolders). The folder
    must have been picked for the active account (drive.file grants access to a
    picked folder's contents)."""
    acc = await _active_account(ctx)
    recs = await _all_picked_files(ctx, _account_email(acc))
    folder = next((r for r in recs if r.get("file_id") == folder_id and r.get("mime_type") == GOOGLE_FOLDER_MIME), None)
    if folder is None:
        raise RuntimeError(f"Folder {folder_id!r} isn't a picked folder for the active account — pick it via the Picker first.")
    resp = await drive_list_folder(ctx, acc, folder_id)
    resp.raise_for_status()
    children = resp.json().get("files", [])
    out = [{
        "file_id": c["id"], "name": c.get("name"),
        "mime_type": c.get("mimeType"), "size_bytes": int(c.get("size", 0) or 0),
        "is_folder": c.get("mimeType") == GOOGLE_FOLDER_MIME,
    } for c in children]
    return folder.get("name") or "Folder", out


# ─── @chat.function wrappers ──────────────────────────────────────────── #


@chat.function(
    "connect_google_docs", action_type="read", data_model=OAuthConnectResult,
    description="Start Google Drive OAuth — returns an authorisation URL to open in the browser. Connecting again adds another Google account (each keeps its own pool of picked files).",
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
    "open_file_picker", action_type="read", data_model=PickerLinkResult,
    description="Get a link to the Google Picker where the user selects which Drive files/folders Google Drive may access (for a specific connected account, default the active one). Picked items register automatically; call list_files afterwards.",
)
async def fn_open_file_picker(ctx, params: PickFilesParams) -> ActionResult:
    try:
        url = await impl_open_file_picker(ctx, account=params.account)
        return ActionResult.success(data=build_picker_link(url), summary="Picker link ready — open it, pick files, then check list_files.")
    except Exception as e:
        return ActionResult.error(str(e), retryable=False)


@chat.function(
    "register_picked_files", action_type="write", event="file.connected", data_model=EditResult,
    description="Manual fallback: register files from the JSON the Picker page showed, ONLY if it displayed a copy-paste box instead of confirming automatically (rare). Skips files already registered.",
)
async def fn_register_picked_files(ctx, params: RegisterPickedFilesParams) -> ActionResult:
    try:
        added = await impl_register_picked_files(ctx, params.files)
        if added:
            await lifecycle.kick_index(ctx)
        return ActionResult.success(
            data=build_edit_result("+".join(f.file_id for f in params.files) or "none"),
            summary=f"{added} new file(s) registered — indexing started." if added else "No new files (already registered).",
            refresh_panels=["doc_files"],
        )
    except Exception as e:
        return ActionResult.error(str(e), retryable=False)


@chat.function(
    "list_files", action_type="read", data_model=DocFileList,
    description="List the Drive files AND folders the user connected — name, type, size and indexing status (pending/indexing/ready/failed/cold). Folders can be opened with open_folder. Automatically picks up items just selected in the Picker. Does not return file content.",
)
async def fn_list_files(ctx, params: EmptyParams) -> ActionResult:
    try:
        added = await _claim_pending_picker_session(ctx)
        if added:
            await lifecycle.kick_index(ctx)   # index just-picked files in the background
        entries = await lifecycle.list_entries(ctx)
        return ActionResult.success(
            data=build_doc_file_list(entries),
            summary=f"{len(entries)} item(s)." if entries else "No files picked yet — call open_file_picker first.",
        )
    except Exception as e:
        return ActionResult.error(str(e), retryable=False)


@chat.function(
    "open_folder", action_type="read", data_model=FolderContents,
    description="List the contents of a connected Google Drive folder (files + subfolders) by its folder_id from list_files. Read a file inside it with read_file.",
)
async def fn_open_folder(ctx, params: FolderParams) -> ActionResult:
    try:
        name, children = await impl_open_folder(ctx, params.folder_id)
        return ActionResult.success(
            data=build_folder_contents(params.folder_id, name, children),
            summary=f"{len(children)} item(s) in {name}." if children else f"{name} is empty.",
        )
    except Exception as e:
        return ActionResult.error(str(e), retryable=False)


@chat.function(
    "disconnect_file", action_type="write", event="file.disconnected", data_model=EditResult,
    description="Remove a file from the connected list and delete its cached index (Postgres+Nextcloud). The file in Google Drive itself is untouched.",
)
async def fn_disconnect_file(ctx, params: FileIdParams) -> ActionResult:
    try:
        await lifecycle.forget_file(ctx, params.file_id)
        return ActionResult.success(data=build_edit_result(params.file_id, op="disconnect"), summary="File removed.", refresh_panels=["doc_files"])
    except Exception as e:
        return ActionResult.error(str(e), retryable=False)
