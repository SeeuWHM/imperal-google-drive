"""ctx.cache value shapes — must be registered before any handler uses them."""
from __future__ import annotations

from pydantic import BaseModel

from app import ext


class PendingPickerSession(BaseModel):
    token: str


ext.cache_model("pending_picker_session")(PendingPickerSession)
