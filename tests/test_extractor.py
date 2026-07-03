"""Federal-grade tests for the doc-extractor engine client (providers/extractor.py).

This client is the single CONTENT-plane gateway. Three things are correctness-
critical and all asserted here:
  1. Exact request shape — `source` is ALWAYS "gdrive" and every call is scoped
     by imperal_id (isolation boundary; a leak here = cross-user/project leak).
  2. Transient retry — one retry on 5xx/network error, but NEVER on 4xx.
  3. Error/404 handling — 404 self-heal signal on read, graceful on delete.
"""
from __future__ import annotations

import pytest

from providers import extractor
from providers.extractor import SOURCE


# ── imperal_id scoping ────────────────────────────────────────────────────────


def test_imperal_id_returns_user_id(make_ctx):
    assert extractor.imperal_id(make_ctx()) == "user-123"


def test_imperal_id_missing_user_raises(make_ctx):
    with pytest.raises(RuntimeError):
        extractor.imperal_id(make_ctx(with_user=False))


def test_imperal_id_empty_raises(make_ctx):
    with pytest.raises(RuntimeError):
        extractor.imperal_id(make_ctx(imperal_id=""))


def test_source_is_gdrive():
    assert SOURCE == "gdrive"


# ── ingest ────────────────────────────────────────────────────────────────────


async def test_ingest_sends_exact_shape_and_returns_doc(make_ctx, resp):
    doc = {"document_id": 7, "status": "processed"}
    ctx = make_ctx([resp(200, {"data": {"documents": [doc]}})])
    out = await extractor.ingest(ctx, fetch_url="https://drive/x?alt=media",
                                 auth="tok", content_key="md5abc", filename="f.pdf")
    assert out == doc
    method, url, kwargs = ctx.http.calls[0]
    assert method == "post"
    assert url == extractor._DOCUMENTS_URL
    data = kwargs["data"]
    assert data["source"] == "gdrive"
    assert data["imperal_id"] == "user-123"
    assert data["url"] == "https://drive/x?alt=media"
    assert data["auth"] == "tok"
    assert data["content_key"] == "md5abc"
    assert data["filename"] == "f.pdf"
    assert kwargs["timeout"] == 120


async def test_ingest_cached_status_passthrough(make_ctx, resp):
    ctx = make_ctx([resp(200, {"data": {"documents": [{"document_id": 7, "status": "cached"}]}})])
    out = await extractor.ingest(ctx, fetch_url="u", auth="t", content_key="c", filename="f")
    assert out["status"] == "cached"


async def test_ingest_no_documents_raises(make_ctx, resp):
    ctx = make_ctx([resp(200, {"data": {"documents": []}})])
    with pytest.raises(RuntimeError):
        await extractor.ingest(ctx, fetch_url="u", auth="t", content_key="c", filename="f")


async def test_ingest_missing_data_key_raises(make_ctx, resp):
    ctx = make_ctx([resp(200, {})])
    with pytest.raises(RuntimeError):
        await extractor.ingest(ctx, fetch_url="u", auth="t", content_key="c", filename="f")


# ── read_text ─────────────────────────────────────────────────────────────────


async def test_read_text_exact_shape_and_return(make_ctx, resp):
    data = {"text": "hello", "offset": 0, "limit": 40000, "total_chars": 5, "truncated": False}
    ctx = make_ctx([resp(200, {"data": data})])
    out = await extractor.read_text(ctx, 7, offset=0, limit=40000)
    assert out == data
    method, url, kwargs = ctx.http.calls[0]
    assert method == "get"
    assert url == f"{extractor._DOCUMENTS_URL}/7/text"
    p = kwargs["params"]
    assert p["source"] == "gdrive" and p["imperal_id"] == "user-123"
    assert p["offset"] == 0 and p["limit"] == 40000


async def test_read_text_404_raises_for_self_heal(make_ctx, resp):
    ctx = make_ctx([resp(404, {})])
    with pytest.raises(RuntimeError):
        await extractor.read_text(ctx, 7)
    assert len(ctx.http.calls) == 1  # 404 is a real answer — not retried


async def test_read_text_409_no_text_raises(make_ctx, resp):
    ctx = make_ctx([resp(409, {})])
    with pytest.raises(RuntimeError):
        await extractor.read_text(ctx, 7)


# ── search ────────────────────────────────────────────────────────────────────


async def test_search_exact_shape_and_hits(make_ctx, resp):
    hits = [{"document_id": 7, "filename": "f", "seq": 0, "text": "x", "score": 0.9}]
    ctx = make_ctx([resp(200, {"data": {"hits": hits, "count": 1}})])
    out = await extractor.search(ctx, "query text", k=6)
    assert out == hits
    method, url, kwargs = ctx.http.calls[0]
    assert method == "post" and url == extractor._SEARCH_URL
    j = kwargs["json"]
    assert j["source"] == "gdrive" and j["imperal_id"] == "user-123"
    assert j["query"] == "query text" and j["k"] == 6


async def test_search_empty_when_no_hits(make_ctx, resp):
    ctx = make_ctx([resp(200, {"data": {}})])
    assert await extractor.search(ctx, "q") == []


# ── overview ──────────────────────────────────────────────────────────────────


async def test_overview_exact_shape(make_ctx, resp):
    doc = {"document_id": 7, "preview": "abc", "status": "processed"}
    ctx = make_ctx([resp(200, {"data": doc})])
    out = await extractor.overview(ctx, 7)
    assert out == doc
    method, url, kwargs = ctx.http.calls[0]
    assert method == "get" and url == f"{extractor._DOCUMENTS_URL}/7"
    assert kwargs["params"]["source"] == "gdrive"


# ── delete ────────────────────────────────────────────────────────────────────


async def test_delete_true_on_200(make_ctx, resp):
    ctx = make_ctx([resp(200, {"data": {"deleted": True}})])
    assert await extractor.delete(ctx, 7) is True
    method, url, kwargs = ctx.http.calls[0]
    assert method == "delete" and url == f"{extractor._DOCUMENTS_URL}/7"
    assert kwargs["params"]["source"] == "gdrive"


async def test_delete_false_on_404(make_ctx, resp):
    ctx = make_ctx([resp(404, {})])
    assert await extractor.delete(ctx, 7) is False


async def test_delete_raises_on_other_4xx(make_ctx, resp):
    ctx = make_ctx([resp(422, {})])
    with pytest.raises(RuntimeError):
        await extractor.delete(ctx, 7)
    assert len(ctx.http.calls) == 1  # not retried


# ── transient retry (via _send, exercised through public ops) ─────────────────


async def test_transient_500_then_success_retries_once(make_ctx, resp):
    doc = {"document_id": 1, "status": "processed"}
    ctx = make_ctx([resp(500, {}), resp(200, {"data": {"documents": [doc]}})])
    out = await extractor.ingest(ctx, fetch_url="u", auth="t", content_key="c", filename="f")
    assert out == doc
    assert len(ctx.http.calls) == 2


async def test_network_error_then_success_retries_once(make_ctx, resp):
    ctx = make_ctx([ConnectionError("boom"), resp(200, {"data": {"hits": []}})])
    out = await extractor.search(ctx, "q")
    assert out == []
    assert len(ctx.http.calls) == 2


async def test_two_500s_raises_after_two_attempts(make_ctx, resp):
    ctx = make_ctx([resp(500, {}), resp(500, {})])
    with pytest.raises(RuntimeError):
        await extractor.search(ctx, "q")
    assert len(ctx.http.calls) == 2  # exactly two — no infinite retry


async def test_two_network_errors_raise(make_ctx):
    ctx = make_ctx([ConnectionError("a"), TimeoutError("b")])
    with pytest.raises(Exception):
        await extractor.search(ctx, "q")
    assert len(ctx.http.calls) == 2


async def test_4xx_not_retried(make_ctx, resp):
    ctx = make_ctx([resp(422, {})])
    with pytest.raises(RuntimeError):
        await extractor.search(ctx, "q")
    assert len(ctx.http.calls) == 1  # 4xx returned immediately, raise_for_status raises
