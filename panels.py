"""Google Drive · Files panel (right slot) — the file manager.

Layout, top to bottom:
  1. Storage bar — doc count / byte usage against the account cap, a quiet
     Progress strip so the limit is never a surprise.
  2. Toolbar — Pick files (Picker) + Re-index pending/failed (bulk retry).
  3. Files & folders — searchable, MULTI-SELECT list (checkboxes act like a
     ctrl-click selection) with a bulk toolbar: Remove / Retry indexing.
     Per-row hover actions still work for a single file. Folders open their
     contents (open_folder) same as before.

Accounts have moved to the LEFT sidebar (panels_overview.py) — this panel
stays focused on one job: the active account's files. Rendering also claims
any pending Picker session and kicks background indexing, so just-picked
files show up (as "pending" → "ready") without a manual step.
"""
from __future__ import annotations

import logging

from imperal_sdk import ui

from app import ext
from handlers_connect import _claim_pending_picker_session, impl_open_file_picker
from handlers_index import kick_index
from providers import lifecycle
from providers.helpers import _account_email, _active_account, _all_accounts

log = logging.getLogger("doc_reader")

_MIME_LABELS = {
    "application/vnd.google-apps.document": "DOC",
    "application/vnd.google-apps.spreadsheet": "SHEET",
    "application/vnd.google-apps.presentation": "SLIDES",
    "application/vnd.google-apps.folder": "FOLDER",
    "application/pdf": "PDF",
    "text/plain": "TXT",
    "text/csv": "CSV",
    "text/html": "HTML",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "DOCX",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "XLSX",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "PPTX",
}

# Keep to colours the existing panel used (green/blue/red) — safe across themes.
_STATUS_COLOR = {"ready": "green", "failed": "red", "indexing": "blue", "pending": "gray", "cold": "gray"}


def _type_label(name: str, mime_type: str) -> str:
    if mime_type in _MIME_LABELS:
        return _MIME_LABELS[mime_type]
    if "." in name:
        return name.rsplit(".", 1)[-1].upper()
    return "FILE"


def _human_size(size_bytes) -> str:
    try:
        n = float(size_bytes or 0)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _entry_items(entries: list) -> list:
    items = []
    for f in entries:
        name = f.get("name", "?")
        if f.get("is_folder"):
            items.append(ui.ListItem(
                id=f["file_id"], title=name, subtitle="Folder — open to see contents",
                icon="Folder",
                badge=ui.Badge("FOLDER", color="blue"),
                on_click=ui.Call("open_folder", folder_id=f["file_id"]),
                actions=[{"label": "Remove", "icon": "Trash2",
                          "on_click": ui.Call("disconnect_files", file_ids=[f["file_id"]]),
                          "confirm": f"Remove \u201c{name}\u201d from Drive Connector?"}],
            ))
            continue
        ext_label = _type_label(name, f.get("mime_type", ""))
        size_label = _human_size(f.get("size_bytes"))
        status = f.get("status") or "pending"
        subtitle_parts = [p for p in (ext_label, size_label) if p]
        if status == "failed" and f.get("error"):
            subtitle_parts.append(f"error: {f['error']}")
        items.append(ui.ListItem(
            id=f["file_id"], title=name,
            subtitle=" · ".join(subtitle_parts),
            icon="FileText",
            badge=ui.Badge(status, color=_STATUS_COLOR.get(status, "blue")),
            actions=[{"label": "Remove", "icon": "Trash2",
                      "on_click": ui.Call("disconnect_files", file_ids=[f["file_id"]]),
                      "confirm": f"Remove \u201c{name}\u201d from Drive Connector?"}],
        ))
    return items


def _storage_bar(count: int, total_bytes: int) -> ui.UINode:
    pct = min(100, round(100 * count / lifecycle.MAX_DOCS)) if lifecycle.MAX_DOCS else 0
    gb_used = total_bytes / (1024 ** 3)
    gb_cap = lifecycle.MAX_BYTES / (1024 ** 3)
    color = "red" if pct >= 90 else ("yellow" if pct >= 70 else "green")
    return ui.Stack([
        ui.Progress(value=pct, label=f"{count}/{lifecycle.MAX_DOCS} files · {gb_used:.2f}/{gb_cap:.0f} GB",
                    color=color),
    ], gap=1)


@ext.panel("doc_files", slot="right", title="Google Drive", icon="FileText",
           refresh="on_event:account.switched,file.connected,file.disconnected,file.bulk_action,file.edited")
async def build_files_panel(ctx, **kwargs) -> ui.UINode:
    accounts = await _all_accounts(ctx)
    if not accounts:
        return ui.Stack([
            ui.Header(text="Google Drive", level=3),
            ui.Empty(message="No Google account connected", icon="FileText"),
            ui.Button("Connect Google account", icon="Plus", variant="primary",
                      on_click=ui.Call("connect_google_docs")),
        ], gap=2)

    try:
        added = await _claim_pending_picker_session(ctx)
        if added:
            await kick_index(ctx)
        active_acc = await _active_account(ctx)
        active_email = _account_email(active_acc)
        entries = await lifecycle.list_entries(ctx)
        doc_count, total_bytes = await lifecycle.quota_state(ctx, active_email)
    except Exception as exc:
        log.error(f"doc_files panel error: {exc}")
        return ui.Stack([
            ui.Header(text="Google Drive", level=3),
            ui.Alert(message=f"Error loading panel: {exc}", type="error"),
        ], gap=2)

    failed_or_pending = [e["file_id"] for e in entries
                         if not e.get("is_folder") and e.get("status") in ("failed", "pending")]

    bulk_actions = [
        {"label": "Remove", "icon": "Trash2",
         "action": ui.Call("file_bulk_action", action="remove"),
         "confirm": "Remove all selected files from Drive Connector?"},
        {"label": "Retry indexing", "icon": "RefreshCw",
         "action": ui.Call("file_bulk_action", action="retry_index")},
    ]

    files_block = (
        ui.List(
            items=_entry_items(entries),
            searchable=True,
            selectable=True,
            bulk_actions=bulk_actions,
        ) if entries
        else ui.Empty(message="No files picked for this account yet", icon="FileText")
    )

    try:
        picker_url = await impl_open_file_picker(ctx, account=active_email)
        pick_btn = ui.Button("Pick files", icon="Plus", variant="primary",
                             on_click=ui.Open(picker_url))
    except Exception as exc:
        pick_btn = ui.Alert(message=f"Picker not ready: {exc}", type="warning")

    folder_ids = [e["file_id"] for e in entries if e.get("is_folder")]

    toolbar = [pick_btn]
    if failed_or_pending:
        toolbar.append(ui.Button(
            f"Re-index {len(failed_or_pending)} pending/failed", icon="RefreshCw", variant="outline",
            on_click=ui.Call("file_bulk_action", action="retry_index", message_ids=failed_or_pending),
        ))
    if len(folder_ids) > 1:
        # Folders picked via the Picker aren't actually readable under the
        # drive.file scope (contents are empty — see README "Known limitations"),
        # so a pile of them is pure clutter, never useful data. One-click escape
        # hatch for exactly the "too many Debug folders" complaint, instead of
        # forcing a manual multi-select of every duplicate.
        toolbar.append(ui.Button(
            f"Remove all {len(folder_ids)} folders", icon="FolderMinus", variant="outline",
            on_click=ui.Call("disconnect_files", file_ids=folder_ids),
        ))

    return ui.Stack([
        ui.Header(text=f"Files — {active_email}", level=3),
        _storage_bar(doc_count, total_bytes),
        ui.Stack(toolbar, direction="h", gap=2),
        ui.Divider(),
        files_block,
    ], gap=2, className="pb-4")
