"""Doc Reader · Files panel (right slot).

Two blocks, so the user can always see WHICH Google account they're in and
switch between accounts, each with its OWN pool of picked files:
  1. Accounts — connected Google accounts, ✓ active one, click to switch,
     per-account file count, "Add Google account" (login) button.
  2. Files — the ACTIVE account's picked files + a "Pick files" button that
     opens the Google Picker for that account (on request, like login).

Rendering also claims any pending Picker session (via impl_list_connected_files),
so files just picked in the popup show up on the next render, no manual step.
"""
from __future__ import annotations

import logging

from imperal_sdk import ui

from app import ext
from handlers_accounts import impl_list_accounts
from handlers_connect import impl_list_connected_files, impl_open_file_picker
from providers.helpers import _account_email, _active_account, _all_accounts

log = logging.getLogger("doc_reader")


def _extension_of(name: str) -> str:
    return name.rsplit(".", 1)[-1].upper() if "." in name else "FILE"


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
        subtitle = f"{count} file(s)"
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


def _file_items(files: list) -> list:
    items = []
    for f in files:
        name = f.get("name", "?")
        ext_label = _extension_of(name)
        size_label = _human_size(f.get("size_bytes"))
        items.append(ui.ListItem(
            id=f["file_id"], title=name,
            subtitle=" · ".join(p for p in (ext_label, size_label) if p),
            badge=ui.Badge(ext_label, color="blue"),
            actions=[{"label": "Remove", "icon": "Trash2",
                      "on_click": ui.Call("disconnect_file", file_id=f["file_id"])}],
        ))
    return items


@ext.panel("doc_files", slot="right", title="Doc Reader", icon="FileText")
async def build_files_panel(ctx, **kwargs) -> ui.UINode:
    accounts = await _all_accounts(ctx)
    if not accounts:
        return ui.Stack([
            ui.Header(text="Doc Reader", level=3),
            ui.Empty(message="No Google account connected", icon="FileText"),
            ui.Button("Connect Google account", icon="Plus", variant="primary",
                      on_click=ui.Call("connect_google_docs")),
        ], gap=2)

    try:
        rows = await impl_list_accounts(ctx)              # [(acc, file_count)]
        active_email = _account_email(await _active_account(ctx))
        files = await impl_list_connected_files(ctx)      # active account's pool (also claims picker)
    except Exception as exc:
        log.error(f"doc_files panel error: {exc}")
        return ui.Stack([
            ui.Header(text="Doc Reader", level=3),
            ui.Alert(message=f"Error loading panel: {exc}", type="error"),
        ], gap=2)

    files_block = (
        ui.List(items=_file_items(files), searchable=True) if files
        else ui.Empty(message="No files picked for this account yet", icon="FileText")
    )

    # Stage a fresh picker token and open the picker page in a new tab on click.
    # ui.Open reliably opens a URL; ui.Call returns the URL but the platform does
    # not auto-open it. Falls back to a warning if a token can't be staged.
    try:
        picker_url = await impl_open_file_picker(ctx, account=active_email)
        pick_btn = ui.Button("Pick files from Drive", icon="Plus", variant="primary",
                             on_click=ui.Open(picker_url))
    except Exception as exc:
        pick_btn = ui.Alert(message=f"Picker not ready: {exc}", type="warning")

    return ui.Stack([
        ui.Header(text="Doc Reader", level=3),
        ui.Text("Accounts", variant="caption"),
        ui.List(items=_account_items(rows, active_email)),
        ui.Button("Add Google account", icon="Plus", variant="outline",
                  on_click=ui.Call("connect_google_docs")),
        ui.Divider(),
        ui.Text(f"Files — {active_email}", variant="caption"),
        pick_btn,
        files_block,
    ], gap=2, className="pb-4")
