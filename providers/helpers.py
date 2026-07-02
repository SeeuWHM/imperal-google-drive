"""Shared constants and account helpers for the Google Docs/Drive provider."""
from __future__ import annotations

ACCOUNTS_COLLECTION = "docreader_accounts"
FILES_COLLECTION = "docreader_picked_files"

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
DRIVE_API = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
DOCS_API = "https://docs.googleapis.com/v1"
SHEETS_API = "https://sheets.googleapis.com/v4"

GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
GOOGLE_SHEET_MIME = "application/vnd.google-apps.spreadsheet"
GOOGLE_SLIDE_MIME = "application/vnd.google-apps.presentation"

# Public route — doc-reader (whm-ai-worker) and doc-extractor-service
# (api-server) are different machines; must go through the nginx-proxied
# public path, same pattern as web-tools' WEB_TOOLS_API_URL.
DOC_EXTRACTOR_URL = "https://api.webhostmost.com/doc-extractor"

# Picker page — hosted BY doc-extractor-service itself (app/picker.py on
# api-server), not GitHub Pages. Does NOT run its own Google login — a
# drive.file grant obtained via the page's own separate client-side OAuth
# was confirmed (empirically, not assumed) invisible to this extension's
# server-side refresh-token grant, even same client_id/user/scope. Instead
# we stage a fresh access token (refreshed from OUR stored refresh_token)
# via /v1/picker/stage-token, HMAC-signed + 30s TTL; the page fetches it
# once and hands it straight to Picker's setOAuthToken(). Picked files
# themselves go through the separate, lower-sensitivity /v1/picker/stage
# (Redis, longer TTL) and get claimed via /v1/picker/claim/{token} from a
# REAL, correctly-scoped ctx — no @ext.webhook involved at all, because
# ctx.as_user() requires system context, which a webhook handler does not
# have (see extensions/doc-reader.md).
PICKER_PAGE_URL = f"{DOC_EXTRACTOR_URL}/v1/picker"
PICKER_CLAIM_URL = f"{DOC_EXTRACTOR_URL}/v1/picker/claim"
PICKER_STAGE_TOKEN_URL = f"{DOC_EXTRACTOR_URL}/v1/picker/stage-token"

# Bind at MODULE LOAD (not inside reconcile) — see below. When an extension
# runs in the shared worker with others that also have a top-level `providers`
# package (e.g. mail-client, which has NO google_api), a call-time
# `from .google_api import ...` can resolve `providers` to the wrong package
# and raise "No module named 'providers.google_api'". Importing here binds to
# OUR google_api once, at load, immune to later sys.modules shadowing.
# Constants above are defined first, so google_api's `from .helpers import ...`
# resolves the circular import cleanly.
from .google_api import drive_list_files  # noqa: E402


async def _all_accounts(ctx) -> list[dict]:
    docs = await ctx.store.query(ACCOUNTS_COLLECTION)
    out = []
    for d in docs:
        item = dict(d.data)
        item["doc_id"] = d.id
        out.append(item)
    return out


async def _active_account(ctx) -> dict:
    accounts = await _all_accounts(ctx)
    if not accounts:
        raise RuntimeError("No Google account connected. Call connect_google_docs first.")
    active = next((a for a in accounts if a.get("is_active")), accounts[0])
    return active


def _account_email(acc: dict) -> str:
    """Stable per-account key used to scope picked files. Prefer the real
    Google address the platform stored on the OAuth account record; fall back
    to the store doc id so scoping still works if `email` is ever absent."""
    return acc.get("email") or acc.get("doc_id") or ""


async def _account_by_email(ctx, account: str) -> dict:
    """Resolve a connected account by its email (or store doc id)."""
    accounts = await _all_accounts(ctx)
    match = next((a for a in accounts if a.get("email") == account or a.get("doc_id") == account), None)
    if not match:
        available = [a.get("email") or a.get("doc_id") for a in accounts]
        raise RuntimeError(f"Account {account!r} not found. Connected: {available}")
    return match


async def _all_picked_files(ctx, account_email: str | None = None) -> list[dict]:
    """Picked-file records. With ``account_email`` — only that account's pool
    (each connected Google account keeps a separate pool of picked files);
    without it — every account's files (used for totals)."""
    docs = await ctx.store.query(FILES_COLLECTION)
    out = []
    for d in docs:
        item = dict(d.data)
        item["doc_id"] = d.id
        if account_email is None or item.get("account_email") == account_email:
            out.append(item)
    return out


async def _active_picked_files(ctx) -> list[dict]:
    acc = await _active_account(ctx)
    return await _all_picked_files(ctx, _account_email(acc))


async def _find_picked_file(ctx, file_id: str) -> dict:
    """Look up a picked file in the ACTIVE account's pool. A drive.file grant
    is scoped to the account that picked the file, so reads/writes must use
    that account's token — the active-account pool guarantees that (handlers
    read with ``_active_account``)."""
    acc = await _active_account(ctx)
    active_email = _account_email(acc)
    files = await _all_picked_files(ctx, active_email)
    match = next((f for f in files if f.get("file_id") == file_id), None)
    if match:
        return match
    # Picked, but under a different account? Point the caller at the fix.
    other = next((f for f in await _all_picked_files(ctx) if f.get("file_id") == file_id), None)
    if other:
        raise RuntimeError(
            f"File {file_id!r} belongs to account {other.get('account_email')!r}, but the active "
            f"account is {active_email!r}. Switch to that account (switch_account) first."
        )
    raise RuntimeError(
        f"File {file_id!r} was not picked through the Google Picker for the active account "
        "(drive.file only grants access to explicitly picked files). Ask the user to pick it first."
    )


async def reconcile_picked_files(ctx, acc: dict) -> list[dict]:
    """Drop local records for files Google no longer grants us access to
    (deleted, trashed, or unshared from the app) — then return the pruned,
    fresh list. `drive.file`'s files.list is the ground truth: a file that
    disappeared from Google simply won't be in this response anymore, no
    per-file existence probing needed.

    Best-effort: if the live check itself fails (network, expired token
    refresh, etc.) the local list is returned unpruned rather than blanked —
    a stale list beats a false "everything is gone".
    """
    local_files = await _all_picked_files(ctx, _account_email(acc))
    if not local_files:
        return local_files

    try:
        resp = await drive_list_files(ctx, acc)
        resp.raise_for_status()
        live_ids = {f["id"] for f in resp.json().get("files", [])}
    except Exception:
        return local_files

    kept = []
    for f in local_files:
        if f.get("file_id") in live_ids:
            kept.append(f)
        else:
            await ctx.store.delete(FILES_COLLECTION, f["doc_id"])
    return kept


async def _remove_picked_file(ctx, file_id: str) -> None:
    files = await _all_picked_files(ctx)
    match = next((f for f in files if f.get("file_id") == file_id), None)
    if not match:
        raise RuntimeError(f"File {file_id!r} is not in the connected files list.")
    await ctx.store.delete(FILES_COLLECTION, match["doc_id"])
