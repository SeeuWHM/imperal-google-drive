"""Doc Reader v0.1.0 — Google Docs/Sheets/Drive text files for Imperal Cloud (SDK 5.9.x / SDL)."""
from __future__ import annotations

import os
import sys

# ── Module purge (hot-reload + cross-extension sys.modules safety) ───────────
_dir = os.path.dirname(os.path.abspath(__file__))
if _dir not in sys.path:
    sys.path.insert(0, _dir)

_MODULES = (
    "app", "schemas", "schemas_sdl",
    "providers.helpers", "providers.token_refresh", "providers.google_api", "providers.text_windows",
    "handlers_connect", "handlers_text_files", "handlers_docs", "handlers_sheets",
    "skeleton", "panels",
)
for _m in [k for k in sys.modules if k in _MODULES]:
    del sys.modules[_m]

# ── Import core + submodules ─────────────────────────────────────────────────
from app import ext, chat  # noqa: E402, F401

import schemas_sdl  # noqa: E402, F401

import handlers_connect      # noqa: E402, F401
import handlers_text_files   # noqa: E402, F401
import handlers_docs         # noqa: E402, F401
import handlers_sheets       # noqa: E402, F401
import skeleton              # noqa: E402, F401
import panels                # noqa: E402, F401
