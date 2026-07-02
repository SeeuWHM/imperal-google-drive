"""Doc Reader · Skeleton — lightweight periodic status for the kernel's
intent classifier. Per SDK contract: flat scalars + short lists (<=5),
<=6 top-level keys, <=1-2KB total. No file content, ever."""
from __future__ import annotations

from app import ext
from providers.helpers import ACCOUNTS_COLLECTION, _all_picked_files


@ext.skeleton("doc_reader_files", ttl=300, description="Doc Reader status — connected Google accounts, total picked files, names, sizes, extensions")
async def skeleton_doc_reader_files(ctx) -> dict:
    # Keep this FAST: local store reads only, no network. (Do NOT call
    # _all_accounts here — it hydrates account emails via a live Drive call,
    # which is far too heavy for a skeleton and can stall the classifier.)
    files = await _all_picked_files(ctx)  # across all connected accounts
    account_docs = await ctx.store.query(ACCOUNTS_COLLECTION)
    accounts_connected = sum(1 for _ in account_docs)
    extensions = sorted({
        f["name"].rsplit(".", 1)[-1].lower()
        for f in files if "." in f.get("name", "")
    })
    return {"response": {
        "accounts_connected": accounts_connected,
        "connected_count": len(files),
        "recent_files": [f.get("name", "?") for f in files[:5]],
        "extensions": extensions[:5],
        "total_size_bytes": sum(int(f.get("size_bytes") or 0) for f in files),
    }}
