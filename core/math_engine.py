"""
core/math_engine.py — Deterministic metric calculator (grounding & provenance).

Every number that ends up in a Vera message must be traceable back to a
function call in this file operating on stored context data. The LLM never
invents counts, durations, prices, or slots — it only phrases numbers this
module has already computed. If the underlying data isn't present, the
corresponding fact is simply omitted rather than guessed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        v = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _humanize_days(days: int) -> str:
    if days < 0:
        days = 0
    if days < 30:
        return f"{days} day{'s' if days != 1 else ''}"
    months = round(days / 30.44)
    if months < 12:
        return f"{months} month{'s' if months != 1 else ''}"
    years = round(days / 365.25, 1)
    return f"{years} year{'s' if years != 1 else ''}"


@dataclass
class GroundingFacts:
    """A bundle of deterministically-derived facts ready to hand to the composer."""

    affected_count: Optional[int] = None
    affected_segment_total: Optional[int] = None
    affected_segment_label: Optional[str] = None

    lapsed_days: Optional[int] = None
    lapsed_human: Optional[str] = None

    available_slots: List[str] = field(default_factory=list)

    price_label: Optional[str] = None
    price_value: Optional[Any] = None

    citable_facts: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "affected_count": self.affected_count,
            "affected_segment_total": self.affected_segment_total,
            "affected_segment_label": self.affected_segment_label,
            "lapsed_days": self.lapsed_days,
            "lapsed_human": self.lapsed_human,
            "available_slots": self.available_slots,
            "price_label": self.price_label,
            "price_value": self.price_value,
            "citable_facts": self.citable_facts,
        }


def calculate_affected_customer_count(
    merchant_customer_aggregate: Dict[str, Any], segment_key: str, total_key: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """
    Reads merchant.customer_aggregate for a named segment count (e.g.
    'chronic_rx_customers') and, if present, a matching total (e.g.
    'total_customers'). Returns None if the segment isn't present at all —
    callers must not fabricate a count.

    Example aggregate shape:
      {"chronic_rx_customers": 22, "total_customers": 240}
    -> {"count": 22, "total": 240, "label": "chronic_rx_customers"}
    """
    if not merchant_customer_aggregate:
        return None
    count = merchant_customer_aggregate.get(segment_key)
    if count is None:
        return None
    total = None
    if total_key:
        total = merchant_customer_aggregate.get(total_key)
    else:
        for candidate in ("total_customers", "total", "all_customers"):
            if candidate in merchant_customer_aggregate:
                total = merchant_customer_aggregate[candidate]
                break
    return {"count": count, "total": total, "label": segment_key}


def calculate_lapsed_duration(last_visit_iso: Optional[str], now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """
    Computes the delta between `now` and customer.relationship.last_visit.
    Returns None if last_visit is missing/unparseable — never guesses.
    """
    last_visit = _parse_dt(last_visit_iso)
    if last_visit is None:
        return None
    now = now or datetime.now(timezone.utc)
    delta_days = (now - last_visit).days
    return {"days": delta_days, "human": _humanize_days(delta_days)}


def get_available_slots(trigger_payload: Dict[str, Any]) -> List[str]:
    """Pulls slots directly from trigger.payload.available_slots. Never invents times."""
    if not trigger_payload:
        return []
    slots = trigger_payload.get("available_slots", [])
    return list(slots) if isinstance(slots, list) else []


def get_price(
    merchant_offers: Dict[str, Any], category_offer_catalog: Dict[str, Any], offer_key: str
) -> Optional[Dict[str, Any]]:
    """
    Prefers a merchant-specific override in merchant.offers, falling back to
    the category-wide offer_catalog. Returns None (never a guessed price)
    if the key isn't found in either.
    """
    if merchant_offers and offer_key in merchant_offers:
        return {"value": merchant_offers[offer_key], "source": "merchant"}
    if category_offer_catalog and offer_key in category_offer_catalog:
        return {"value": category_offer_catalog[offer_key], "source": "category"}
    return None


def build_grounding_facts(
    *,
    merchant_customer_aggregate: Optional[Dict[str, Any]] = None,
    segment_key: Optional[str] = None,
    total_key: Optional[str] = None,
    last_visit_iso: Optional[str] = None,
    trigger_payload: Optional[Dict[str, Any]] = None,
    merchant_offers: Optional[Dict[str, Any]] = None,
    category_offer_catalog: Optional[Dict[str, Any]] = None,
    offer_key: Optional[str] = None,
) -> GroundingFacts:
    """
    Convenience aggregator: runs whichever of the above calculations have
    enough input data, and assembles a single GroundingFacts bundle plus
    human-readable citable_facts strings the composer can drop straight
    into a prompt (and that guardrails can later cross-check against the
    generated body for hallucination).
    """
    facts = GroundingFacts()

    if merchant_customer_aggregate and segment_key:
        seg = calculate_affected_customer_count(merchant_customer_aggregate, segment_key, total_key)
        if seg:
            facts.affected_count = seg["count"]
            facts.affected_segment_total = seg["total"]
            facts.affected_segment_label = seg["label"]
            if seg["total"] is not None:
                facts.citable_facts.append(f"{seg['count']} of your {seg['total']} {seg['label']}")
            else:
                facts.citable_facts.append(f"{seg['count']} {seg['label']}")

    if last_visit_iso:
        lapsed = calculate_lapsed_duration(last_visit_iso)
        if lapsed:
            facts.lapsed_days = lapsed["days"]
            facts.lapsed_human = lapsed["human"]
            facts.citable_facts.append(f"{lapsed['human']} since last visit")

    if trigger_payload:
        slots = get_available_slots(trigger_payload)
        if slots:
            facts.available_slots = slots
            facts.citable_facts.append(f"available slots: {', '.join(slots)}")

    if offer_key:
        price = get_price(merchant_offers or {}, category_offer_catalog or {}, offer_key)
        if price:
            facts.price_label = offer_key
            facts.price_value = price["value"]
            facts.citable_facts.append(f"{offer_key}: {price['value']}")

    return facts
