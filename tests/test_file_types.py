"""Federal-grade tests for the pure mime-routing module (providers/file_types.py).

This is the ONE branch between Google-native and binary files — a wrong URL or
an unstable content_key here breaks reads for a whole file class or defeats the
idempotency cache, so every mapping is pinned.
"""
from __future__ import annotations

from providers import file_types as ft
from providers.helpers import (
    GOOGLE_DOC_MIME,
    GOOGLE_FOLDER_MIME,
    GOOGLE_SHEET_MIME,
    GOOGLE_SLIDE_MIME,
)

PDF = "application/pdf"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


# ── classification ────────────────────────────────────────────────────────────


def test_is_folder():
    assert ft.is_folder(GOOGLE_FOLDER_MIME) is True
    assert ft.is_folder(GOOGLE_DOC_MIME) is False
    assert ft.is_folder(PDF) is False


def test_is_native_true_for_google_types():
    assert ft.is_native(GOOGLE_DOC_MIME)
    assert ft.is_native(GOOGLE_SHEET_MIME)
    assert ft.is_native(GOOGLE_SLIDE_MIME)


def test_is_native_false_for_binary_and_folder():
    assert not ft.is_native(PDF)
    assert not ft.is_native(PPTX)
    assert not ft.is_native(GOOGLE_FOLDER_MIME)


# ── export mime ───────────────────────────────────────────────────────────────


def test_export_mime_doc_is_markdown():
    assert ft.export_mime_for(GOOGLE_DOC_MIME) == "text/markdown"


def test_export_mime_slides_is_plain():
    assert ft.export_mime_for(GOOGLE_SLIDE_MIME) == "text/plain"


def test_export_mime_sheet_is_xlsx_for_all_tabs():
    assert ft.export_mime_for(GOOGLE_SHEET_MIME) == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


def test_export_mime_none_for_binary():
    assert ft.export_mime_for(PDF) is None


# ── fetch url ─────────────────────────────────────────────────────────────────


def test_fetch_url_binary_is_alt_media():
    url = ft.build_fetch_url("FILE1", PDF)
    assert url.endswith("/files/FILE1?alt=media")


def test_fetch_url_native_is_export_with_encoded_mime():
    url = ft.build_fetch_url("FILE2", GOOGLE_DOC_MIME)
    assert "/files/FILE2/export?mimeType=" in url
    assert "text%2Fmarkdown" in url  # mime is URL-encoded


def test_fetch_url_sheet_exports_xlsx():
    url = ft.build_fetch_url("S1", GOOGLE_SHEET_MIME)
    assert "/files/S1/export?mimeType=" in url
    assert "spreadsheetml.sheet" in url


# ── content key (change detection / cache) ────────────────────────────────────


def test_content_key_binary_uses_md5():
    assert ft.content_key(PDF, {"md5Checksum": "abc123"}) == "abc123"


def test_content_key_binary_fallback_when_no_md5():
    key = ft.content_key(PDF, {"modifiedTime": "2026-07-03T10:00:00Z", "size": "42"})
    assert key == "2026-07-03T10:00:00Z:42"


def test_content_key_native_uses_revision():
    key = ft.content_key(GOOGLE_DOC_MIME, {
        "modifiedTime": "2026-07-03T10:00:00Z", "headRevisionId": "REV9",
    })
    assert key == "2026-07-03T10:00:00Z:REV9"


def test_content_key_native_ignores_md5_even_if_present():
    # A native file must key on revision, not any stray md5 — stability matters.
    key = ft.content_key(GOOGLE_SHEET_MIME, {
        "modifiedTime": "t1", "headRevisionId": "r1", "md5Checksum": "should-be-ignored",
    })
    assert key == "t1:r1"


def test_content_key_stable_for_same_input():
    meta = {"md5Checksum": "same"}
    assert ft.content_key(PDF, meta) == ft.content_key(PDF, meta)
