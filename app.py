"""Doc Reader — Extension instance + lifecycle (SDK 5.9.x, drive.file scope only).

Reads and edits Google Docs / Sheets / plain text files in Google Drive —
live, nothing stored on Imperal except file_id pointers. See
extensions/doc-reader.md in the SeeU-Extensions workspace for the full design.
"""
from __future__ import annotations

import logging

from imperal_sdk import Extension
from imperal_sdk.chat import ChatExtension
from imperal_sdk.secrets.spec import SecretSpec

from providers.helpers import _all_accounts

log = logging.getLogger("doc_reader")

# ── Extension + ChatExtension ─────────────────────────────────────────────────

ext = Extension(
    "doc-reader",
    version="0.1.0",
    display_name="Doc Reader",
    description=(
        "Read and edit Google Docs, Google Sheets, and plain text files stored "
        "in the user's Google Drive. Nothing is stored on Imperal — content is "
        "fetched live and written straight back to the source document."
    ),
    icon="doc-reader.svg",
    actions_explicit=True,
    capabilities=["store:read", "store:write", "secrets:read"],
)

chat = ChatExtension(
    ext=ext,
    tool_name="tool_doc_reader_chat",
    description=(
        "Doc Reader — read and edit Google Docs/Sheets/plain-text files from the "
        "user's Google Drive. Connect once via connect_google_docs, pick files via "
        "the Google Picker, then read/search/edit them by file_id."
    ),
)

# ── OAuth — unified platform OAuth (gateway handles code exchange + storage) ──
# drive.file only: non-sensitive scope, no Google verification needed, and it
# already covers read AND write for Docs (documents.batchUpdate), Sheets
# (spreadsheets.values.update), and plain Drive files (files.update) — see
# extensions/doc-reader.md for the scope research.

ext.oauth(
    "google-docs",
    collection="docreader_accounts",
    scopes=["https://www.googleapis.com/auth/drive.file"],
)

# ── App-scope secrets (scope="app"): one shared Google OAuth Client for all
# users of this extension — must be created in Google Cloud Console before
# this extension can connect anyone (separate consent-screen identity from
# mail-client's own Google OAuth Client). Not yet provisioned — see
# extensions/doc-reader.md open questions.

_APP_SECRETS = [
    ("google_client_id", "Shared Google OAuth Client ID for Doc Reader (developer-owned; one OAuth app for all users)", "IMPERAL_APPSECRET_DOCREADER_GOOGLE_CLIENT_ID"),
    ("google_client_secret", "Shared Google OAuth Client Secret for Doc Reader (developer-owned)", "IMPERAL_APPSECRET_DOCREADER_GOOGLE_CLIENT_SECRET"),
    # Not a confidential secret by Google's own design (used client-side, restricted
    # by HTTP referrer in Google Cloud Console) — stored as a secret anyway for
    # consistent management, not because it needs to be kept hidden.
    ("google_picker_api_key", "Google API Key restricted to Picker API for the picker.html static page (HTTP referrer restricted)", "IMPERAL_APPSECRET_DOCREADER_GOOGLE_PICKER_API_KEY"),
]
for _name, _desc, _fb in _APP_SECRETS:
    ext._secrets[_name] = SecretSpec(
        name=_name, description=_desc, scope="app", env_fallback=_fb, required=True,
    )

# ── Lifecycle ─────────────────────────────────────────────────────────────────


@ext.health_check
async def health(ctx) -> dict:
    accounts = await _all_accounts(ctx)
    return {"status": "ok", "version": ext.version, "accounts_connected": len(accounts)}


@ext.on_install
async def on_install(ctx):
    uid = ctx.user.imperal_id if ctx and hasattr(ctx, "user") and ctx.user else "system"
    log.info(f"doc-reader installed for user {uid}")
