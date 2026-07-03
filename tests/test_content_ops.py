"""Federal-grade tests for the UNIVERSAL, list-based CONTENT plane
(providers/content_ops.py). Pins: 1..N handling, per-file token window (full for
one, preview for many), parallel fan-out, per-file status (ok/preparing/error)
never failing the batch, and the two search modes. Lifecycle + engine isolated
via monkeypatch.
"""
from __future__ import annotations

import pytest

from providers import content_ops, lifecycle


@pytest.fixture
def base(monkeypatch):
    async def fake_active(ctx):
        return {"email": "a@b.com", "access_token": "tok"}

    async def fake_touch(ctx, rec):
        return rec

    monkeypatch.setattr(content_ops, "_active_account", fake_active)
    monkeypatch.setattr(content_ops.lifecycle, "touch", fake_touch)


# ── read_files ────────────────────────────────────────────────────────────────


async def test_read_files_single_full_window(base, make_ctx, monkeypatch):
    seen = {}

    async def fake_resolve(ctx, acc, fid):
        return {"file_id": fid, "name": "a.pdf", "status": lifecycle.READY, "document_id": 7}

    async def fake_ensure(ctx, acc, rec):
        return rec["document_id"]

    async def fake_read(ctx, doc_id, offset=0, limit=0):
        seen["limit"] = limit
        return {"text": "hello", "offset": 0, "total_chars": 5, "truncated": False}

    monkeypatch.setattr(content_ops.lifecycle, "resolve_record", fake_resolve)
    monkeypatch.setattr(content_ops.lifecycle, "ensure_ready", fake_ensure)
    monkeypatch.setattr(content_ops.extractor, "read_text", fake_read)
    out = await content_ops.read_files(make_ctx(), ["F1"])
    assert len(out) == 1 and out[0]["status"] == "ok" and out[0]["text"] == "hello"
    assert seen["limit"] == content_ops.DEFAULT_READ_LIMIT   # single → full window


async def test_read_files_multi_uses_preview_limit(base, make_ctx, monkeypatch):
    seen = []

    async def fake_resolve(ctx, acc, fid):
        return {"file_id": fid, "name": fid, "status": lifecycle.READY, "document_id": 1}

    async def fake_ensure(ctx, acc, rec):
        return 1

    async def fake_read(ctx, doc_id, offset=0, limit=0):
        seen.append(limit)
        return {"text": "x", "offset": 0, "total_chars": 1, "truncated": False}

    monkeypatch.setattr(content_ops.lifecycle, "resolve_record", fake_resolve)
    monkeypatch.setattr(content_ops.lifecycle, "ensure_ready", fake_ensure)
    monkeypatch.setattr(content_ops.extractor, "read_text", fake_read)
    out = await content_ops.read_files(make_ctx(), ["A", "B", "C"])
    assert len(out) == 3 and all(r["status"] == "ok" for r in out)
    assert seen == [content_ops.MULTI_READ_LIMIT] * 3   # many → bounded preview each


async def test_read_files_preparing_and_error_dont_fail_batch(base, make_ctx, monkeypatch):
    async def fake_resolve(ctx, acc, fid):
        if fid == "BAD":
            raise RuntimeError("not picked for the active account")
        return {"file_id": fid, "name": fid, "status": lifecycle.PENDING, "document_id": None}

    async def fake_ensure(ctx, acc, rec):
        if rec["file_id"] == "PREP":
            raise lifecycle.NotReadyError("preparing")
        return 9

    async def fake_read(ctx, doc_id, offset=0, limit=0):
        return {"text": "ok", "total_chars": 2, "truncated": False}

    monkeypatch.setattr(content_ops.lifecycle, "resolve_record", fake_resolve)
    monkeypatch.setattr(content_ops.lifecycle, "ensure_ready", fake_ensure)
    monkeypatch.setattr(content_ops.extractor, "read_text", fake_read)
    out = await content_ops.read_files(make_ctx(), ["OK", "PREP", "BAD"])
    by = {r["file_id"]: r for r in out}
    assert by["OK"]["status"] == "ok"
    assert by["PREP"]["status"] == "preparing"
    assert by["BAD"]["status"] == "error"


# ── file_overview ─────────────────────────────────────────────────────────────


async def test_file_overview_ready_and_pending(base, make_ctx, monkeypatch):
    async def fake_resolve(ctx, acc, fid):
        if fid == "R":
            return {"file_id": "R", "name": "r", "mime_type": "application/pdf", "size_bytes": 10, "status": lifecycle.READY, "document_id": 7}
        return {"file_id": "P", "name": "p", "mime_type": "x", "size_bytes": 5, "status": lifecycle.PENDING, "document_id": None}

    async def fake_overview(ctx, doc_id):
        return {"preview": "abc"}

    monkeypatch.setattr(content_ops.lifecycle, "resolve_record", fake_resolve)
    monkeypatch.setattr(content_ops.extractor, "overview", fake_overview)
    out = await content_ops.file_overview(make_ctx(), ["R", "P"])
    by = {r["file_id"]: r for r in out}
    assert by["R"]["preview"] == "abc" and by["R"]["status"] == "ready"
    assert by["P"]["preview"] is None and by["P"]["status"] == "pending"


# ── search_files ──────────────────────────────────────────────────────────────


async def test_search_semantic_global(base, make_ctx, monkeypatch):
    async def fake_search(ctx, query, k=0):
        return [{"filename": "a.pdf", "seq": 3, "text": "chunk", "score": 0.8}]

    monkeypatch.setattr(content_ops.extractor, "search", fake_search)
    out = await content_ops.search_files(make_ctx(), "x")
    assert out["mode"] == "semantic" and out["results"][0]["label"] == "a.pdf#3"


async def test_search_exact_across_files_parallel(base, make_ctx, monkeypatch):
    async def fake_resolve(ctx, acc, fid):
        return {"file_id": fid, "name": fid, "status": lifecycle.READY, "document_id": 1}

    async def fake_ensure(ctx, acc, rec):
        return 1

    async def fake_read(ctx, doc_id, offset=0, limit=0):
        return {"text": "alpha\nbeta\ngamma beta"}

    async def boom(*a, **k):
        raise AssertionError("must not run semantic search when file_ids are given")

    monkeypatch.setattr(content_ops.lifecycle, "resolve_record", fake_resolve)
    monkeypatch.setattr(content_ops.lifecycle, "ensure_ready", fake_ensure)
    monkeypatch.setattr(content_ops.extractor, "read_text", fake_read)
    monkeypatch.setattr(content_ops.extractor, "search", boom)
    out = await content_ops.search_files(make_ctx(), "beta", file_ids=["F1", "F2"])
    assert out["mode"] == "exact"
    assert len(out["results"]) == 4   # 2 matching lines × 2 files
