"""Google Drive · Overview panel (left slot) — accounts + at-a-glance stats.

Splits what used to be crammed into one right-hand panel into two focused
surfaces: this LEFT sidebar is the "control" surface (which account, how much
space is left, connect another account) and the RIGHT panel (panels.py) is
the "work" surface (the files themselves, multi-select, bulk actions).

Kept deliberately calm: a header, one Stats row, the account list (switch /
disconnect inline), and a single "Connect another account" affordance. No
duplication of the file list itself.
"""
from __future__ import annotations

import logging

from imperal_sdk import ui

from app import ext
from handlers_accounts import impl_list_accounts
from providers import lifecycle
from providers.helpers import _account_email, _all_accounts

log = logging.getLogger("doc_reader")


def _account_items(rows: list[tuple[dict, int]]) -> list[ui.UINode]:
    items = []
    for acc, file_count in rows:
        email = _account_email(acc)
        is_active = bool(acc.get("is_active"))
        items.append(ui.ListItem(
            id=email, title=email,
            subtitle=f"{file_count} file(s)",
            icon="User",
            badge=ui.Badge("active", color="green") if is_active else None,
            on_click=None if is_active else ui.Call("switch_account", account=email),
            actions=[{"label": "Disconnect", "icon": "LogOut",
                      "on_click": ui.Call("disconnect_account", account=email),
                      "confirm": f"Disconnect {email}? Its picked files will be forgotten too."}],
        ))
    return items


@ext.panel("doc_overview", slot="left", title="Google Drive", icon="HardDrive",
           refresh="on_event:account.switched,account.disconnected,file.connected,file.disconnected,file.bulk_action")
async def build_overview_panel(ctx, **kwargs) -> ui.UINode:
    accounts = await _all_accounts(ctx)
    if not accounts:
        return ui.Stack([
            ui.Header(text="Google Drive", level=3),
            ui.Empty(message="Connect a Google account to get started", icon="HardDrive"),
            ui.Button("Connect Google account", icon="Plus", variant="primary",
                      on_click=ui.Call("connect_google_docs")),
        ], gap=2)

    try:
        rows = await impl_list_accounts(ctx)
        total_files = sum(fc for _, fc in rows)
        total_bytes = 0
        for acc, _ in rows:
            _, acc_bytes = await lifecycle.quota_state(ctx, _account_email(acc))
            total_bytes += acc_bytes
    except Exception as exc:  # noqa: BLE001
        log.error(f"doc_overview panel error: {exc}")
        return ui.Stack([
            ui.Header(text="Google Drive", level=3),
            ui.Alert(message=f"Error loading panel: {exc}", type="error"),
        ], gap=2)

    gb_used = total_bytes / (1024 ** 3)

    return ui.Stack([
        ui.Header(text="Google Drive", level=3, subtitle="Connected accounts & storage"),
        ui.Stats([
            ui.Stat(label="Accounts", value=len(rows), icon="Users", color="blue"),
            ui.Stat(label="Files", value=total_files, icon="FileText", color="purple"),
            ui.Stat(label="Storage", value=f"{gb_used:.2f} GB", icon="HardDrive", color="green"),
        ], columns=1),
        ui.Divider(label="Accounts"),
        ui.List(items=_account_items(rows)) if rows
        else ui.Empty(message="No accounts yet", icon="User"),
        ui.Button("Connect another account", icon="Plus", variant="outline",
                  on_click=ui.Call("connect_google_docs")),
    ], gap=3, className="pb-4")
