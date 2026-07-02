"""Doc Reader · Account management — list / switch / disconnect connected
Google accounts. Each account keeps its own separate pool of picked files."""
from __future__ import annotations

import logging

from imperal_sdk.chat.action_result import ActionResult

from app import chat
from providers.helpers import (
    ACCOUNTS_COLLECTION,
    FILES_COLLECTION,
    _account_by_email,
    _account_email,
    _all_accounts,
    _all_picked_files,
)
from schemas import AccountParam, EmptyParams
from schemas_sdl import (
    AccountDisconnected,
    AccountSwitched,
    AccountsList,
    build_account_disconnected,
    build_account_switched,
    build_accounts_list,
)

log = logging.getLogger("doc_reader")


# ─── impl_* business logic ────────────────────────────────────────────── #


async def impl_list_accounts(ctx) -> list[tuple[dict, int]]:
    """Each connected account paired with the size of its own picked-files pool."""
    accounts = await _all_accounts(ctx)
    out: list[tuple[dict, int]] = []
    for a in accounts:
        files = await _all_picked_files(ctx, _account_email(a))
        out.append((a, len(files)))
    return out


async def impl_switch_account(ctx, account: str) -> str:
    target = await _account_by_email(ctx, account)
    for a in await _all_accounts(ctx):
        new_active = a["doc_id"] == target["doc_id"]
        if a.get("is_active") != new_active:
            clean = {k: v for k, v in a.items() if k != "doc_id"}
            await ctx.store.update(ACCOUNTS_COLLECTION, a["doc_id"], {**clean, "is_active": new_active})
    return _account_email(target)


async def impl_disconnect_account(ctx, account: str) -> tuple[str, int]:
    target = await _account_by_email(ctx, account)
    email = _account_email(target)
    # Forget this account's picked-files pool, then the account record itself.
    for f in await _all_picked_files(ctx, email):
        await ctx.store.delete(FILES_COLLECTION, f["doc_id"])
    await ctx.store.delete(ACCOUNTS_COLLECTION, target["doc_id"])
    remaining = len(await _all_accounts(ctx))
    return email, remaining


# ─── @chat.function wrappers ──────────────────────────────────────────── #


@chat.function(
    "list_accounts", action_type="read", data_model=AccountsList,
    description="List the Google accounts connected to Doc Reader — each account's email, which one is active, and how many files are in its own pool. Use when the user asks which Google accounts are connected or which one is in use.",
)
async def fn_list_accounts(ctx, params: EmptyParams) -> ActionResult:
    try:
        rows = await impl_list_accounts(ctx)
        return ActionResult.success(
            data=build_accounts_list(rows),
            summary=f"{len(rows)} Google account(s) connected." if rows else "No Google accounts connected.",
        )
    except Exception as e:
        return ActionResult.error(str(e), retryable=False)


@chat.function(
    "switch_account", action_type="write", event="account.switched",
    data_model=AccountSwitched,
    description="Change the active Google account. Subsequent file listing, picking, reading and editing use this account's own pool of picked files until switched again.",
)
async def fn_switch_account(ctx, params: AccountParam) -> ActionResult:
    try:
        active = await impl_switch_account(ctx, params.account)
        return ActionResult.success(
            data=build_account_switched(active),
            summary=f"Switched to {active}.",
            refresh_panels=["doc_files"],
        )
    except Exception as e:
        return ActionResult.error(str(e), retryable=False)


@chat.function(
    "disconnect_account", action_type="destructive", event="account.disconnected",
    data_model=AccountDisconnected,
    description="Disconnect a Google account from Doc Reader and forget its pool of picked files. Nothing is deleted in Google Drive itself; the OAuth grant can be fully revoked by the user at myaccount.google.com/permissions.",
)
async def fn_disconnect_account(ctx, params: AccountParam) -> ActionResult:
    try:
        email, remaining = await impl_disconnect_account(ctx, params.account)
        return ActionResult.success(
            data=build_account_disconnected(email, remaining),
            summary=f"Disconnected {email}. {remaining} account(s) remaining.",
            refresh_panels=["doc_files"],
        )
    except Exception as e:
        return ActionResult.error(str(e), retryable=False)
