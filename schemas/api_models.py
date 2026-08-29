"""
schemas/api_models.py — Request & Response schemas for the 5 mandatory
endpoints: /healthz, /metadata, /context, /tick, /reply.

Field names are kept in exact lockstep with the published API contract —
the judge harness matches on these names verbatim.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

class ContextsLoadedCount(BaseModel):
    category: int = 0
    merchant: int = 0
    customer: int = 0
    trigger: int = 0


class HealthzResponse(BaseModel):
    status: Literal["ok"] = "ok"
    uptime_seconds: int
    contexts_loaded: ContextsLoadedCount

class MetadataResponse(BaseModel):
    team_name: str
    team_members: List[str]
    model: str
    approach: str
    contact_email: str
    version: str
    submitted_at: str

Scope = Literal["category", "merchant", "customer", "trigger"]


class ContextIngestRequest(BaseModel):
    scope: Scope
    context_id: str
    version: int
    delivered_at: Optional[str] = None
    payload: Dict[str, Any] = Field(default_factory=dict)


class ContextIngestAcceptedResponse(BaseModel):
    accepted: Literal[True] = True
    ack_id: str
    stored_at: str


class ContextIngestConflictResponse(BaseModel):
    accepted: Literal[False] = False
    reason: Literal["stale_version"] = "stale_version"
    current_version: int

class TickRequest(BaseModel):
    now: str
    available_triggers: List[str] = Field(default_factory=list)


SendAs = Literal["vera", "merchant_on_behalf"]
CtaType = Literal[
    "binary_yes_no",
    "binary_confirm_cancel",
    "multi_choice_slot",
    "open_ended",
    "none",
]


class Action(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: str
    merchant_id: str
    customer_id: Optional[str] = None
    send_as: SendAs
    trigger_id: str
    template_name: str
    template_params: List[str] = Field(default_factory=list)
    body: str
    cta: CtaType
    suppression_key: str
    rationale: str


class TickResponse(BaseModel):
    actions: List[Action] = Field(default_factory=list)


FromRole = Literal["merchant", "customer"]
ReplyAction = Literal["send", "wait", "end"]


class ReplyRequest(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: FromRole
    message: str
    received_at: str
    turn_number: int


class ReplyResponse(BaseModel):
    action: ReplyAction
    body: Optional[str] = None
    cta: Optional[CtaType] = None
    rationale: str
    wait_seconds: Optional[int] = None
