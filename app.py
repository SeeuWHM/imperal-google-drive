"""Google Drive — Extension instance + lifecycle (SDK 5.9.x, drive.file scope only).

Reads and edits Google Docs / Sheets / plain text files in Google Drive —
live, nothing stored on Imperal except file_id pointers. See
extensions/doc-reader.md in the SeeU-Extensions workspace for the full design.
"""
from __future__ import annotations

import logging

from imperal_sdk import Extension
from imperal_sdk.chat import ChatExtension
from imperal_sdk.secrets.spec import SecretSpec

from providers.extractor import DOC_EXTRACTOR_TOKEN_SECRET
from providers.helpers import _all_accounts

log = logging.getLogger("doc_reader")

# ── Extension + ChatExtension ─────────────────────────────────────────────────

ext = Extension(
    "google-drive-connector",
    version="0.4.0",
    display_name="Google Drive Connector",
    description=(
        "Read and edit Google Docs, Google Sheets, and Google Slides, and read/write "
        "plain text files, stored in the user's Google Drive. Nothing is stored on "
        "Imperal — content is fetched live and written straight back to the source."
    ),
    icon="google-drive-connector.svg",
    actions_explicit=True,
    capabilities=["store:read", "store:write", "secrets:read"],
)

chat = ChatExtension(
    ext=ext,
    tool_name="tool_doc_reader_chat",
    description=(
        "Google Drive — read and edit Google Docs/Sheets/plain-text files from the "
        "user's Google Drive. Connect once via connect_google_docs, pick files via "
        "the Google Picker, then read/search/edit them by file_id."
    ),
)

# ── OAuth — unified platform OAuth (gateway handles code exchange + storage) ──
# drive.file only: non-sensitive scope, no Google verification needed, and it
# already covers read AND write for Docs (documents.batchUpdate), Sheets
# (spreadsheets.values.update), and plain Drive files (files.update) — see
# extensions/doc-reader.md for the scope research.

# Provider key MUST be "google" — SDK 5.9.2 ctx.oauth_authorize_url() only knows
# google/microsoft/yahoo (hardcoded authorize endpoints) and reads the app secret
# {provider}_client_id, i.e. "google_client_id". A custom key like "google-docs"
# raises ValueError at runtime and looks up a non-existent secret. The callback
# route is /v1/ext/google-drive-connector/oauth/google/callback (register THIS
# as the redirect URI in the Google console — it derives from the app_id, which
# is google-drive-connector to match the Dev Portal profile; the git repo name
# imperal-google-drive is NOT the app_id). Same pattern as mail-client.
# openid + userinfo.email (added 2026-08-16, same fix already proven in
# gsc-connector/app.py for the identical symptom): drive.file alone does NOT
# hand Google's OAuth response an email address, so the platform gateway
# cannot identify/label the account on connect ("Couldn't identify the
# account... missing openid/email permission" — reproduced live). These two
# scopes are why gsc-connector's gateway callback CAN capture the address;
# doc-reader was missing them entirely until now. helpers.py's own
# _hydrate_missing_emails() (about.get, allowed under drive.file) remains as
# a second, independent path to fill in "unknown" for any already-connected
# account that predates this fix.
ext.oauth(
    "google",
    collection="docreader_accounts",
    scopes=[
        "https://www.googleapis.com/auth/drive.file",
        "openid",
        "https://www.googleapis.com/auth/userinfo.email",
    ],
)

# ── App-scope secrets (scope="app"): one shared Google OAuth Client for all
# users of this extension — must be created in Google Cloud Console before
# this extension can connect anyone (separate consent-screen identity from
# mail-client's own Google OAuth Client). Not yet provisioned — see
# extensions/doc-reader.md open questions.

_APP_SECRETS = [
    ("google_client_id", "Shared Google OAuth Client ID for Google Drive (developer-owned; one OAuth app for all users)", "IMPERAL_APPSECRET_DOCREADER_GOOGLE_CLIENT_ID"),
    ("google_client_secret", "Shared Google OAuth Client Secret for Google Drive (developer-owned)", "IMPERAL_APPSECRET_DOCREADER_GOOGLE_CLIENT_SECRET"),
    # Not a confidential secret by Google's own design (used client-side, restricted
    # by HTTP referrer in Google Cloud Console) — stored as a secret anyway for
    # consistent management, not because it needs to be kept hidden.
    ("google_picker_api_key", "Google API Key restricted to Picker API for the picker.html static page (HTTP referrer restricted)", "IMPERAL_APPSECRET_DOCREADER_GOOGLE_PICKER_API_KEY"),
    # Shared with doc-extractor-service (its PICKER_HMAC_SECRET env var) — signs
    # the short-lived OAuth token handoff so the Picker page never runs its own
    # separate Google login (confirmed empirically: a grant obtained that way is
    # invisible to this extension's server-side refresh token — different grant
    # lineage). Must match exactly on both sides.
    ("picker_hmac_secret", "HMAC key shared with doc-extractor-service for the Picker OAuth token handoff", "IMPERAL_APPSECRET_DOCREADER_PICKER_HMAC_SECRET"),
    # Bearer token for the doc-extractor-service engine's data endpoints
    # (/v1/documents, /v1/search) — REQUIRED since 2026-07-19, when that engine
    # turned on auth (before that date this extension worked with no header at
    # all, silently, because auth there was a no-op). This extension carries its
    # OWN token (server config key api_auth_token_gdrive), independent of
    # file-reader's — added 2026-08-16 after a full-repo audit found every
    # content call had been failing with a hard 401 in prod since that rollout.
    (DOC_EXTRACTOR_TOKEN_SECRET, "Bearer token for the doc-extractor-service engine (developer-owned; shared secret, not per-user)", "IMPERAL_APPSECRET_DOCREADER_DOC_EXTRACTOR_TOKEN"),
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
    log.info(f"google-drive-connector installed for user {uid}")
