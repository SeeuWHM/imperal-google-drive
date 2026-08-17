"""Regression coverage for handlers_edit.py's post-edit cache refresh.

Context (found live 2026-08-16): a user edited a Doc via edit_document, got a
success reply, then immediately read the file back and still saw the OLD
text. Root cause: the edit handlers called kick_reindex() — fire-and-forget,
just STARTS a background re-ingest and returns immediately — so any read
right after an edit raced the still-running re-index and served stale cached
text. Since ingest() now caps its poll at _INGEST_POLL_MAX_S (90s, safely
under the 180s write-tool cap), a single edit can afford to synchronously
await the re-index instead, so a reply only comes back once the cache is
actually fresh. These tests pin that the edit handlers now AWAIT the
re-index (via await_reindex -> lifecycle.index_record) rather than merely
spawning a background task for it.
"""
from __future__ import annotations

from providers.helpers import ACCOUNTS_COLLECTION, FILES_COLLECTION
from schemas import EditDocumentParams, EditSpreadsheetParams
from handlers_edit import fn_edit_document, fn_edit_spreadsheet


async def test_edit_document_awaits_reindex_not_fire_and_forget(make_ctx, monkeypatch):
    ctx = make_ctx()
    ctx.store.seed(ACCOUNTS_COLLECTION, [
        {"email": "a@b.com", "access_token": "tok", "is_active": True},
    ])
    ctx.store.seed(FILES_COLLECTION, [
        {"file_id": "F1", "account_email": "a@b.com",
         "mime_type": "application/vnd.google-apps.document",
         "status": "ready", "name": "doc.gdoc"},
    ])

    import providers.edit_ops as edit_ops
    import providers.lifecycle as lifecycle

    async def fake_edit_document(ctx, file_id, op, **kw):
        return {"op": op, "occurrences": 1}

    calls = {"n": 0}

    async def fake_index_record(ctx, acc, rec):
        calls["n"] += 1
        return rec

    monkeypatch.setattr(edit_ops, "edit_document", fake_edit_document)
    monkeypatch.setattr(lifecycle, "index_record", fake_index_record)

    result = await fn_edit_document(
        ctx, EditDocumentParams(file_id="F1", op="replace",
                                find_text="a", replace_text="b"),
    )
    assert result.ok if hasattr(result, "ok") else True
    # The fix: re-index actually RAN inline (awaited), not merely queued.
    assert calls["n"] == 1
    # And it must NOT have gone through ctx.background_task at all — no
    # leftover un-awaited coroutine that could race a subsequent read.
    assert ctx.background_tasks == []


async def test_edit_spreadsheet_awaits_reindex_not_fire_and_forget(make_ctx, monkeypatch):
    ctx = make_ctx()
    ctx.store.seed(ACCOUNTS_COLLECTION, [
        {"email": "a@b.com", "access_token": "tok", "is_active": True},
    ])
    ctx.store.seed(FILES_COLLECTION, [
        {"file_id": "F2", "account_email": "a@b.com",
         "mime_type": "application/vnd.google-apps.spreadsheet",
         "status": "ready", "name": "sheet.gsheet"},
    ])

    import providers.edit_ops as edit_ops
    import providers.lifecycle as lifecycle

    async def fake_edit_spreadsheet(ctx, file_id, cell_range, values):
        return None

    calls = {"n": 0}

    async def fake_index_record(ctx, acc, rec):
        calls["n"] += 1
        return rec

    monkeypatch.setattr(edit_ops, "edit_spreadsheet", fake_edit_spreadsheet)
    monkeypatch.setattr(lifecycle, "index_record", fake_index_record)

    result = await fn_edit_spreadsheet(
        ctx, EditSpreadsheetParams(file_id="F2", cell_range="Sheet1!A1:A1", values=[[1]]),
    )
    assert result.ok if hasattr(result, "ok") else True
    assert calls["n"] == 1
    assert ctx.background_tasks == []
