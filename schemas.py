"""Pydantic parameter models for @chat.function handlers."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class EmptyParams(BaseModel):
    pass


class FileIdParams(BaseModel):
    file_id: str = Field(description="Google Drive file ID (from list_connected_files).")


class ReadRangeParams(BaseModel):
    file_id: str = Field(description="Google Drive file ID.")
    offset: int = Field(default=0, description="0-based starting line number.")
    limit: int | None = Field(default=None, description="Max number of lines to return. Omit to read to the end.")


class SearchParams(BaseModel):
    file_id: str = Field(description="Google Drive file ID.")
    query: str = Field(description="Text to search for (case-insensitive by default).")
    case_sensitive: bool = Field(default=False, description="Match case exactly.")


class ReplaceTextParams(BaseModel):
    file_id: str = Field(description="Google Doc file ID.")
    find_text: str = Field(description="Exact text to find.")
    replace_text: str = Field(description="Text to replace it with.")
    match_case: bool = Field(default=False, description="Require exact case match.")


class AppendTextParams(BaseModel):
    file_id: str = Field(description="Google Doc file ID.")
    text: str = Field(description="Text to append at the end of the document.")


class OverwriteTextParams(BaseModel):
    file_id: str = Field(description="File ID (Google Doc or plain text file).")
    content: str = Field(description="New full content — replaces everything currently in the file.")


class ReadSpreadsheetParams(BaseModel):
    file_id: str = Field(description="Google Sheets file ID.")
    cell_range: str = Field(description="A1 notation range, e.g. 'Sheet1!A1:D10', or just a sheet name for the whole sheet.")


class WriteSpreadsheetParams(BaseModel):
    file_id: str = Field(description="Google Sheets file ID.")
    cell_range: str = Field(description="A1 notation range, e.g. 'Sheet1!A1:D10'.")
    values: list[list[str | int | float | bool | None]] = Field(description="Row-major 2D array of cell values.")


class AggregateSpreadsheetParams(BaseModel):
    file_id: str = Field(description="Google Sheets file ID.")
    cell_range: str = Field(description="A1 notation range, e.g. 'Sheet1!B2:B50'.")
    operation: Literal["sum", "count", "average", "min", "max"] = Field(description="Exact computation to run over the range's numeric cells (count counts all non-empty cells, not just numeric ones).")


class PickedFileInput(BaseModel):
    file_id: str
    name: str
    mime_type: str
    size_bytes: int = 0


class RegisterPickedFilesParams(BaseModel):
    files: list[PickedFileInput] = Field(description="Files copied from the Google Picker page's output box (JSON with a 'files' array). Paste that exact array here.")
