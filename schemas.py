"""Pydantic parameter models for @chat.function handlers (unified toolset).

Two planes:
  CONTENT (read/understand any file) — FileIdParams, ReadFileParams, SearchFilesParams
  ACTION  (change/compute)           — EditDocumentParams, EditSpreadsheetParams,
                                       SpreadsheetComputeParams, WriteTextParams
Plus account / picker / folder management params.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class EmptyParams(BaseModel):
    pass


class FileIdParams(BaseModel):
    file_id: str = Field(description="Google Drive file ID (from list_files).")


class AccountParam(BaseModel):
    account: str = Field(description="Which connected Google account — its email (from list_accounts).")


class PickFilesParams(BaseModel):
    account: str = Field(default="", description="Which connected Google account to pick files for (email). Omit to use the active account.")


class FolderParams(BaseModel):
    folder_id: str = Field(description="Google Drive folder ID (from list_files) whose contents to list.")


class DisconnectFilesParams(BaseModel):
    file_ids: list[str] = Field(description="File IDs (from list_files) to remove in one bulk action.")


# ── CONTENT plane ─────────────────────────────────────────────────────────────


class ReadFileParams(BaseModel):
    file_id: str = Field(description="Google Drive file ID (from list_files). Any type — Doc, Sheet, Slides, PDF, PPTX, DOCX, XLSX, text.")
    offset: int = Field(default=0, description="0-based character offset to start reading from.")
    limit: int | None = Field(default=None, description="Max characters to return. Omit for a sensible default window (use offset to page a long file, or search_files to jump to the relevant part).")


class SearchFilesParams(BaseModel):
    query: str = Field(description="What to look for.")
    file_id: str = Field(default="", description="Optional: restrict to ONE file (exact substring search in that file). Omit to search across ALL your files by meaning (semantic).")


# ── ACTION plane ──────────────────────────────────────────────────────────────


class EditDocumentParams(BaseModel):
    file_id: str = Field(description="Google Doc file ID.")
    op: Literal["replace", "append", "overwrite"] = Field(description="replace = find-and-replace exact text; append = add to the end; overwrite = replace the whole document.")
    find_text: str | None = Field(default=None, description="For op=replace: exact text to find.")
    replace_text: str | None = Field(default=None, description="For op=replace: text to replace it with.")
    match_case: bool = Field(default=False, description="For op=replace: require exact case match.")
    text: str | None = Field(default=None, description="For op=append: text to add at the end.")
    content: str | None = Field(default=None, description="For op=overwrite: the new full document content.")


class EditSpreadsheetParams(BaseModel):
    file_id: str = Field(description="Google Sheets file ID.")
    cell_range: str = Field(description="A1 notation range, e.g. 'Sheet1!A1:D10'.")
    values: list[list[str | int | float | bool | None]] = Field(description="Row-major 2D array of cell values to write into the range.")


class SpreadsheetComputeParams(BaseModel):
    file_id: str = Field(description="Google Sheets file ID.")
    cell_range: str = Field(description="A1 notation range, e.g. 'Sheet1!B2:B50'.")
    operation: Literal["sum", "count", "average", "min", "max"] = Field(description="Exact computation over the range (count counts all non-empty cells; the rest use numeric cells).")


class WriteTextParams(BaseModel):
    file_id: str = Field(description="File ID of a genuinely text-based file (text/JSON/XML/YAML). Binary formats (PDF/DOCX/etc) are read-only.")
    content: str = Field(description="New full content — replaces everything currently in the file.")


class ReadSpreadsheetParams(BaseModel):
    file_id: str = Field(description="Google Sheets file ID.")
    cell_range: str = Field(description="A1 notation range, e.g. 'Sheet1!A1:D20', or a bare sheet name for the whole sheet.")


class AppendRowsParams(BaseModel):
    file_id: str = Field(description="Google Sheets file ID.")
    rows: list[list[str | int | float | bool | None]] = Field(description="Rows to append AFTER the existing data (row-major 2D array). Use for adding new records.")
    cell_range: str = Field(default="", description="Optional sheet/table name to append into. Omit to append to the first sheet.")


# ── Picker registration fallback ──────────────────────────────────────────────


class PickedFileInput(BaseModel):
    file_id: str
    name: str
    mime_type: str
    size_bytes: int = 0


class RegisterPickedFilesParams(BaseModel):
    files: list[PickedFileInput] = Field(description="Files copied from the Google Picker page's output box (JSON with a 'files' array). Paste that exact array here.")
