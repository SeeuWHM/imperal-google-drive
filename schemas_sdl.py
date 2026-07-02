"""Doc Reader — SDL entity classes + builders (imperal-sdk 5.9.x).

All @chat.function data_model= types live here, alongside the builder
functions that turn plain impl_* return values into SDL entities.
"""
from __future__ import annotations

from pydantic import Field

from imperal_sdk import sdl

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
    values: list[list] = Field(default_factory=list)


class SpreadsheetInfo(sdl.Entity):
    """Sheet names + dimensions — needed before a range can be addressed by
    name, since there is no way to guess a sheet's title otherwise."""
    kind: str = "spreadsheet_info"
    file_id: str | None = None
    sheets: list[dict] = Field(default_factory=list)


class AggregateResult(sdl.Entity):
    kind: str = "aggregate_result"
    file_id: str | None = None
    cell_range: str | None = None
    operation: str | None = None
    result: float = 0.0
    cell_count: int = 0


class AccountItem(sdl.Entity):
    """A connected Google account and the size of its own picked-files pool."""
    kind: str = "doc_account"
    email: str | None = None
    provider: str = "Google"
    is_active: bool = False
    file_count: int = 0


class AccountsList(sdl.EntityList[AccountItem]):
    connected: bool = False


class AccountSwitched(sdl.Entity):
    kind: str = "account_switched"
    active_account: str | None = None


class AccountDisconnected(sdl.Entity):
    kind: str = "account_disconnected"
    email: str | None = None
    remaining: int = 0


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


def build_spreadsheet_info(file_id: str, sheets: list[dict]) -> SpreadsheetInfo:
    return SpreadsheetInfo(id=file_id, title=f"Sheets in {file_id}", file_id=file_id, sheets=sheets)


def build_aggregate_result(file_id: str, cell_range: str, operation: str, result: float, cell_count: int) -> AggregateResult:
    return AggregateResult(
        id=f"{file_id}:{cell_range}:{operation}", title=f"{operation}({cell_range})",
        file_id=file_id, cell_range=cell_range, operation=operation, result=result, cell_count=cell_count,
    )


def build_account_item(acc: dict, file_count: int) -> AccountItem:
    email = acc.get("email") or acc.get("doc_id") or "?"
    return AccountItem(
        id=email, title=email,
        email=acc.get("email"), provider="Google",
        is_active=bool(acc.get("is_active", False)), file_count=file_count,
    )


def build_accounts_list(accounts_with_counts: list[tuple[dict, int]]) -> AccountsList:
    items = [build_account_item(a, c) for a, c in accounts_with_counts]
    return AccountsList(items=items, total=len(items), connected=bool(items))


def build_account_switched(active_account: str) -> AccountSwitched:
    return AccountSwitched(
        id=active_account or "none", title=f"Switched to {active_account}", active_account=active_account,
    )


def build_account_disconnected(email: str, remaining: int) -> AccountDisconnected:
    return AccountDisconnected(
        id=email or "none", title=f"Disconnected {email}", email=email, remaining=remaining,
    )
