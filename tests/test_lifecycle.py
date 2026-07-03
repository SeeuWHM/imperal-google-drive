"""Federal-grade tests for the connector-side brain (providers/lifecycle.py).

The state machine, quota and cold-eviction decide correctness of the whole
"engine is a cache" model — a bug here means drift between panel and engine,
lost files, or quota bypass. HTTP is isolated via monkeypatch (the engine
client + Drive metadata are covered by their own suites); this pins the LOGIC:
status transitions, quota math, self-heal, eviction, folder-child materialise.
"""
from __future__ import annotations

import time

import pytest

from providers import extractor, lifecycle
from providers.helpers import FILES_COLLECTION

PDF = "application/pdf"


def make_acc(email="a@b.com"):
    # expires far in the future → _refresh_token_if_needed is a no-op (no HTTP)
    return {"doc_id": "acc1", "email": email, "access_token": "tok",
            "expires_at": time.time() + 3600, "is_active": True}


async def _seed_one(ctx, **overrides):
    rec = {"file_id": "F1", "name": "a.pdf", "mime_type": PDF, "size_bytes": 10,
           "account_email": "a@b.com", "status": lifecycle.PENDING, "document_id": None}
    rec.update(overrides)
    ctx.store.seed(FILES_COLLECTION, [rec])
    recs = await lifecycle._all_picked_files(ctx, "a@b.com")
    return recs[0]


# ── quota ─────────────────────────────────────────────────────────────────────


async def test_quota_state_counts_and_sums(make_ctx):
    ctx = make_ctx()
    ctx.store.seed(FILES_COLLECTION, [
        {"file_id": "A", "account_email": "a@b.com", "size_bytes": 100},
        {"file_id": "B", "account_email": "a@b.com", "size_bytes": 250},
        {"file_id": "C", "account_email": "other@b.com", "size_bytes": 999},
    ])
    count, total = await lifecycle.quota_state(ctx, "a@b.com")
    assert count == 2 and total == 350  # other account's file excluded


async def test_check_quota_passes_under_limit(make_ctx):
    ctx = make_ctx()
    await lifecycle.check_quota(ctx, "a@b.com", adding=1, adding_bytes=100)  # no raise


async def test_check_quota_docs_exceeded(make_ctx, monkeypatch):
    monkeypatch.setattr(lifecycle, "MAX_DOCS", 2)
    ctx = make_ctx()
    ctx.store.seed(FILES_COLLECTION, [
        {"file_id": "A", "account_email": "a@b.com", "size_bytes": 1},
        {"file_id": "B", "account_email": "a@b.com", "size_bytes": 1},
    ])
    with pytest.raises(RuntimeError):
        await lifecycle.check_quota(ctx, "a@b.com", adding=1)


async def test_check_quota_bytes_exceeded(make_ctx, monkeypatch):
    monkeypatch.setattr(lifecycle, "MAX_BYTES", 1000)
    ctx = make_ctx()
    ctx.store.seed(FILES_COLLECTION, [{"file_id": "A", "account_email": "a@b.com", "size_bytes": 900}])
    with pytest.raises(RuntimeError):
        await lifecycle.check_quota(ctx, "a@b.com", adding=1, adding_bytes=200)


# ── set_fields / touch ────────────────────────────────────────────────────────


async def test_set_fields_persists(make_ctx):
    ctx = make_ctx()
    rec = await _seed_one(ctx)
    await lifecycle.set_fields(ctx, rec, status=lifecycle.READY, document_id=9)
    assert rec["status"] == "ready" and rec["document_id"] == 9
    row = ctx.store.rows(FILES_COLLECTION)[0]
    assert row["status"] == "ready" and row["document_id"] == 9
    assert "doc_id" not in row  # transient key never persisted


async def test_touch_updates_last_access(make_ctx):
    ctx = make_ctx()
    rec = await _seed_one(ctx, last_access_at=0)
    await lifecycle.touch(ctx, rec)
    assert rec["last_access_at"] > 0
    assert ctx.store.rows(FILES_COLLECTION)[0]["last_access_at"] > 0


# ── index_record ──────────────────────────────────────────────────────────────


async def test_index_record_success(make_ctx, monkeypatch):
    ctx = make_ctx()
    rec = await _seed_one(ctx)

    async def fake_meta(ctx, acc, file_id):
        return {"md5Checksum": "abc"}

    async def fake_ingest(ctx, *, fetch_url, auth, content_key, filename):
        assert content_key == "abc"  # binary → md5
        assert fetch_url.endswith("/files/F1?alt=media")
        assert auth == "tok"
        return {"status": "processed", "document_id": 7}

    monkeypatch.setattr(lifecycle, "_drive_meta", fake_meta)
    monkeypatch.setattr(lifecycle.extractor, "ingest", fake_ingest)

    out = await lifecycle.index_record(ctx, make_acc(), rec)
    assert out["status"] == "ready"
    assert out["document_id"] == 7
    assert out["content_key"] == "abc"
    assert ctx.store.rows(FILES_COLLECTION)[0]["status"] == "ready"


async def test_index_record_unsupported_marks_failed(make_ctx, monkeypatch):
    ctx = make_ctx()
    rec = await _seed_one(ctx)

    async def fake_meta(ctx, acc, file_id):
        return {"md5Checksum": "abc"}

    async def fake_ingest(ctx, **kw):
        return {"status": "unsupported", "error": "video not read", "error_code": "unsupported_format"}

    monkeypatch.setattr(lifecycle, "_drive_meta", fake_meta)
    monkeypatch.setattr(lifecycle.extractor, "ingest", fake_ingest)

    with pytest.raises(RuntimeError):
        await lifecycle.index_record(ctx, make_acc(), rec)
    assert rec["status"] == "failed"
    assert "video" in (rec["error"] or "")


async def test_index_record_ingest_error_marks_failed(make_ctx, monkeypatch):
    ctx = make_ctx()
    rec = await _seed_one(ctx)

    async def fake_meta(ctx, acc, file_id):
        return {"md5Checksum": "abc"}

    async def fake_ingest(ctx, **kw):
        raise RuntimeError("engine down")

    monkeypatch.setattr(lifecycle, "_drive_meta", fake_meta)
    monkeypatch.setattr(lifecycle.extractor, "ingest", fake_ingest)

    with pytest.raises(RuntimeError):
        await lifecycle.index_record(ctx, make_acc(), rec)
    assert rec["status"] == "failed"


# ── ensure_ready (self-heal) ──────────────────────────────────────────────────


async def test_ensure_ready_returns_existing_without_reindex(make_ctx, monkeypatch):
    ctx = make_ctx()
    rec = await _seed_one(ctx, status=lifecycle.READY, document_id=5)

    async def boom(*a, **k):
        raise AssertionError("must not re-index a ready record")

    monkeypatch.setattr(lifecycle, "index_record", boom)
    assert await lifecycle.ensure_ready(ctx, make_acc(), rec) == 5


async def test_ensure_ready_raises_not_ready_never_indexes(make_ctx, monkeypatch):
    ctx = make_ctx()
    rec = await _seed_one(ctx, status=lifecycle.COLD, document_id=None)

    async def boom(*a, **k):
        raise AssertionError("reads must NEVER synchronously ingest (timeout risk)")

    monkeypatch.setattr(lifecycle, "index_record", boom)
    with pytest.raises(lifecycle.NotReadyError):
        await lifecycle.ensure_ready(ctx, make_acc(), rec)


# ── index_pending / kick_index (background) ──────────────────────────────────


async def test_index_pending_indexes_not_ready_skips_ready_and_folders(make_ctx, monkeypatch):
    ctx = make_ctx()
    ctx.store.seed(FILES_COLLECTION, [
        {"file_id": "P1", "account_email": "a@b.com", "mime_type": PDF, "status": lifecycle.PENDING, "document_id": None},
        {"file_id": "R1", "account_email": "a@b.com", "mime_type": PDF, "status": lifecycle.READY, "document_id": 7},
        {"file_id": "FOLD", "account_email": "a@b.com", "mime_type": "application/vnd.google-apps.folder", "status": lifecycle.PENDING, "document_id": None},
        {"file_id": "C1", "account_email": "a@b.com", "mime_type": PDF, "status": lifecycle.COLD, "document_id": None},
    ])
    seen = []

    async def fake_active(ctx):
        return make_acc()

    async def fake_index(ctx, acc, rec):
        seen.append(rec["file_id"])
        rec["status"] = lifecycle.READY
        return rec

    monkeypatch.setattr(lifecycle, "_active_account", fake_active)
    monkeypatch.setattr(lifecycle, "index_record", fake_index)
    res = await lifecycle.index_pending(ctx)
    assert res == {"indexed": 2, "failed": 0}
    assert set(seen) == {"P1", "C1"}  # ready + folder skipped


async def test_index_pending_counts_failures(make_ctx, monkeypatch):
    ctx = make_ctx()
    ctx.store.seed(FILES_COLLECTION, [
        {"file_id": "P1", "account_email": "a@b.com", "mime_type": PDF, "status": lifecycle.PENDING, "document_id": None},
        {"file_id": "P2", "account_email": "a@b.com", "mime_type": PDF, "status": lifecycle.PENDING, "document_id": None},
    ])

    async def fake_active(ctx):
        return make_acc()

    async def fake_index(ctx, acc, rec):
        if rec["file_id"] == "P2":
            raise RuntimeError("boom")
        return rec

    monkeypatch.setattr(lifecycle, "_active_account", fake_active)
    monkeypatch.setattr(lifecycle, "index_record", fake_index)
    assert await lifecycle.index_pending(ctx) == {"indexed": 1, "failed": 1}


async def test_kick_index_fires_long_running_background(make_ctx):
    ctx = make_ctx()
    fired = {}

    async def fake_bg(coro, *, long_running=False, name=""):
        fired["long_running"] = long_running
        fired["name"] = name
        coro.close()  # we never run it here
        return "task-1"

    ctx.background_task = fake_bg
    await lifecycle.kick_index(ctx)
    assert fired == {"long_running": True, "name": "gdrive-index"}


async def test_kick_index_noop_without_spawn_hook(make_ctx):
    ctx = make_ctx()  # FakeCtx has no background_task attr
    await lifecycle.kick_index(ctx)  # must not raise


# ── evict_cold ────────────────────────────────────────────────────────────────


async def test_evict_cold_evicts_stale(make_ctx, monkeypatch):
    ctx = make_ctx()
    rec = await _seed_one(ctx, status=lifecycle.READY, document_id=7,
                          last_access_at=time.time() - lifecycle.COLD_AFTER_S - 100)
    deleted = []

    async def fake_delete(ctx, doc_id):
        deleted.append(doc_id)
        return True

    monkeypatch.setattr(lifecycle.extractor, "delete", fake_delete)
    n = await lifecycle.evict_cold(ctx)
    assert n == 1 and deleted == [7]
    row = ctx.store.rows(FILES_COLLECTION)[0]
    assert row["status"] == "cold" and row["document_id"] is None


async def test_evict_cold_skips_warm(make_ctx, monkeypatch):
    ctx = make_ctx()
    await _seed_one(ctx, status=lifecycle.READY, document_id=7, last_access_at=time.time())
    monkeypatch.setattr(lifecycle.extractor, "delete",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("warm must not evict")))
    assert await lifecycle.evict_cold(ctx) == 0


async def test_evict_cold_skips_non_ready(make_ctx, monkeypatch):
    ctx = make_ctx()
    await _seed_one(ctx, status=lifecycle.PENDING, document_id=None,
                    last_access_at=time.time() - lifecycle.COLD_AFTER_S - 100)
    monkeypatch.setattr(lifecycle.extractor, "delete",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("non-ready must not evict")))
    assert await lifecycle.evict_cold(ctx) == 0


async def test_evict_cold_best_effort_on_delete_error(make_ctx, monkeypatch):
    ctx = make_ctx()
    rec = await _seed_one(ctx, status=lifecycle.READY, document_id=7,
                          last_access_at=time.time() - lifecycle.COLD_AFTER_S - 100)

    async def fake_delete(ctx, doc_id):
        raise RuntimeError("engine unreachable")

    monkeypatch.setattr(lifecycle.extractor, "delete", fake_delete)
    assert await lifecycle.evict_cold(ctx) == 0
    # record stays READY (not falsely marked cold) — will retry next sweep
    assert ctx.store.rows(FILES_COLLECTION)[0]["status"] == "ready"


# ── resolve_record ────────────────────────────────────────────────────────────


async def test_resolve_record_returns_existing(make_ctx, monkeypatch):
    ctx = make_ctx()

    async def fake_find(ctx, file_id):
        return {"file_id": file_id, "doc_id": "dX", "name": "a.pdf", "mime_type": PDF}

    monkeypatch.setattr(lifecycle, "_find_picked_file", fake_find)
    rec = await lifecycle.resolve_record(ctx, make_acc(), "F9")
    assert rec["doc_id"] == "dX"
    assert ctx.store.rows(FILES_COLLECTION) == []  # nothing created


async def test_resolve_record_materialises_folder_child(make_ctx, monkeypatch):
    ctx = make_ctx()

    async def fake_find(ctx, file_id):
        # folder child: accessible, but no store record yet (no doc_id)
        return {"file_id": file_id, "name": "child.pdf", "mime_type": PDF, "size_bytes": 42}

    monkeypatch.setattr(lifecycle, "_find_picked_file", fake_find)
    rec = await lifecycle.resolve_record(ctx, make_acc(), "CHILD")
    assert rec["doc_id"]  # created
    assert rec["status"] == "pending"
    rows = ctx.store.rows(FILES_COLLECTION)
    assert len(rows) == 1 and rows[0]["file_id"] == "CHILD"
    assert rows[0]["account_email"] == "a@b.com"


# ── forget_file / forget_account_files (panel + engine, no drift) ─────────────


async def test_forget_file_deletes_engine_doc_and_record(make_ctx, monkeypatch):
    ctx = make_ctx()
    await _seed_one(ctx, status=lifecycle.READY, document_id=7)
    deleted = []

    async def fake_delete(ctx, doc_id):
        deleted.append(doc_id)
        return True

    monkeypatch.setattr(lifecycle.extractor, "delete", fake_delete)
    await lifecycle.forget_file(ctx, "F1")
    assert deleted == [7]
    assert ctx.store.rows(FILES_COLLECTION) == []


async def test_forget_file_no_document_id_skips_engine(make_ctx, monkeypatch):
    ctx = make_ctx()
    await _seed_one(ctx, status=lifecycle.PENDING, document_id=None)
    monkeypatch.setattr(lifecycle.extractor, "delete",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no doc → no engine call")))
    await lifecycle.forget_file(ctx, "F1")
    assert ctx.store.rows(FILES_COLLECTION) == []


async def test_forget_file_not_found_raises(make_ctx):
    ctx = make_ctx()
    with pytest.raises(RuntimeError):
        await lifecycle.forget_file(ctx, "NOPE")


async def test_forget_file_engine_error_still_removes_record(make_ctx, monkeypatch):
    ctx = make_ctx()
    await _seed_one(ctx, status=lifecycle.READY, document_id=7)

    async def fake_delete(ctx, doc_id):
        raise RuntimeError("engine down")

    monkeypatch.setattr(lifecycle.extractor, "delete", fake_delete)
    await lifecycle.forget_file(ctx, "F1")  # must not raise
    assert ctx.store.rows(FILES_COLLECTION) == []


async def test_forget_account_files_scoped(make_ctx, monkeypatch):
    ctx = make_ctx()
    ctx.store.seed(FILES_COLLECTION, [
        {"file_id": "A", "account_email": "a@b.com", "document_id": 1},
        {"file_id": "B", "account_email": "a@b.com", "document_id": None},
        {"file_id": "C", "account_email": "other@b.com", "document_id": 9},
    ])
    deleted = []

    async def fake_delete(ctx, doc_id):
        deleted.append(doc_id)
        return True

    monkeypatch.setattr(lifecycle.extractor, "delete", fake_delete)
    n = await lifecycle.forget_account_files(ctx, "a@b.com")
    assert n == 2 and deleted == [1]  # only a@b.com; only the one with a doc
    remaining = [r["file_id"] for r in ctx.store.rows(FILES_COLLECTION)]
    assert remaining == ["C"]


# ── list_entries ──────────────────────────────────────────────────────────────


async def test_list_entries_empty_without_accounts(make_ctx):
    ctx = make_ctx()
    assert await lifecycle.list_entries(ctx) == []


async def test_list_entries_tags_folders(make_ctx):
    from providers.helpers import ACCOUNTS_COLLECTION
    ctx = make_ctx()
    ctx.store.seed(ACCOUNTS_COLLECTION, [{"email": "a@b.com", "is_active": True, "access_token": "t"}])
    ctx.store.seed(FILES_COLLECTION, [
        {"file_id": "F", "account_email": "a@b.com", "mime_type": PDF},
        {"file_id": "D", "account_email": "a@b.com", "mime_type": "application/vnd.google-apps.folder"},
    ])
    entries = await lifecycle.list_entries(ctx)
    by_id = {e["file_id"]: e for e in entries}
    assert by_id["F"]["is_folder"] is False
    assert by_id["D"]["is_folder"] is True
