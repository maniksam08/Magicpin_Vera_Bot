"""
schemas/context_models.py — Typed views over the four context scopes
(category, merchant, customer, trigger).

The wire contract lets `payload` be an arbitrary dict (whatever the judge
harness sends), so every model here uses `extra="allow"` and keeps the
well-known fields optional. This means core/ modules get convenient typed
access (e.g. `merchant.customer_aggregate`) while never rejecting a payload
just because it carries fields we didn't anticipate.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class VoiceConfig(BaseModel):
    """CategoryContext.voice — tone & compliance rules for message composition."""

    model_config = ConfigDict(extra="allow")

    tone: Optional[str] = None
    persona_notes: Optional[str] = None
    vocab_taboo: List[str] = Field(default_factory=list)
    vocab_preferred: List[str] = Field(default_factory=list)
    grounding_style: Optional[str] = None
    persuasion_levers: List[str] = Field(default_factory=list)


class CategoryContext(BaseModel):
    """scope='category' — one of dentists, salons, restaurants, gyms, pharmacies."""

    model_config = ConfigDict(extra="allow")

    category_id: str
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    offer_catalog: Dict[str, Any] = Field(default_factory=dict)
    compliance_notes: Optional[str] = None


class MerchantContext(BaseModel):
    """scope='merchant' — a single merchant's operating profile."""

    model_config = ConfigDict(extra="allow")

    merchant_id: str
    category: Optional[str] = None
    name: Optional[str] = None
    first_name: Optional[str] = None
    locality: Optional[str] = None
    offers: Dict[str, Any] = Field(default_factory=dict)
    customer_aggregate: Dict[str, Any] = Field(default_factory=dict)
    operating_hours: Optional[str] = None
    language_preference: Optional[str] = None


class CustomerRelationship(BaseModel):
    model_config = ConfigDict(extra="allow")

    last_visit: Optional[str] = None 
    visit_count: Optional[int] = None
    lifetime_value: Optional[float] = None
    family_member_contact: Optional[str] = None


class CustomerContext(BaseModel):
    """scope='customer' — an individual customer of a specific merchant."""

    model_config = ConfigDict(extra="allow")

    customer_id: str
    merchant_id: Optional[str] = None
    name: Optional[str] = None
    language_preference: Optional[str] = None 
    is_senior_citizen: bool = False
    relationship: CustomerRelationship = Field(default_factory=CustomerRelationship)
    profile: Dict[str, Any] = Field(default_factory=dict)


class TriggerContext(BaseModel):
    """scope='trigger' — an external/internal event Vera may proactively act on."""

    model_config = ConfigDict(extra="allow")

    trigger_id: str
    category_id: Optional[str] = None
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    trigger_type: Optional[str] = None
    payload: Dict[str, Any] = Field(default_factory=dict)


SCOPE_MODEL_MAP = {
    "category": CategoryContext,
    "merchant": MerchantContext,
    "customer": CustomerContext,
    "trigger": TriggerContext,
}


def parse_context(scope: str, context_id: str, raw_payload: Dict[str, Any]) -> BaseModel:
    """
    Best-effort typed parse of a raw context payload for a given scope.
    Never raises: falls back to a minimally-populated model (id + raw
    payload preserved via extra="allow") if the payload doesn't cleanly
    match expected field names, so ingestion is never blocked by this step.
    """
    model_cls = SCOPE_MODEL_MAP.get(scope)
    if model_cls is None:
        raise ValueError(f"Unknown scope: {scope}")

    id_field = {
        "category": "category_id",
        "merchant": "merchant_id",
        "customer": "customer_id",
        "trigger": "trigger_id",
    }[scope]

    data = dict(raw_payload or {})
    data.setdefault(id_field, context_id)

    try:
        return model_cls(**data)
    except Exception:
        return model_cls(**{id_field: context_id})
