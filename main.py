"""Google Drive Connector — unified toolset for Imperal Cloud (SDK 5.9.x / SDL).

CONTENT plane (read/understand any file via the doc-extractor engine cache):
    read_file · search_files · file_overview
ACTION plane (native Google API writes + exact compute):
    edit_document · edit_spreadsheet · spreadsheet_compute · write_text_file
Files/accounts/folders: list_files · open_folder · connect · picker · switch/disconnect.
Background: index_files (+ auto-kicked at pick time).
"""
from __future__ import annotations

import os
import sys

# ── Module purge (hot-reload + cross-extension sys.modules safety) ───────────
_dir = os.path.dirname(os.path.abspath(__file__))
if _dir not in sys.path:
    sys.path.insert(0, _dir)

_MODULES = (
    "app", "schemas", "schemas_sdl", "cache_models",
    "providers.helpers", "providers.token_refresh", "providers.google_api",
    "providers.text_windows", "providers.spreadsheet_math",
    "providers.extractor", "providers.file_types", "providers.lifecycle",
    "providers.content_ops", "providers.edit_ops",
    "handlers_connect", "handlers_accounts", "handlers_content",
    "handlers_edit", "handlers_index", "handlers_debug", "skeleton", "panels",
)
for _m in [k for k in sys.modules if k in _MODULES]:
    del sys.modules[_m]

# ── Import core + submodules ─────────────────────────────────────────────────
from app import ext, chat  # noqa: E402, F401

import schemas_sdl  # noqa: E402, F401

# Register cache models BEFORE modules that use ctx.cache.
import cache_models  # noqa: E402, F401

import handlers_connect   # noqa: E402, F401
import handlers_accounts  # noqa: E402, F401
import handlers_content   # noqa: E402, F401
import handlers_edit      # noqa: E402, F401
import handlers_index     # noqa: E402, F401
import handlers_debug     # noqa: E402, F401  (TEMPORARY folder-access probe)
import skeleton           # noqa: E402, F401
import panels             # noqa: E402, F401
