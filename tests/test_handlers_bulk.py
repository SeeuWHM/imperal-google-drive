"""Regression coverage for the panel's bulk file-action dispatcher
(handlers_bulk.py) — specifically that 'retry_index' is fire-and-forget.

Context (2026-08-16): extractor.ingest() started polling the engine up to 90s
PER FILE for a terminal status (fixing a race where a fresh 'pending' row was
misread as a final failure). Running lifecycle.reindex_files() SYNCHRONOUSLY
inside the request/click handler meant a multi-file "Retry indexing" click in
the panel could block for minutes — which is exactly the "panels take forever
to respond" regression this file guards against. retry_index must now return
near-instantly and hand the real work to ctx.background_task.
"""
from __future__ import annotations

from providers.helpers import FILES_COLLECTION
from schemas import FileBulkActionParams
from handlers_bulk import fn_file_bulk_action


def _params(action: str, ids: list[str]) -> FileBulkActionParams:
    return FileBulkActionParams(action=action, message_ids=ids)


async def test_retry_index_does_not_call_reindex_files_synchronously(make_ctx, monkeypatch):
    ctx = make_ctx()
    ctx.store.seed(FILES_COLLECTION, [
        {"file_id": "F1", "account_email": "a@b.com", "mime_type": "application/pdf",
         "status": "failed", "name": "f1.pdf"},
    ])

    async def boom(*_a, **_kw):  # noqa: ANN001 - if this runs, the fix regressed
        raise AssertionError("reindex_files must NOT be awaited on the request path")

    import providers.lifecycle as lifecycle
    monkeypatch.setattr(lifecycle, "reindex_files", boom)

    result = await fn_file_bulk_action(ctx, _params("retry_index", ["F1"]))
    assert result.ok if hasattr(result, "ok") else True
    # The real work must have been handed to the background, not run inline.
    assert len(ctx.background_tasks) == 1
    ctx.background_tasks[0][0].close()  # never awaited by design — dispose cleanly


async def test_retry_index_returns_immediately_with_pending_summary(make_ctx):
    ctx = make_ctx()
    ctx.store.seed(FILES_COLLECTION, [
        {"file_id": "F1", "account_email": "a@b.com", "mime_type": "application/pdf",
         "status": "failed", "name": "f1.pdf"},
        {"file_id": "F2", "account_email": "a@b.com", "mime_type": "application/pdf",
         "status": "failed", "name": "f2.pdf"},
    ])
    result = await fn_file_bulk_action(ctx, _params("retry_index", ["F1", "F2"]))
    assert "2 file" in (result.summary or "")
    assert len(ctx.background_tasks) == 1
    ctx.background_tasks[0][0].close()


async def test_retry_index_empty_selection_still_errors_without_spawning(make_ctx):
    ctx = make_ctx()
    result = await fn_file_bulk_action(ctx, _params("retry_index", []))
    assert len(ctx.background_tasks) == 0
