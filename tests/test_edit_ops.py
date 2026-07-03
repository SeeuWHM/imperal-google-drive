"""Federal-grade tests for the ACTION plane (providers/edit_ops.py).

Edits are the only writes — a wrong request shape corrupts the user's real
document, and a missing re-ingest leaves stale reads/search. Both are pinned,
with the Google API + lifecycle isolated via monkeypatch.
"""
from __future__ import annotations

import pytest

from providers import edit_ops


@pytest.fixture
def patched(monkeypatch):
    """Fake active account + record resolution; count re-ingests; capture the
    last docs batchUpdate requests for shape assertions."""
    state = {"reindex": 0, "requests": None, "update": None}

    async def fake_active_account(ctx):
        return {"email": "a@b.com", "access_token": "tok"}

    async def fake_resolve(ctx, acc, file_id):
        return {"doc_id": "d1", "file_id": file_id, "name": "a.txt", "mime_type": "text/plain"}

    async def fake_reindex(ctx, acc, rec):
        state["reindex"] += 1

    monkeypatch.setattr(edit_ops, "_active_account", fake_active_account)
    monkeypatch.setattr(edit_ops.lifecycle, "resolve_record", fake_resolve)
    monkeypatch.setattr(edit_ops, "_reindex", fake_reindex)
    return state


def _ok(json_data):
    from tests.conftest import FakeResponse  # reuse the shared double
    return FakeResponse(200, json_data)


# ── edit_document ─────────────────────────────────────────────────────────────


async def test_edit_document_replace_counts_and_reindexes(patched, make_ctx, monkeypatch):
    async def fake_batch(ctx, acc, file_id, requests):
        patched["requests"] = requests
        return _ok({"replies": [{"replaceAllText": {"occurrencesChanged": 3}}]})

    monkeypatch.setattr(edit_ops, "docs_batch_update", fake_batch)
    out = await edit_ops.edit_document(make_ctx(), "F1", "replace",
                                       find_text="foo", replace_text="bar")
    assert out == {"op": "replace", "occurrences": 3}
    assert patched["requests"][0]["replaceAllText"]["containsText"]["text"] == "foo"
    assert patched["requests"][0]["replaceAllText"]["replaceText"] == "bar"
    assert patched["reindex"] == 1


async def test_edit_document_replace_zero_raises_and_no_reindex(patched, make_ctx, monkeypatch):
    async def fake_batch(ctx, acc, file_id, requests):
        return _ok({"replies": [{"replaceAllText": {"occurrencesChanged": 0}}]})

    monkeypatch.setattr(edit_ops, "docs_batch_update", fake_batch)
    with pytest.raises(RuntimeError):
        await edit_ops.edit_document(make_ctx(), "F1", "replace", find_text="x", replace_text="y")
    assert patched["reindex"] == 0  # nothing changed → no re-ingest


async def test_edit_document_append(patched, make_ctx, monkeypatch):
    async def fake_batch(ctx, acc, file_id, requests):
        patched["requests"] = requests
        return _ok({"replies": []})

    monkeypatch.setattr(edit_ops, "docs_batch_update", fake_batch)
    out = await edit_ops.edit_document(make_ctx(), "F1", "append", text="more")
    assert out == {"op": "append"}
    assert patched["requests"][0]["insertText"]["text"] == "more"
    assert patched["reindex"] == 1


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
    kinds = [list(r.keys())[0] for r in patched["requests"]]
    assert kinds == ["deleteContentRange", "insertText"]
    assert patched["reindex"] == 1


async def test_edit_document_unknown_op_raises(patched, make_ctx):
    with pytest.raises(ValueError):
        await edit_ops.edit_document(make_ctx(), "F1", "frobnicate")


async def test_edit_document_reindex_failure_is_swallowed(make_ctx, monkeypatch):
    # real _reindex path: index_record raises → edit still succeeds
    async def fake_active_account(ctx):
        return {"email": "a@b.com", "access_token": "tok"}

    async def fake_resolve(ctx, acc, file_id):
        return {"doc_id": "d1", "file_id": file_id, "mime_type": "text/plain"}

    async def fake_index(ctx, acc, rec):
        raise RuntimeError("engine down")

    async def fake_batch(ctx, acc, file_id, requests):
        return _ok({"replies": [{"replaceAllText": {"occurrencesChanged": 1}}]})

    monkeypatch.setattr(edit_ops, "_active_account", fake_active_account)
    monkeypatch.setattr(edit_ops.lifecycle, "resolve_record", fake_resolve)
    monkeypatch.setattr(edit_ops.lifecycle, "index_record", fake_index)
    monkeypatch.setattr(edit_ops, "docs_batch_update", fake_batch)
    out = await edit_ops.edit_document(make_ctx(), "F1", "replace", find_text="a", replace_text="b")
    assert out["occurrences"] == 1  # edit succeeded despite re-ingest failure


# ── edit_spreadsheet ──────────────────────────────────────────────────────────


async def test_edit_spreadsheet_updates_and_reindexes(patched, make_ctx, monkeypatch):
    async def fake_update(ctx, acc, file_id, cell_range, values):
        patched["update"] = (cell_range, values)
        return _ok({})

    monkeypatch.setattr(edit_ops, "sheets_update_values", fake_update)
    out = await edit_ops.edit_spreadsheet(make_ctx(), "S1", "Sheet1!A1:B1", [["x", "y"]])
    assert out == {"updated": True, "range": "Sheet1!A1:B1"}
    assert patched["update"] == ("Sheet1!A1:B1", [["x", "y"]])
    assert patched["reindex"] == 1


# ── spreadsheet_compute ───────────────────────────────────────────────────────


async def test_spreadsheet_compute_exact_no_reindex(patched, make_ctx, monkeypatch):
    async def fake_get(ctx, acc, file_id, cell_range):
        return _ok({"values": [["1", "2"], ["3"]]})

    monkeypatch.setattr(edit_ops, "sheets_get_values", fake_get)
    out = await edit_ops.spreadsheet_compute(make_ctx(), "S1", "A1:B2", "sum")
    assert out["result"] == 6 and out["cell_count"] == 3
    assert patched["reindex"] == 0  # read-only


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
    assert patched["reindex"] == 1


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
