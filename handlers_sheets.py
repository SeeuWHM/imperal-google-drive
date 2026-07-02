"""Doc Reader · Google Sheets read/write — same drive.file token, Sheets API."""
from __future__ import annotations

import logging

from imperal_sdk.chat.action_result import ActionResult

from app import chat
from providers.google_api import sheets_get_metadata, sheets_get_values, sheets_update_values
from providers.helpers import _active_account, _find_picked_file
from providers.spreadsheet_math import compute_aggregate
from schemas import AggregateSpreadsheetParams, FileIdParams, ReadSpreadsheetParams, WriteSpreadsheetParams
from schemas_sdl import (
    AggregateResult,
    EditResult,
    SpreadsheetInfo,
    SpreadsheetRange,
    build_aggregate_result,
    build_edit_result,
    build_spreadsheet_info,
    build_spreadsheet_range,
)

log = logging.getLogger("doc_reader")


# ─── impl_* business logic ────────────────────────────────────────────── #


async def impl_get_spreadsheet_info(ctx, file_id: str) -> list[dict]:
    acc = await _active_account(ctx)
    await _find_picked_file(ctx, file_id)
    resp = await sheets_get_metadata(ctx, acc, file_id)
    resp.raise_for_status()
    body = resp.json()
    sheets = []
    for s in body.get("sheets", []):
        props = s.get("properties", {})
        grid = props.get("gridProperties", {})
        sheets.append({
            "name": props.get("title", "?"),
            "row_count": grid.get("rowCount", 0),
            "column_count": grid.get("columnCount", 0),
        })
    return sheets


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


async def impl_aggregate_spreadsheet_range(ctx, file_id: str, cell_range: str, operation: str) -> tuple[float, int]:
    values = await impl_read_spreadsheet_range(ctx, file_id, cell_range)
    return compute_aggregate(values, operation)


# ─── @chat.function wrappers ──────────────────────────────────────────── #


@chat.function(
    "get_spreadsheet_info", action_type="read",
    data_model=SpreadsheetInfo,
    description="List a Google Sheet's tab names and dimensions (rows/columns). Call this before read_spreadsheet_range/write_spreadsheet_range if you don't already know the sheet name — there's no way to guess it otherwise.",
)
async def fn_get_spreadsheet_info(ctx, params: FileIdParams) -> ActionResult:
    try:
        sheets = await impl_get_spreadsheet_info(ctx, params.file_id)
        return ActionResult.success(
            data=build_spreadsheet_info(params.file_id, sheets),
            summary=f"{len(sheets)} sheet(s): " + ", ".join(s["name"] for s in sheets) if sheets else "No sheets found.",
        )
    except Exception as e:
        return ActionResult.error(str(e), retryable=False)


@chat.function(
    "read_spreadsheet_range", action_type="read",
    data_model=SpreadsheetRange,
    description="Read a range of cells from a Google Sheet (A1 notation, e.g. 'Sheet1!A1:D10', or a bare sheet name for the whole sheet). Returns the raw values as a 2D array. Use get_spreadsheet_info first if you don't know the sheet name.",
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


@chat.function(
    "aggregate_spreadsheet_range", action_type="read",
    data_model=AggregateResult,
    description="Compute an exact sum/count/average/min/max over a Google Sheet range, calculated in code — not estimated by reading the dumped values. Use this instead of read_spreadsheet_range for questions like 'what's the total of column B'.",
)
async def fn_aggregate_spreadsheet_range(ctx, params: AggregateSpreadsheetParams) -> ActionResult:
    try:
        result, cell_count = await impl_aggregate_spreadsheet_range(ctx, params.file_id, params.cell_range, params.operation)
        return ActionResult.success(
            data=build_aggregate_result(params.file_id, params.cell_range, params.operation, result, cell_count),
            summary=f"{params.operation}({params.cell_range}) = {result} (over {cell_count} cell(s)).",
        )
    except Exception as e:
        return ActionResult.error(str(e), retryable=False)
