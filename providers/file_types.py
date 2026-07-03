"""Pure mapping: a Drive file's mime type → how we fetch it for the engine and
how we detect changes.

This is the ONE place the 'Google-native vs binary' split lives. Everything
downstream (ingest, read, search) is type-agnostic — it just works with the
(url, content_key) this module produces. Keeping it pure (no ctx, no I/O) makes
the whole routing decision unit-testable in isolation.
"""
from __future__ import annotations

from urllib.parse import quote

from .helpers import (
    DRIVE_API,
    GOOGLE_DOC_MIME,
    GOOGLE_FOLDER_MIME,
    GOOGLE_SHEET_MIME,
    GOOGLE_SLIDE_MIME,
)

# Google-native export targets (Drive files.export). Chosen so the engine gets
# already-converted, RAG-ready content and does minimal reparsing:
#   Doc    → markdown (headings/lists preserved, best for chunking)
#   Slides → plain text (all slides)
#   Sheet  → xlsx (ALL tabs; CSV would export only the first sheet)
_EXPORT_MIME = {
    GOOGLE_DOC_MIME: "text/markdown",
    GOOGLE_SLIDE_MIME: "text/plain",
    GOOGLE_SHEET_MIME: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def is_folder(mime_type: str) -> bool:
    return mime_type == GOOGLE_FOLDER_MIME


def is_native(mime_type: str) -> bool:
    """A Google-native doc that must be EXPORTED (has no directly downloadable
    bytes) rather than downloaded via alt=media."""
    return mime_type in _EXPORT_MIME


def export_mime_for(mime_type: str) -> str | None:
    """The canonical export mime for a native type, else None (binary)."""
    return _EXPORT_MIME.get(mime_type)


def build_fetch_url(file_id: str, mime_type: str) -> str:
    """The URL the engine fetches this file's content from:
      native → files/{id}/export?mimeType=<canonical>
      binary → files/{id}?alt=media
    drive.file authorises both for a picked file."""
    export = _EXPORT_MIME.get(mime_type)
    if export:
        return f"{DRIVE_API}/files/{file_id}/export?mimeType={quote(export, safe='')}"
    return f"{DRIVE_API}/files/{file_id}?alt=media"


def content_key(mime_type: str, meta: dict) -> str:
    """Stable change key: unchanged file → same key → engine cache hit (no
    re-download/re-embed). Binary files expose md5Checksum; Google-native files
    have none, so key on the revision (modifiedTime + headRevisionId)."""
    if mime_type in _EXPORT_MIME:
        return f"{meta.get('modifiedTime', '')}:{meta.get('headRevisionId', '')}"
    return meta.get("md5Checksum") or f"{meta.get('modifiedTime', '')}:{meta.get('size', '')}"
