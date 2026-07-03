"""Google Drive · ACTION plane — edit_document / edit_spreadsheet /
spreadsheet_compute / write_text_file.

Thin @chat.function wrappers over providers/edit_ops (logic + tests there).
The only writes in the extension — each goes through the native Google API and
re-ingests the file so read_file/search_files stay fresh.
"""
from __future__ import annotations

import logging

from imperal_sdk.chat.action_result import ActionResult

from app import chat
from handlers_index import kick_reindex
from providers import edit_ops
from schemas import (
    AppendRowsParams,
    EditDocumentParams,
    EditSpreadsheetParams,
    FileIdParams,
    ReadSpreadsheetParams,
    SpreadsheetComputeParams,
    WriteTextParams,
)
from schemas_sdl import (
    ComputeResult,
    EditResult,
    SpreadsheetInfo,
    SpreadsheetRange,
    build_compute_result,
    build_edit_result,
    build_spreadsheet_info,
    build_spreadsheet_range,
)

log = logging.getLogger("doc_reader")

_OP_SUMMARY = {
    "replace": "Replaced {occ} occurrence(s).",
    "append": "Text appended.",
    "overwrite": "Document overwritten.",
}


@chat.function(
    "edit_document", action_type="write", event="file.edited", data_model=EditResult,
    description=(
        "Edit a Google Doc: op=replace (exact find-and-replace — fails if find_text has no match), "
        "op=append (add text to the end), or op=overwrite (replace the whole document). Changes the "
        "live document immediately."
    ),
)
async def fn_edit_document(ctx, params: EditDocumentParams) -> ActionResult:
    try:
        out = await edit_ops.edit_document(
            ctx, params.file_id, params.op,
            find_text=params.find_text, replace_text=params.replace_text,
            match_case=params.match_case, text=params.text, content=params.content,
        )
        await kick_reindex(ctx, params.file_id)   # refresh cache in the background
        occ = out.get("occurrences")
        summary = _OP_SUMMARY.get(out["op"], "Document edited.").format(occ=occ)
        return ActionResult.success(
            data=build_edit_result(params.file_id, op=out["op"], occurrences_changed=occ),
            summary=summary, refresh_panels=["doc_files"],
        )
    except Exception as e:
        return ActionResult.error(str(e), retryable=False)


@chat.function(
    "edit_spreadsheet", action_type="write", event="file.edited", data_model=EditResult,
    description=(
        "Write a 2D array of values into a Google Sheet range (A1 notation, e.g. 'Sheet1!A1:D10'). "
        "Overwrites only that range on the live sheet."
    ),
)
async def fn_edit_spreadsheet(ctx, params: EditSpreadsheetParams) -> ActionResult:
    try:
        await edit_ops.edit_spreadsheet(ctx, params.file_id, params.cell_range, params.values)
        await kick_reindex(ctx, params.file_id)   # refresh cache in the background
        return ActionResult.success(
            data=build_edit_result(params.file_id, op="edit_spreadsheet"),
            summary=f"Range {params.cell_range} updated.", refresh_panels=["doc_files"],
        )
    except Exception as e:
        return ActionResult.error(str(e), retryable=False)


@chat.function(
    "get_spreadsheet_info", action_type="read", data_model=SpreadsheetInfo,
    description=(
        "List a Google Sheet's tab names and dimensions (rows/columns). Call this FIRST when you need "
        "to read a specific range or figure out where to write/append — there's no way to guess the "
        "sheet name or size otherwise."
    ),
)
async def fn_get_spreadsheet_info(ctx, params: FileIdParams) -> ActionResult:
    try:
        sheets = await edit_ops.get_spreadsheet_info(ctx, params.file_id)
        summary = (f"{len(sheets)} sheet(s): " + ", ".join(s["name"] for s in sheets)) if sheets else "No sheets found."
        return ActionResult.success(data=build_spreadsheet_info(params.file_id, sheets), summary=summary)
    except Exception as e:
        return ActionResult.error(str(e), retryable=False)


@chat.function(
    "read_spreadsheet_range", action_type="read", data_model=SpreadsheetRange,
    description=(
        "Read a range of cells from a Google Sheet (A1 notation, e.g. 'Sheet1!A1:D20', or a bare sheet "
        "name for the whole sheet). Returns the raw 2D values — structured access for understanding the "
        "layout before editing/appending."
    ),
)
async def fn_read_spreadsheet_range(ctx, params: ReadSpreadsheetParams) -> ActionResult:
    try:
        values = await edit_ops.read_spreadsheet_range(ctx, params.file_id, params.cell_range)
        return ActionResult.success(data=build_spreadsheet_range(params.file_id, params.cell_range, values), summary=f"{len(values)} row(s).")
    except Exception as e:
        return ActionResult.error(str(e), retryable=False)


@chat.function(
    "append_spreadsheet_rows", action_type="write", event="file.edited", data_model=EditResult,
    description=(
        "Append new rows AFTER the existing data in a Google Sheet — it auto-finds the table, so you "
        "DON'T need to compute the target range. Use this to ADD records/rows (e.g. new companies). "
        "Defaults to the first sheet; pass a sheet name to target another tab."
    ),
)
async def fn_append_spreadsheet_rows(ctx, params: AppendRowsParams) -> ActionResult:
    try:
        n = await edit_ops.append_spreadsheet_rows(ctx, params.file_id, params.rows, params.cell_range)
        await kick_reindex(ctx, params.file_id)
        return ActionResult.success(
            data=build_edit_result(params.file_id, op="append_rows", occurrences_changed=n),
            summary=f"Appended {n} row(s).", refresh_panels=["doc_files"],
        )
    except Exception as e:
        return ActionResult.error(str(e), retryable=False)


@chat.function(
    "spreadsheet_compute", action_type="read", data_model=ComputeResult,
    description=(
        "Compute an EXACT sum/count/average/min/max over a Google Sheet range (A1 notation), "
        "calculated in code — not estimated from a text dump. Use for totals instead of reading raw cells."
    ),
)
async def fn_spreadsheet_compute(ctx, params: SpreadsheetComputeParams) -> ActionResult:
    try:
        out = await edit_ops.spreadsheet_compute(ctx, params.file_id, params.cell_range, params.operation)
        return ActionResult.success(
            data=build_compute_result({**out, "file_id": params.file_id}),
            summary=f"{out['operation']}({out['range']}) = {out['result']} over {out['cell_count']} cell(s).",
        )
    except Exception as e:
        return ActionResult.error(str(e), retryable=False)


@chat.function(
    "write_text_file", action_type="write", event="file.edited", data_model=EditResult,
    description=(
        "Overwrite a genuinely text-based Drive file (text/JSON/XML/YAML) with new content. "
        "Binary formats (PDF/DOCX/XLSX/PPTX) are read-only and will be refused."
    ),
)
async def fn_write_text_file(ctx, params: WriteTextParams) -> ActionResult:
    try:
        await edit_ops.write_text_file(ctx, params.file_id, params.content)
        await kick_reindex(ctx, params.file_id)   # refresh cache in the background
        return ActionResult.success(
            data=build_edit_result(params.file_id, op="write_text_file"),
            summary="File saved.", refresh_panels=["doc_files"],
        )
    except Exception as e:
        return ActionResult.error(str(e), retryable=False)
