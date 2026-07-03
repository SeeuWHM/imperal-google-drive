"""TEMPORARY diagnostic — probe what drive.file can actually see for a picked
folder. Lets us decide empirically: is folder browsing fixable under drive.file,
or does it need drive.readonly? Read-only. Remove once the question is settled.
"""
from __future__ import annotations

import json
import logging

from imperal_sdk.chat.action_result import ActionResult

from app import chat
from providers.google_api import drive_folder_probe
from providers.helpers import _active_account
from schemas import FolderParams
from schemas_sdl import FileOverview, build_file_overview

log = logging.getLogger("doc_reader")


@chat.function(
    "debug_folder", action_type="read", data_model=FileOverview,
    description="Diagnostic: probe what the drive.file grant can actually see inside a folder (multiple strategies + folder capabilities). Temporary tool for debugging folder access.",
)
async def fn_debug_folder(ctx, params: FolderParams) -> ActionResult:
    try:
        acc = await _active_account(ctx)
        probe = await drive_folder_probe(ctx, acc, params.folder_id)
        blob = json.dumps(probe, ensure_ascii=False)
        return ActionResult.success(
            data=build_file_overview({"file_id": params.folder_id, "name": "folder-probe",
                                      "preview": blob[:1800], "status": "debug"}),
            summary=blob[:1800],
        )
    except Exception as e:
        return ActionResult.error(str(e), retryable=False)
