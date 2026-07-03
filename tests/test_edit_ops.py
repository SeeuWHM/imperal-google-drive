"""Federal-grade tests for the ACTION plane (providers/edit_ops.py).

Edits are the only writes — a wrong request shape corrupts the user's real
document, so request shape is pinned. Re-indexing after a write is NO LONGER
here (it moved to a background kick in the SDK handler, so the write returns
instantly), so these tests assert edit_ops just performs the native write.
Google API + record resolution isolated via monkeypatch.
"""
from __future__ import annotations

import pytest

from providers import edit_ops


@pytest.fixture
def patched(monkeypatch):
    """Fake active account + record resolution; capture the last docs
    batchUpdate requests / sheet update for shape assertions."""
    state = {"requests": None, "update": None}

    async def fake_active_account(ctx):
        return {"email": "a@b.com", "access_token": "tok"}

    async def fake_resolve(ctx, acc, file_id):
        return {"doc_id": "d1", "file_id": file_id, "name": "a.txt", "mime_type": "text/plain"}

    monkeypatch.setattr(edit_ops, "_active_account", fake_active_account)
    monkeypatch.setattr(edit_ops.lifecycle, "resolve_record", fake_resolve)
    return state


def _ok(json_data):
    from tests.conftest import FakeResponse  # reuse the shared double
    return FakeResponse(200, json_data)


# ── edit_document ─────────────────────────────────────────────────────────────


async def test_edit_document_replace_counts(patched, make_ctx, monkeypatch):
    async def fake_batch(ctx, acc, file_id, requests):
        patched["requests"] = requests
        return _ok({"replies": [{"replaceAllText": {"occurrencesChanged": 3}}]})

    monkeypatch.setattr(edit_ops, "docs_batch_update", fake_batch)
    out = await edit_ops.edit_document(make_ctx(), "F1", "replace", find_text="foo", replace_text="bar")
    assert out == {"op": "replace", "occurrences": 3}
    r = patched["requests"][0]["replaceAllText"]
    assert r["containsText"]["text"] == "foo" and r["replaceText"] == "bar"


async def test_edit_document_replace_zero_raises(patched, make_ctx, monkeypatch):
    async def fake_batch(ctx, acc, file_id, requests):
        return _ok({"replies": [{"replaceAllText": {"occurrencesChanged": 0}}]})

    monkeypatch.setattr(edit_ops, "docs_batch_update", fake_batch)
    with pytest.raises(RuntimeError):
        await edit_ops.edit_document(make_ctx(), "F1", "replace", find_text="x", replace_text="y")


async def test_edit_document_append(patched, make_ctx, monkeypatch):
    async def fake_batch(ctx, acc, file_id, requests):
        patched["requests"] = requests
        return _ok({"replies": []})

    monkeypatch.setattr(edit_ops, "docs_batch_update", fake_batch)
    out = await edit_ops.edit_document(make_ctx(), "F1", "append", text="more")
    assert out == {"op": "append"}
    assert patched["requests"][0]["insertText"]["text"] == "more"


async def test_edit_document_overwrite_deletes_then_inserts(patched, make_ctx, monkeypatch):
    async def fake_get(ctx, acc, file_id):
        return _ok({"body": {"content": [{"endIndex": 50}]}})

    async def fake_batch(ctx, acc, file_id, requests):
        patched["requests"] = requests
        return _ok({"replies": []})

    monkeypatch.setattr(edit_ops, "docs_get", fake_get)
    monkeypatch.setattr(edit_ops, "docs_batch_update", fake_batch)
    out = await edit_ops.edit_document(make_ctx(), "F1", "overwrite", content="brand new")
    assert out == {"op": "overwrite"}
    assert [list(r.keys())[0] for r in patched["requests"]] == ["deleteContentRange", "insertText"]


async def test_edit_document_unknown_op_raises(patched, make_ctx):
    with pytest.raises(ValueError):
        await edit_ops.edit_document(make_ctx(), "F1", "frobnicate")


# ── edit_spreadsheet ──────────────────────────────────────────────────────────


async def test_edit_spreadsheet_updates(patched, make_ctx, monkeypatch):
    async def fake_update(ctx, acc, file_id, cell_range, values):
        patched["update"] = (cell_range, values)
        return _ok({})

    monkeypatch.setattr(edit_ops, "sheets_update_values", fake_update)
    out = await edit_ops.edit_spreadsheet(make_ctx(), "S1", "Sheet1!A1:B1", [["x", "y"]])
    assert out == {"updated": True, "range": "Sheet1!A1:B1"}
    assert patched["update"] == ("Sheet1!A1:B1", [["x", "y"]])


# ── spreadsheet_compute ───────────────────────────────────────────────────────


async def test_spreadsheet_compute_exact(patched, make_ctx, monkeypatch):
    async def fake_get(ctx, acc, file_id, cell_range):
        return _ok({"values": [["1", "2"], ["3"]]})

    monkeypatch.setattr(edit_ops, "sheets_get_values", fake_get)
    out = await edit_ops.spreadsheet_compute(make_ctx(), "S1", "A1:B2", "sum")
    assert out["result"] == 6 and out["cell_count"] == 3


# ── structured Sheets access (needed to edit correctly) ───────────────────────


async def test_get_spreadsheet_info(patched, make_ctx, monkeypatch):
    async def fake_meta(ctx, acc, file_id):
        return _ok({"sheets": [{"properties": {"title": "Companies", "gridProperties": {"rowCount": 100, "columnCount": 8}}}]})

    monkeypatch.setattr(edit_ops, "sheets_get_metadata", fake_meta)
    out = await edit_ops.get_spreadsheet_info(make_ctx(), "S1")
    assert out == [{"name": "Companies", "row_count": 100, "column_count": 8}]


async def test_read_spreadsheet_range(patched, make_ctx, monkeypatch):
    async def fake_get(ctx, acc, file_id, cell_range):
        return _ok({"values": [["a", "b"], ["c"]]})

    monkeypatch.setattr(edit_ops, "sheets_get_values", fake_get)
    out = await edit_ops.read_spreadsheet_range(make_ctx(), "S1", "Sheet1!A1:B2")
    assert out == [["a", "b"], ["c"]]


async def test_append_rows_defaults_to_first_sheet(patched, make_ctx, monkeypatch):
    seen = {}

    async def fake_meta(ctx, acc, file_id):
        return _ok({"sheets": [{"properties": {"title": "Companies"}}]})

    async def fake_append(ctx, acc, file_id, cell_range, values):
        seen["range"] = cell_range
        seen["values"] = values
        return _ok({})

    monkeypatch.setattr(edit_ops, "sheets_get_metadata", fake_meta)
    monkeypatch.setattr(edit_ops, "sheets_append_values", fake_append)
    n = await edit_ops.append_spreadsheet_rows(make_ctx(), "S1", [["JUCARII.MD", "https://jucarii.md/"]])
    assert n == 1
    assert seen["range"] == "Companies"   # resolved to the first sheet
    assert seen["values"] == [["JUCARII.MD", "https://jucarii.md/"]]


async def test_append_rows_explicit_range_skips_metadata(patched, make_ctx, monkeypatch):
    seen = {}

    async def fake_append(ctx, acc, file_id, cell_range, values):
        seen["range"] = cell_range
        return _ok({})

    def boom(*a, **k):
        raise AssertionError("must not fetch metadata when a range is given")

    monkeypatch.setattr(edit_ops, "sheets_get_metadata", boom)
    monkeypatch.setattr(edit_ops, "sheets_append_values", fake_append)
    n = await edit_ops.append_spreadsheet_rows(make_ctx(), "S1", [["x"]], cell_range="Sheet2")
    assert n == 1 and seen["range"] == "Sheet2"


# ── write_text_file ───────────────────────────────────────────────────────────


async def test_write_text_file_ok(patched, make_ctx, monkeypatch):
    captured = {}

    async def fake_upload(ctx, acc, file_id, content, mime_type="text/plain"):
        captured["bytes"] = content
        captured["mime"] = mime_type
        return _ok({})

    monkeypatch.setattr(edit_ops, "drive_upload_media", fake_upload)
    out = await edit_ops.write_text_file(make_ctx(), "T1", "hello")
    assert out == {"saved": True}
    assert captured["bytes"] == b"hello" and captured["mime"] == "text/plain"


async def test_write_text_file_refuses_binary(make_ctx, monkeypatch):
    async def fake_active_account(ctx):
        return {"email": "a@b.com", "access_token": "tok"}

    async def fake_resolve(ctx, acc, file_id):
        return {"doc_id": "d1", "file_id": file_id, "name": "x.pdf", "mime_type": "application/pdf"}

    async def boom(*a, **k):
        raise AssertionError("must not upload to a binary file")

    monkeypatch.setattr(edit_ops, "_active_account", fake_active_account)
    monkeypatch.setattr(edit_ops.lifecycle, "resolve_record", fake_resolve)
    monkeypatch.setattr(edit_ops, "drive_upload_media", boom)
    with pytest.raises(RuntimeError):
        await edit_ops.write_text_file(make_ctx(), "P1", "nope")
