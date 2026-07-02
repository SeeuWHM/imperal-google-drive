"""Doc Reader · Google Sheets read/write — same drive.file token, Sheets API."""
from __future__ import annotations

import logging

from imperal_sdk.chat.action_result import ActionResult

from app import chat
from providers.google_api import sheets_get_values, sheets_update_values
from providers.helpers import _active_account, _find_picked_file
from schemas import ReadSpreadsheetParams, WriteSpreadsheetParams
from schemas_sdl import EditResult, SpreadsheetRange, build_edit_result, build_spreadsheet_range

log = logging.getLogger("doc_reader")


# ─── impl_* business logic ────────────────────────────────────────────── #


async def impl_read_spreadsheet_range(ctx, file_id: str, cell_range: str) -> list[list]:
    acc = await _active_account(ctx)
    await _find_picked_file(ctx, file_id)
    resp = await sheets_get_values(ctx, acc, file_id, cell_range)
    resp.raise_for_status()
    return resp.json().get("values", [])


async def impl_write_spreadsheet_range(ctx, file_id: str, cell_range: str, values: list[list]) -> None:
    acc = await _active_account(ctx)
    await _find_picked_file(ctx, file_id)
    resp = await sheets_update_values(ctx, acc, file_id, cell_range, values)
    resp.raise_for_status()


# ─── @chat.function wrappers ──────────────────────────────────────────── #


@chat.function(
    "read_spreadsheet_range", action_type="read",
    data_model=SpreadsheetRange,
    description="Read a range of cells from a Google Sheet (A1 notation, e.g. 'Sheet1!A1:D10', or a bare sheet name for the whole sheet). Returns the raw values as a 2D array.",
)
async def fn_read_spreadsheet_range(ctx, params: ReadSpreadsheetParams) -> ActionResult:
    try:
        values = await impl_read_spreadsheet_range(ctx, params.file_id, params.cell_range)
        return ActionResult.success(
            data=build_spreadsheet_range(params.file_id, params.cell_range, values),
            summary=f"{len(values)} row(s).",
        )
    except Exception as e:
        return ActionResult.error(str(e), retryable=False)


@chat.function(
    "write_spreadsheet_range", action_type="write", event="file.edited",
    data_model=EditResult,
    description="Write a 2D array of values into a Google Sheet range (A1 notation) — changes the real spreadsheet immediately, overwriting only the given range.",
)
async def fn_write_spreadsheet_range(ctx, params: WriteSpreadsheetParams) -> ActionResult:
    try:
        await impl_write_spreadsheet_range(ctx, params.file_id, params.cell_range, params.values)
        return ActionResult.success(data=build_edit_result(params.file_id), summary="Range updated.", refresh_panels=["doc_files"])
    except Exception as e:
        return ActionResult.error(str(e), retryable=False)
