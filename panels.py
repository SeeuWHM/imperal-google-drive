"""Doc Reader · Files panel (right slot) — list, search, remove.

Scope, deliberately: a list of connected files + search + a remove action.
No preview, no in-panel editing, no real folder hierarchy — those weren't
asked for. `grouped_by` on ui.List exists but its exact contract (which
ListItem field it groups by) isn't confirmed from the SDK source alone —
left unset rather than guessing; the file extension is still visible via
the badge/subtitle for now.
"""
from __future__ import annotations

import logging

from imperal_sdk import ui

from app import ext
from providers.helpers import _active_account, _all_accounts, reconcile_picked_files

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


@ext.panel("doc_files", slot="right", title="Doc Reader", icon="FileText")
async def build_files_panel(ctx, **kwargs) -> ui.UINode:
    accounts = await _all_accounts(ctx)
    if not accounts:
        return ui.Stack([
            ui.Header(text="Doc Reader", level=3),
            ui.Empty(message="Not connected to Google Drive", icon="FileText"),
            ui.Button("Connect Google Docs", icon="Plus", variant="primary",
                      on_click=ui.Call("connect_google_docs")),
        ], gap=2)

    acc = await _active_account(ctx)
    files = await reconcile_picked_files(ctx, acc)

    if not files:
        return ui.Stack([
            ui.Header(text="Doc Reader", level=3),
            ui.Empty(message="No files picked yet", icon="FileText"),
            ui.Text("Use the Google Picker to choose which files Doc Reader can access.", variant="caption"),
        ], gap=2)

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

    return ui.Stack([
        ui.Header(text="Doc Reader", level=3),
        ui.Text(f"{len(files)} file(s)", variant="caption"),
        ui.List(items=items, searchable=True),
    ], gap=2, className="pb-4")
