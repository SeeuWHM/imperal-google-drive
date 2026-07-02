"""Doc Reader — SDL entity classes + builders (imperal-sdk 5.9.x).

All @chat.function data_model= types live here, alongside the builder
functions that turn plain impl_* return values into SDL entities.
"""
from __future__ import annotations

from imperal_sdk import sdl
from imperal_sdk.sdl import field as sdl_field

# ── Entities ─────────────────────────────────────────────────────────────────


class OAuthConnectResult(sdl.Entity):
    kind: str = "oauth_connect"
    auth_url: str | None = None
    already_connected: bool = False
    instruction: str | None = None


class PickerLinkResult(sdl.Entity):
    kind: str = "picker_link"
    picker_url: str | None = None


class DocFile(sdl.Entity, sdl.FileObject, sdl.Timestamped):
    """A file the user has picked through connect_google_docs' Google Picker."""
    kind: str = "doc_file"


class DocFileList(sdl.EntityList[DocFile]):
    pass


class TextWindow(sdl.Entity, sdl.Bodied):
    """A line-numbered window of a text file or document's content."""
    kind: str = "text_window"
    file_id: str | None = None
    offset: int = 0
    has_more: bool = False
    total_lines: int = 0


class SearchMatch(sdl.Entity):
    kind: str = "search_match"
    line_number: int = 0
    snippet: str = ""


class SearchResults(sdl.EntityList[SearchMatch]):
    file_id: str | None = None
    query: str | None = None


class DocStats(sdl.Entity, sdl.Excerptable):
    kind: str = "doc_stats"
    file_id: str | None = None
    char_count: int = 0
    paragraph_count: int | None = None


class EditResult(sdl.Entity):
    kind: str = "edit_result"
    file_id: str | None = None
    occurrences_changed: int | None = None


class SpreadsheetRange(sdl.Entity):
    kind: str = "spreadsheet_range"
    file_id: str | None = None
    cell_range: str | None = None
    row_count: int = 0
    values: list[list] = sdl_field(default_factory=list, role="content.body")


# ── Builders — plain dict/dataclass -> SDL entity ────────────────────────────


def build_oauth_connect(auth_url: str | None, already_connected: bool, instruction: str | None) -> OAuthConnectResult:
    return OAuthConnectResult(
        id="google-docs-connect",
        title="Already connected" if already_connected else "Connect Google Docs",
        auth_url=auth_url,
        already_connected=already_connected,
        instruction=instruction,
    )


def build_picker_link(picker_url: str) -> PickerLinkResult:
    return PickerLinkResult(id="picker-link", title="Pick files from Google Drive", picker_url=picker_url)


def build_doc_file(f: dict) -> DocFile:
    return DocFile(
        id=f["file_id"],
        title=f.get("name", f["file_id"]),
        filename=f.get("name"),
        mime_type=f.get("mime_type"),
        size_bytes=f.get("size_bytes"),
        updated_at=f.get("modified_at"),
    )


def build_doc_file_list(files: list[dict]) -> DocFileList:
    return DocFileList(items=[build_doc_file(f) for f in files], total=len(files))


def build_text_window(file_id: str, numbered_text: str, offset: int, has_more: bool, total_lines: int) -> TextWindow:
    return TextWindow(
        id=file_id, title=f"{file_id} (lines {offset + 1}-{offset + numbered_text.count(chr(10)) + 1})",
        body=numbered_text, body_format="plain",
        file_id=file_id, offset=offset, has_more=has_more, total_lines=total_lines,
    )


def build_search_results(file_id: str, query: str, matches: list[tuple[int, str]]) -> SearchResults:
    items = [SearchMatch(id=f"{file_id}:{ln}", title=f"line {ln}", line_number=ln, snippet=snippet) for ln, snippet in matches]
    return SearchResults(items=items, total=len(items), file_id=file_id, query=query)


def build_doc_stats(file_id: str, char_count: int, word_count: int, paragraph_count: int | None) -> DocStats:
    return DocStats(
        id=file_id, title=f"Stats for {file_id}",
        file_id=file_id, char_count=char_count, word_count=word_count, paragraph_count=paragraph_count,
    )


def build_edit_result(file_id: str, occurrences_changed: int | None = None) -> EditResult:
    return EditResult(id=file_id, title=f"Edited {file_id}", file_id=file_id, occurrences_changed=occurrences_changed)


def build_spreadsheet_range(file_id: str, cell_range: str, values: list[list]) -> SpreadsheetRange:
    return SpreadsheetRange(
        id=f"{file_id}:{cell_range}", title=f"{file_id} {cell_range}",
        file_id=file_id, cell_range=cell_range, row_count=len(values), values=values,
    )
