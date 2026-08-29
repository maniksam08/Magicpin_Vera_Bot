"""
core/router.py — Dual-scope dispatch: B2B ("vera") vs B2C ("merchant_on_behalf").

Decides WHO Vera is speaking to and AS WHOM, and derives the addressing /
language conventions that core/composer.py must honor:

- scope == "merchant"  -> send_as = "vera"                (peer/advisor voice)
- scope == "customer"  -> send_as = "merchant_on_behalf"   (service voice, on the merchant's behalf)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from schemas.context_models import CustomerContext, MerchantContext


@dataclass
class RouteDecision:
    scope: str
    send_as: str
    merchant_id: Optional[str]
    customer_id: Optional[str]


def route_for_trigger(merchant_id: Optional[str], customer_id: Optional[str]) -> RouteDecision:
    """
    A trigger addressed to a specific customer_id is a B2C send on the
    merchant's behalf; otherwise it's a B2B send from Vera directly to
    the merchant.
    """
    if customer_id:
        return RouteDecision(
            scope="customer", send_as="merchant_on_behalf", merchant_id=merchant_id, customer_id=customer_id
        )
    return RouteDecision(scope="merchant", send_as="vera", merchant_id=merchant_id, customer_id=None)


def route_for_reply(from_role: str, merchant_id: Optional[str], customer_id: Optional[str]) -> RouteDecision:
    """A conversation reply continues in the voice matching who Vera is talking to."""
    if from_role == "customer":
        return RouteDecision(
            scope="customer", send_as="merchant_on_behalf", merchant_id=merchant_id, customer_id=customer_id
        )
    return RouteDecision(scope="merchant", send_as="vera", merchant_id=merchant_id, customer_id=customer_id)


@dataclass
class AddressingPlan:
    address_name: str
    language_mode: str
    use_namaste: bool = False
    address_via_family: bool = False


def merchant_addressing(merchant: Optional[MerchantContext]) -> AddressingPlan:
    """
    Merchant-facing (B2B) voice: peer operator / clinical peer / strategic
    advisor. Always addressed by first name.
    """
    if merchant is None:
        return AddressingPlan(address_name="there", language_mode="english")
    name = merchant.first_name or (merchant.name.split()[0] if merchant.name else "there")
    return AddressingPlan(address_name=name, language_mode="english")


def customer_addressing(customer: Optional[CustomerContext]) -> AddressingPlan:
    """
    Customer-facing (B2C, on behalf of merchant) voice: respectful,
    category-appropriate service tone, matched to the customer's stated
    language preference, and re-routed to a family contact for senior
    citizens when one is on file.
    """
    if customer is None:
        return AddressingPlan(address_name="there", language_mode="english")

    lang_pref = (customer.language_preference or "en").lower()
    if "hi-en" in lang_pref or "hinglish" in lang_pref:
        language_mode = "hinglish"
    elif lang_pref in ("hi", "hindi"):
        language_mode = "hindi"
    else:
        language_mode = "english"

    use_namaste = language_mode == "hindi"

    address_via_family = False
    address_name = customer.name or "there"
    if customer.is_senior_citizen and customer.relationship.family_member_contact:
        address_via_family = True
        address_name = customer.relationship.family_member_contact

    return AddressingPlan(
        address_name=address_name,
        language_mode=language_mode,
        use_namaste=use_namaste,
        address_via_family=address_via_family,
    )
