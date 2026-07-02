"""Doc Reader · Skeleton — lightweight periodic status for the kernel's
intent classifier. Per SDK contract: flat scalars + short lists (<=5),
<=6 top-level keys, <=1-2KB total. No file content, ever."""
from __future__ import annotations

from app import ext
from providers.helpers import _all_accounts, _all_picked_files


@ext.skeleton("doc_reader_files", ttl=300, description="Doc Reader status — connected Google accounts, total picked files, names, sizes, extensions")
async def skeleton_doc_reader_files(ctx) -> dict:
    files = await _all_picked_files(ctx)  # across all connected accounts
    accounts = await _all_accounts(ctx)
    extensions = sorted({
        f["name"].rsplit(".", 1)[-1].lower()
        for f in files if "." in f.get("name", "")
    })
    return {"response": {
        "accounts_connected": len(accounts),
        "connected_count": len(files),
        "recent_files": [f.get("name", "?") for f in files[:5]],
        "extensions": extensions[:5],
        "total_size_bytes": sum(int(f.get("size_bytes") or 0) for f in files),
    }}
