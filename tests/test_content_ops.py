"""Federal-grade tests for the CONTENT plane (providers/content_ops.py).

Pins the token-economy guarantees (windowed reads with a hard ceiling, top-K
search) and the two search modes (semantic global vs exact single-file), with
lifecycle + engine isolated via monkeypatch (both are covered by their own
suites). These are the tools Webby actually calls, so their contract matters.
"""
from __future__ import annotations

import pytest

from providers import content_ops, extractor, lifecycle


@pytest.fixture
def patched(monkeypatch):
    """Isolate content_ops: fake active account, record resolution, ensure_ready,
    touch. Returns a namespace where tests inject engine responses."""
    calls = {"read_text": [], "search": [], "overview": [], "touched": 0, "ensured": 0}

    async def fake_active_account(ctx):
        return {"email": "a@b.com", "access_token": "tok"}

    async def fake_resolve(ctx, acc, file_id):
        return {"doc_id": "d1", "file_id": file_id, "name": "a.pdf",
                "mime_type": "application/pdf", "size_bytes": 10,
                "status": lifecycle.READY, "document_id": 7}

    async def fake_ensure(ctx, acc, rec):
        calls["ensured"] += 1
        return rec["document_id"]

    async def fake_touch(ctx, rec):
        calls["touched"] += 1
        return rec

    monkeypatch.setattr(content_ops, "_active_account", fake_active_account)
    monkeypatch.setattr(content_ops.lifecycle, "resolve_record", fake_resolve)
    monkeypatch.setattr(content_ops.lifecycle, "ensure_ready", fake_ensure)
    monkeypatch.setattr(content_ops.lifecycle, "touch", fake_touch)
    return calls


# ── read_file ─────────────────────────────────────────────────────────────────


async def test_read_file_default_limit_and_shape(patched, make_ctx, monkeypatch):
    async def fake_read_text(ctx, document_id, offset=0, limit=0):
        assert document_id == 7
        assert limit == content_ops.DEFAULT_READ_LIMIT  # default applied
        return {"text": "hello world", "offset": 0, "total_chars": 11, "truncated": False}

    monkeypatch.setattr(content_ops.extractor, "read_text", fake_read_text)
    out = await content_ops.read_file(make_ctx(), "F1")
    assert out["text"] == "hello world"
    assert out["returned_chars"] == 11
    assert out["total_chars"] == 11
    assert out["has_more"] is False
    assert patched["ensured"] == 1 and patched["touched"] == 1


async def test_read_file_clamps_limit_to_max(patched, make_ctx, monkeypatch):
    seen = {}

    async def fake_read_text(ctx, document_id, offset=0, limit=0):
        seen["limit"] = limit
        return {"text": "x", "offset": 0, "total_chars": 1, "truncated": True}

    monkeypatch.setattr(content_ops.extractor, "read_text", fake_read_text)
    out = await content_ops.read_file(make_ctx(), "F1", offset=-5, limit=10_000_000)
    assert seen["limit"] == content_ops.MAX_READ_LIMIT   # clamped
    assert out["has_more"] is True


async def test_read_file_negative_offset_floored(patched, make_ctx, monkeypatch):
    seen = {}

    async def fake_read_text(ctx, document_id, offset=0, limit=0):
        seen["offset"] = offset
        return {"text": "", "offset": offset, "total_chars": 0, "truncated": False}

    monkeypatch.setattr(content_ops.extractor, "read_text", fake_read_text)
    await content_ops.read_file(make_ctx(), "F1", offset=-99)
    assert seen["offset"] == 0


# ── search_files — semantic (global) ──────────────────────────────────────────


async def test_search_semantic_global_topk(patched, make_ctx, monkeypatch):
    async def fake_search(ctx, query, k=0):
        assert k == content_ops.DEFAULT_SEARCH_K
        return [{"filename": "a.pdf", "seq": 3, "text": "chunk", "score": 0.8}]

    monkeypatch.setattr(content_ops.extractor, "search", fake_search)
    out = await content_ops.search_files(make_ctx(), "what is x")
    assert out["mode"] == "semantic" and out["file_id"] is None
    assert out["results"][0]["label"] == "a.pdf#3"
    assert out["results"][0]["text"] == "chunk"


async def test_search_semantic_k_clamped(patched, make_ctx, monkeypatch):
    seen = {}

    async def fake_search(ctx, query, k=0):
        seen["k"] = k
        return []

    monkeypatch.setattr(content_ops.extractor, "search", fake_search)
    await content_ops.search_files(make_ctx(), "q", k=999)
    assert seen["k"] == content_ops.MAX_SEARCH_K


# ── search_files — exact (single file) ────────────────────────────────────────


async def test_search_exact_single_file_greps_stored_text(patched, make_ctx, monkeypatch):
    async def fake_read_text(ctx, document_id, offset=0, limit=0):
        assert limit == content_ops.FULLTEXT_LIMIT
        return {"text": "alpha\nBETA line\ngamma\nbeta again", "total_chars": 30}

    called_search = {"n": 0}

    async def fake_search(ctx, query, k=0):
        called_search["n"] += 1
        return []

    monkeypatch.setattr(content_ops.extractor, "read_text", fake_read_text)
    monkeypatch.setattr(content_ops.extractor, "search", fake_search)
    out = await content_ops.search_files(make_ctx(), "beta", file_id="F1")
    assert out["mode"] == "exact" and out["file_id"] == "F1"
    # case-insensitive grep → both "BETA line" and "beta again"
    labels = [r["label"] for r in out["results"]]
    assert labels == ["line 2", "line 4"]
    assert called_search["n"] == 0  # exact mode must NOT hit semantic search


# ── file_overview ─────────────────────────────────────────────────────────────


async def test_file_overview_ready_includes_preview(patched, make_ctx, monkeypatch):
    async def fake_overview(ctx, document_id):
        assert document_id == 7
        return {"preview": "first 600 chars…"}

    monkeypatch.setattr(content_ops.extractor, "overview", fake_overview)
    out = await content_ops.file_overview(make_ctx(), "F1")
    assert out["status"] == "ready"
    assert out["preview"] == "first 600 chars…"
    assert out["name"] == "a.pdf" and out["mime_type"] == "application/pdf"


async def test_file_overview_not_ready_is_cheap_no_engine_call(make_ctx, monkeypatch):
    async def fake_active_account(ctx):
        return {"email": "a@b.com", "access_token": "tok"}

    async def fake_resolve(ctx, acc, file_id):
        return {"doc_id": "d1", "file_id": file_id, "name": "b.pptx",
                "mime_type": "application/vnd.ms", "size_bytes": 999,
                "status": lifecycle.PENDING, "document_id": None}

    async def boom(*a, **k):
        raise AssertionError("overview must not hit the engine when not ready")

    monkeypatch.setattr(content_ops, "_active_account", fake_active_account)
    monkeypatch.setattr(content_ops.lifecycle, "resolve_record", fake_resolve)
    monkeypatch.setattr(content_ops.extractor, "overview", boom)
    out = await content_ops.file_overview(make_ctx(), "F1")
    assert out["status"] == "pending"
    assert out["preview"] is None
    assert out["size_bytes"] == 999
