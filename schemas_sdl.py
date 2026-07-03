"""Google Drive — SDL entity classes + builders (imperal-sdk 5.9.x).

All @chat.function data_model= types live here, with the builders that turn
plain impl_* return values into SDL entities. Unified toolset:
  CONTENT → FileText, SearchResults, FileOverview
  ACTION  → EditResult, ComputeResult
  FILES   → DocFile / DocFileList / FolderContents
  ACCOUNTS→ AccountItem / AccountsList / AccountSwitched / AccountDisconnected
  CONNECT → OAuthConnectResult / PickerLinkResult
"""
from __future__ import annotations

from pydantic import Field

from imperal_sdk import sdl

# ── Connect / picker ───────────────────────────────────────────────────────────


class OAuthConnectResult(sdl.Entity):
    kind: str = "oauth_connect"
    auth_url: str | None = None
    already_connected: bool = False
    instruction: str | None = None


class PickerLinkResult(sdl.Entity):
    kind: str = "picker_link"
    picker_url: str | None = None


# ── Files ──────────────────────────────────────────────────────────────────────


class DocFile(sdl.Entity, sdl.FileObject, sdl.Timestamped):
    """A file the user picked (or a folder they granted) through the Picker."""
    kind: str = "doc_file"
    status: str | None = None          # pending | indexing | ready | failed | cold
    is_folder: bool = False


class DocFileList(sdl.EntityList[DocFile]):
    pass


class FolderContents(sdl.EntityList[DocFile]):
    """The children of one granted folder (drill-in view)."""
    folder_id: str | None = None
    folder_name: str | None = None


# ── CONTENT plane ──────────────────────────────────────────────────────────────


class FileText(sdl.Entity, sdl.Bodied):
    """A windowed, character-addressed slice of any file's extracted text."""
    kind: str = "file_text"
    file_id: str | None = None
    offset: int = 0
    returned_chars: int = 0
    total_chars: int = 0
    has_more: bool = False


class SearchHit(sdl.Entity):
    kind: str = "search_hit"
    label: str = ""
    snippet: str = ""
    score: float | None = None


class SearchResults(sdl.EntityList[SearchHit]):
    query: str | None = None
    file_id: str | None = None
    mode: str | None = None            # semantic | exact


class FileOverview(sdl.Entity, sdl.Excerptable):
    kind: str = "file_overview"
    file_id: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = None
    status: str | None = None


# ── ACTION plane ───────────────────────────────────────────────────────────────


class EditResult(sdl.Entity):
    kind: str = "edit_result"
    file_id: str | None = None
    op: str | None = None
    occurrences_changed: int | None = None


class ComputeResult(sdl.Entity):
    kind: str = "compute_result"
    file_id: str | None = None
    cell_range: str | None = None
    operation: str | None = None
    result: float = 0.0
    cell_count: int = 0


class IndexResult(sdl.Entity):
    kind: str = "index_result"
    indexed: int = 0
    failed: int = 0


# ── Accounts ───────────────────────────────────────────────────────────────────


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


# ── Builders — plain dict/tuple -> SDL entity ─────────────────────────────────


def build_oauth_connect(auth_url: str | None, already_connected: bool, instruction: str | None) -> OAuthConnectResult:
    return OAuthConnectResult(
        id="google-drive-connect",
        title="Already connected" if already_connected else "Connect Google Drive",
        auth_url=auth_url, already_connected=already_connected, instruction=instruction,
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
        status=f.get("status"),
        is_folder=bool(f.get("is_folder")),
    )


def build_doc_file_list(files: list[dict]) -> DocFileList:
    return DocFileList(items=[build_doc_file(f) for f in files], total=len(files))


def build_folder_contents(folder_id: str, folder_name: str | None, files: list[dict]) -> FolderContents:
    return FolderContents(
        items=[build_doc_file(f) for f in files], total=len(files),
        folder_id=folder_id, folder_name=folder_name,
    )


def build_file_text(data: dict) -> FileText:
    fid = data.get("file_id")
    off = data.get("offset", 0)
    return FileText(
        id=str(fid),
        title=f"{data.get('name') or fid} (from char {off})",
        body=data.get("text", ""), body_format="plain",
        file_id=fid, offset=off,
        returned_chars=data.get("returned_chars", 0),
        total_chars=data.get("total_chars", 0),
        has_more=bool(data.get("has_more")),
    )


def build_search_results(data: dict) -> SearchResults:
    items = [
        SearchHit(id=f"{i}", title=r.get("label", ""), label=r.get("label", ""),
                  snippet=r.get("text", ""), score=r.get("score"))
        for i, r in enumerate(data.get("results", []))
    ]
    return SearchResults(
        items=items, total=len(items),
        query=data.get("query"), file_id=data.get("file_id"), mode=data.get("mode"),
    )


def build_file_overview(data: dict) -> FileOverview:
    return FileOverview(
        id=str(data.get("file_id")),
        title=data.get("name") or str(data.get("file_id")),
        excerpt=data.get("preview"),
        file_id=data.get("file_id"), mime_type=data.get("mime_type"),
        size_bytes=data.get("size_bytes"), status=data.get("status"),
    )


def build_edit_result(file_id: str, op: str | None = None, occurrences_changed: int | None = None) -> EditResult:
    return EditResult(id=file_id, title=f"Edited {file_id}", file_id=file_id, op=op, occurrences_changed=occurrences_changed)


def build_compute_result(data: dict) -> ComputeResult:
    return ComputeResult(
        id=f"{data.get('file_id')}:{data.get('range')}:{data.get('operation')}",
        title=f"{data.get('operation')}({data.get('range')})",
        file_id=data.get("file_id"), cell_range=data.get("range"),
        operation=data.get("operation"), result=float(data.get("result", 0.0)),
        cell_count=data.get("cell_count", 0),
    )


def build_index_result(indexed: int, failed: int) -> IndexResult:
    return IndexResult(id="index", title=f"Indexed {indexed} file(s)", indexed=indexed, failed=failed)


def build_account_item(acc: dict, file_count: int) -> AccountItem:
    email = acc.get("email") or acc.get("doc_id") or "?"
    return AccountItem(
        id=email, title=email, email=acc.get("email"), provider="Google",
        is_active=bool(acc.get("is_active", False)), file_count=file_count,
    )


def build_accounts_list(accounts_with_counts: list[tuple[dict, int]]) -> AccountsList:
    items = [build_account_item(a, c) for a, c in accounts_with_counts]
    return AccountsList(items=items, total=len(items), connected=bool(items))


def build_account_switched(active_account: str) -> AccountSwitched:
    return AccountSwitched(id=active_account or "none", title=f"Switched to {active_account}", active_account=active_account)


def build_account_disconnected(email: str, remaining: int) -> AccountDisconnected:
    return AccountDisconnected(id=email or "none", title=f"Disconnected {email}", email=email, remaining=remaining)
