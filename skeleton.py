"""Google Drive · Skeleton — lightweight periodic status for the kernel's
intent classifier. Per SDK contract: flat scalars + short lists (<=5), <=6
top-level keys, <=1-2KB total, local store reads only (no network)."""
from __future__ import annotations

from app import ext
from providers.helpers import ACCOUNTS_COLLECTION, _all_picked_files


@ext.skeleton("doc_reader_files", ttl=300, description="Google Drive status — connected accounts, picked files, how many are indexed and ready, names, types")
async def skeleton_doc_reader_files(ctx) -> dict:
    # Keep FAST: local store only. (Do NOT call _all_accounts — it hydrates
    # emails via a live Drive call, too heavy for the classifier skeleton.)
    files = await _all_picked_files(ctx)  # across all connected accounts
    account_docs = await ctx.store.query(ACCOUNTS_COLLECTION)
    ready = sum(1 for f in files if f.get("status") == "ready")
    extensions = sorted({
        f["name"].rsplit(".", 1)[-1].lower()
        for f in files if "." in f.get("name", "")
    })
    return {"response": {
        "accounts_connected": sum(1 for _ in account_docs),
        "files_total": len(files),
        "files_ready": ready,
        "recent_files": [f.get("name", "?") for f in files[:5]],
        "types": extensions[:5],
    }}
