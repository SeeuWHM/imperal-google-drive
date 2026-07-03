"""Thin wrappers over Drive/Docs/Sheets REST calls — one primitive per API
operation, no orchestration baked in here (the handler layer decides what to
call and when)."""
from __future__ import annotations

from .helpers import DOCS_API, DRIVE_API, DRIVE_UPLOAD_API, SHEETS_API
from .token_refresh import _refresh_token_if_needed


def _auth_headers(acc: dict) -> dict:
    return {"Authorization": f"Bearer {acc['access_token']}"}


# ── Drive ──────────────────────────────────────────────────────────────────


async def drive_get_metadata(ctx, acc: dict, file_id: str):
    acc = await _refresh_token_if_needed(ctx, acc)
    return await ctx.http.get(
        f"{DRIVE_API}/files/{file_id}",
        params={"fields": "id,name,mimeType,modifiedTime,size,md5Checksum,headRevisionId"},
        headers=_auth_headers(acc),
    )


async def drive_list_files(ctx, acc: dict):
    """List every file this drive.file grant currently has access to.

    This is the ground truth for reconciliation: a file the user deleted or
    unshared from the app simply stops appearing here — no per-file
    existence check needed, one call reflects current access for all of them.
    """
    acc = await _refresh_token_if_needed(ctx, acc)
    return await ctx.http.get(
        f"{DRIVE_API}/files",
        params={"fields": "files(id,name,mimeType,modifiedTime,size)", "pageSize": 200, "q": "trashed=false"},
        headers=_auth_headers(acc),
    )


async def drive_list_folder(ctx, acc: dict, folder_id: str):
    """List the direct children of a folder the user granted via the Picker.
    Includes shared-drive params (harmless for My Drive, needed if the folder
    lives in a shared drive)."""
    acc = await _refresh_token_if_needed(ctx, acc)
    return await ctx.http.get(
        f"{DRIVE_API}/files",
        params={
            "q": f"'{folder_id}' in parents and trashed=false",
            "fields": "files(id,name,mimeType,modifiedTime,size)",
            "pageSize": 200,
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
            "corpora": "allDrives",
        },
        headers=_auth_headers(acc),
    )


async def drive_folder_probe(ctx, acc: dict, folder_id: str) -> dict:
    """DIAGNOSTIC: probe what drive.file can actually see for a picked folder,
    trying several strategies. Returns a plain dict of raw outcomes so we can
    tell (empirically) whether drive.file exposes folder contents or not."""
    acc = await _refresh_token_if_needed(ctx, acc)
    h = _auth_headers(acc)
    out: dict = {}

    async def _try(label, params):
        try:
            r = await ctx.http.get(f"{DRIVE_API}/files", params=params, headers=h)
            body = r.json() if r.status_code < 400 else {}
            out[label] = {"status": r.status_code, "count": len(body.get("files", [])),
                          "sample": [f.get("name") for f in body.get("files", [])[:5]]}
        except Exception as e:  # noqa: BLE001
            out[label] = {"error": type(e).__name__ + ": " + str(e)[:120]}

    await _try("parents_trashedfalse", {"q": f"'{folder_id}' in parents and trashed=false", "fields": "files(id,name,mimeType)", "pageSize": 50})
    await _try("parents_no_trashed", {"q": f"'{folder_id}' in parents", "fields": "files(id,name,mimeType)", "pageSize": 50})
    await _try("parents_alldrives", {"q": f"'{folder_id}' in parents and trashed=false", "fields": "files(id,name,mimeType)", "pageSize": 50, "supportsAllDrives": "true", "includeItemsFromAllDrives": "true", "corpora": "allDrives"})
    # capabilities on the folder itself
    try:
        rm = await ctx.http.get(f"{DRIVE_API}/files/{folder_id}",
                                params={"fields": "id,name,mimeType,capabilities,driveId,ownedByMe,shared"},
                                headers=h, )
        out["folder_meta"] = rm.json() if rm.status_code < 400 else {"status": rm.status_code}
    except Exception as e:  # noqa: BLE001
        out["folder_meta"] = {"error": type(e).__name__ + ": " + str(e)[:120]}
    return out


async def drive_about(ctx, acc: dict):
    """The signed-in user's identity for this account. drive.file grants this
    (about.get needs no extra scope), so we can show the real email even
    though Google doesn't hand it to the platform for a drive.file-only grant."""
    acc = await _refresh_token_if_needed(ctx, acc)
    return await ctx.http.get(
        f"{DRIVE_API}/about",
        params={"fields": "user(displayName,emailAddress)"},
        headers=_auth_headers(acc),
    )


async def drive_download_media(ctx, acc: dict, file_id: str):
    acc = await _refresh_token_if_needed(ctx, acc)
    return await ctx.http.get(
        f"{DRIVE_API}/files/{file_id}",
        params={"alt": "media"},
        headers=_auth_headers(acc),
    )


async def drive_upload_media(ctx, acc: dict, file_id: str, content: bytes, mime_type: str = "text/plain"):
    acc = await _refresh_token_if_needed(ctx, acc)
    return await ctx.http.patch(
        f"{DRIVE_UPLOAD_API}/files/{file_id}",
        params={"uploadType": "media"},
        headers={**_auth_headers(acc), "Content-Type": mime_type},
        content=content,
    )


async def drive_export_text(ctx, acc: dict, file_id: str, mime_type: str = "text/plain"):
    """Drive's own format conversion — used for Google Slides, where walking
    the native presentations.get structure (slide -> pageElement -> shape ->
    text -> textRun) would just reimplement what this one call already does."""
    acc = await _refresh_token_if_needed(ctx, acc)
    return await ctx.http.get(
        f"{DRIVE_API}/files/{file_id}/export",
        params={"mimeType": mime_type},
        headers=_auth_headers(acc),
    )


# ── Docs ───────────────────────────────────────────────────────────────────


async def docs_get(ctx, acc: dict, document_id: str):
    acc = await _refresh_token_if_needed(ctx, acc)
    return await ctx.http.get(f"{DOCS_API}/documents/{document_id}", headers=_auth_headers(acc))


async def docs_batch_update(ctx, acc: dict, document_id: str, requests: list[dict]):
    acc = await _refresh_token_if_needed(ctx, acc)
    return await ctx.http.post(
        f"{DOCS_API}/documents/{document_id}:batchUpdate",
        headers=_auth_headers(acc),
        json={"requests": requests},
    )


def walk_document_text(doc_json: dict) -> str:
    """Flatten a Docs API document JSON body into plain text."""
    parts = []
    for elem in doc_json.get("body", {}).get("content", []):
        para = elem.get("paragraph")
        if not para:
            continue
        for pe in para.get("elements", []):
            parts.append(pe.get("textRun", {}).get("content", ""))
    return "".join(parts)


def document_end_index(doc_json: dict) -> int:
    """Index just past the last body element — needed for overwrite/append."""
    content = doc_json.get("body", {}).get("content", [])
    if not content:
        return 1
    return content[-1].get("endIndex", 1)


# ── Sheets ───────────────────────────────────────────────────────────────────


async def sheets_get_metadata(ctx, acc: dict, spreadsheet_id: str):
    """Sheet names + dimensions — needed before a caller can address a range
    by name (there's no way to guess "Sheet1" is right otherwise)."""
    acc = await _refresh_token_if_needed(ctx, acc)
    return await ctx.http.get(
        f"{SHEETS_API}/spreadsheets/{spreadsheet_id}",
        params={"fields": "properties.title,sheets.properties"},
        headers=_auth_headers(acc),
    )


async def sheets_get_values(ctx, acc: dict, spreadsheet_id: str, cell_range: str):
    acc = await _refresh_token_if_needed(ctx, acc)
    return await ctx.http.get(
        f"{SHEETS_API}/spreadsheets/{spreadsheet_id}/values/{cell_range}",
        headers=_auth_headers(acc),
    )


async def sheets_update_values(ctx, acc: dict, spreadsheet_id: str, cell_range: str, values: list[list]):
    acc = await _refresh_token_if_needed(ctx, acc)
    return await ctx.http.put(
        f"{SHEETS_API}/spreadsheets/{spreadsheet_id}/values/{cell_range}",
        params={"valueInputOption": "USER_ENTERED"},
        headers=_auth_headers(acc),
        json={"values": values},
    )
