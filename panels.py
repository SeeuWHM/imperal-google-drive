"""Google Drive · Files panel (right slot).

Two blocks:
  1. Accounts — connected Google accounts, ✓ active one, click to switch,
     per-account item count, "Add Google account".
  2. Files & folders — the ACTIVE account's picked items with their indexing
     status; folders open their contents (open_folder); files can be removed.

Rendering also claims any pending Picker session and kicks background indexing,
so just-picked files show up (as "pending" → "ready") without a manual step.
"""
from __future__ import annotations

import logging

from imperal_sdk import ui

from app import ext
from handlers_accounts import impl_list_accounts
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
_STATUS_COLOR = {"ready": "green", "failed": "red"}


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


def _account_items(rows: list, active_email: str) -> list:
    items = []
    for acc, count in rows:
        email = acc.get("email") or acc.get("doc_id") or "?"
        is_active = email == active_email
        subtitle = f"{count} item(s)"
        if is_active:
            subtitle = f"✓ Active — {subtitle}"
        items.append(ui.ListItem(
            id=email, title=email, subtitle=subtitle,
            avatar=ui.Avatar(fallback=email[0].upper(), size="sm"),
            badge=ui.Badge("✓", color="green") if is_active else None,
            on_click=ui.Call("switch_account", account=email),
            actions=[{"label": "Disconnect", "icon": "Trash2",
                      "on_click": ui.Call("disconnect_account", account=email)}],
        ))
    return items


def _entry_items(entries: list) -> list:
    items = []
    for f in entries:
        name = f.get("name", "?")
        if f.get("is_folder"):
            items.append(ui.ListItem(
                id=f["file_id"], title=name, subtitle="Folder — open to see contents",
                badge=ui.Badge("FOLDER", color="blue"),
                on_click=ui.Call("open_folder", folder_id=f["file_id"]),
                actions=[{"label": "Remove", "icon": "Trash2",
                          "on_click": ui.Call("disconnect_file", file_id=f["file_id"])}],
            ))
            continue
        ext_label = _type_label(name, f.get("mime_type", ""))
        size_label = _human_size(f.get("size_bytes"))
        status = f.get("status") or "pending"
        items.append(ui.ListItem(
            id=f["file_id"], title=name,
            subtitle=" · ".join(p for p in (ext_label, size_label, status) if p),
            badge=ui.Badge(status, color=_STATUS_COLOR.get(status, "blue")),
            actions=[{"label": "Remove", "icon": "Trash2",
                      "on_click": ui.Call("disconnect_file", file_id=f["file_id"])}],
        ))
    return items


@ext.panel("doc_files", slot="right", title="Google Drive", icon="FileText")
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
        rows = await impl_list_accounts(ctx)
        active_email = _account_email(await _active_account(ctx))
        entries = await lifecycle.list_entries(ctx)
    except Exception as exc:
        log.error(f"doc_files panel error: {exc}")
        return ui.Stack([
            ui.Header(text="Google Drive", level=3),
            ui.Alert(message=f"Error loading panel: {exc}", type="error"),
        ], gap=2)

    files_block = (
        ui.List(items=_entry_items(entries), searchable=True) if entries
        else ui.Empty(message="No files picked for this account yet", icon="FileText")
    )

    try:
        picker_url = await impl_open_file_picker(ctx, account=active_email)
        pick_btn = ui.Button("Pick files from Drive", icon="Plus", variant="primary",
                             on_click=ui.Open(picker_url))
    except Exception as exc:
        pick_btn = ui.Alert(message=f"Picker not ready: {exc}", type="warning")

    return ui.Stack([
        ui.Header(text="Google Drive", level=3),
        ui.Text("Accounts", variant="caption"),
        ui.List(items=_account_items(rows, active_email)),
        ui.Button("Add Google account", icon="Plus", variant="outline",
                  on_click=ui.Call("connect_google_docs")),
        ui.Divider(),
        ui.Text(f"Files — {active_email}", variant="caption"),
        pick_btn,
        files_block,
    ], gap=2, className="pb-4")
