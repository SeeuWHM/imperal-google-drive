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

# Public route — doc-reader (whm-ai-worker) and doc-extractor-service
# (api-server) are different machines; must go through the nginx-proxied
# public path, same pattern as web-tools' WEB_TOOLS_API_URL.
DOC_EXTRACTOR_URL = "https://api.webhostmost.com/doc-extractor"


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


async def _all_picked_files(ctx) -> list[dict]:
    docs = await ctx.store.query(FILES_COLLECTION)
    out = []
    for d in docs:
        item = dict(d.data)
        item["doc_id"] = d.id
        out.append(item)
    return out


async def _find_picked_file(ctx, file_id: str) -> dict:
    files = await _all_picked_files(ctx)
    match = next((f for f in files if f.get("file_id") == file_id), None)
    if not match:
        raise RuntimeError(
            f"File {file_id!r} was not picked through connect_google_docs' Google Picker "
            "(drive.file scope only grants access to explicitly picked/shared files). "
            "Ask the user to pick it first."
        )
    return match


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
    from .google_api import drive_list_files  # local import: avoids a circular import with google_api's helpers usage

    local_files = await _all_picked_files(ctx)
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
