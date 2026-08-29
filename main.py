"""
main.py — FastAPI application exposing the 5 mandatory Vera endpoints:
GET /v1/healthz, GET /v1/metadata, POST /v1/context, POST /v1/tick, POST /v1/reply.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from config import settings
from core import guardrails
from core.auto_reply_detector import classify as classify_reply, texts_identical
from core.composer import compose_proactive_message, compose_reply_message
from core.math_engine import build_grounding_facts
from core.router import (
    customer_addressing,
    merchant_addressing,
    route_for_reply,
    route_for_trigger,
)
from schemas.api_models import (
    Action,
    ContextIngestRequest,
    ContextsLoadedCount,
    HealthzResponse,
    MetadataResponse,
    ReplyRequest,
    ReplyResponse,
    TickRequest,
    TickResponse,
)
from store.state_store import store

app = FastAPI(title="Vera — magicpin AI Proactive Engine", version=settings.APP_VERSION)



@app.get("/v1/healthz", response_model=HealthzResponse)
def healthz():
    counts = store.counts()
    return HealthzResponse(
        status="ok",
        uptime_seconds=store.uptime_seconds(),
        contexts_loaded=ContextsLoadedCount(**counts),
    )


@app.get("/v1/metadata", response_model=MetadataResponse)
def metadata():
    return MetadataResponse(
        team_name=settings.TEAM_NAME,
        team_members=settings.TEAM_MEMBERS,
        model=settings.LLM_MODEL,
        approach=settings.APPROACH_SUMMARY,
        contact_email=settings.CONTACT_EMAIL,
        version=settings.APP_VERSION,
        submitted_at=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    )



@app.post("/v1/context")
def ingest_context(req: ContextIngestRequest):
    result = store.ingest_context(
        scope=req.scope,
        context_id=req.context_id,
        version=req.version,
        delivered_at=req.delivered_at,
        payload=req.payload,
    )
    if not result["accepted"]:
        return JSONResponse(
            status_code=409,
            content={
                "accepted": False,
                "reason": result["reason"],
                "current_version": result["current_version"],
            },
        )
    return JSONResponse(
        status_code=200,
        content={
            "accepted": True,
            "ack_id": result["ack_id"],
            "stored_at": result["stored_at"],
        },
    )


def _resolve_tick_targets(trigger_ctx):
    """Returns a list of (merchant_id, customer_id) pairs a trigger fans out to."""
    if trigger_ctx.customer_id:
        return [(trigger_ctx.merchant_id, trigger_ctx.customer_id)]
    if trigger_ctx.merchant_id:
        return [(trigger_ctx.merchant_id, None)]
    if trigger_ctx.category_id:
        recs = store.find_merchants_by_category(trigger_ctx.category_id)
        return [(rec.parsed.merchant_id, None) for rec in recs]
    return []


def _default_suppression_key(trigger_ctx, merchant_id: Optional[str]) -> str:
    explicit = (trigger_ctx.payload or {}).get("suppression_key")
    if explicit:
        return f"{explicit}:{merchant_id}" if merchant_id and trigger_ctx.merchant_id is None else explicit
    scope_bit = trigger_ctx.category_id or merchant_id or "global"
    return f"{trigger_ctx.trigger_type or 'trigger'}:{scope_bit}:{trigger_ctx.trigger_id}"


@app.post("/v1/tick", response_model=TickResponse)
def tick(req: TickRequest):
    actions: List[Action] = []

    for trigger_id in req.available_triggers:
        trigger_rec = store.get_context("trigger", trigger_id)
        if trigger_rec is None:
            continue  
        trigger_ctx = trigger_rec.parsed

        targets = _resolve_tick_targets(trigger_ctx)
        for merchant_id, customer_id in targets:
            if merchant_id is None:
                continue

            suppression_key = _default_suppression_key(trigger_ctx, merchant_id)
            if store.is_suppressed(suppression_key):
                continue
            if store.is_suppressed(f"optout:{merchant_id}"):
                continue

            merchant_rec = store.get_context("merchant", merchant_id)
            merchant_ctx = merchant_rec.parsed if merchant_rec else None
            category_id = (merchant_ctx.category if merchant_ctx else None) or trigger_ctx.category_id
            category_rec = store.get_context("category", category_id) if category_id else None
            category_ctx = category_rec.parsed if category_rec else None

            customer_ctx = None
            if customer_id:
                customer_rec = store.get_context("customer", customer_id)
                customer_ctx = customer_rec.parsed if customer_rec else None

            route = route_for_trigger(merchant_id, customer_id)
            addressing = (
                customer_addressing(customer_ctx) if route.scope == "customer" else merchant_addressing(merchant_ctx)
            )

            facts = build_grounding_facts(
                merchant_customer_aggregate=merchant_ctx.customer_aggregate if merchant_ctx else None,
                segment_key=(trigger_ctx.payload or {}).get("segment_key"),
                total_key=(trigger_ctx.payload or {}).get("segment_total_key"),
                last_visit_iso=customer_ctx.relationship.last_visit if customer_ctx else None,
                trigger_payload=trigger_ctx.payload,
                merchant_offers=merchant_ctx.offers if merchant_ctx else None,
                category_offer_catalog=category_ctx.offer_catalog if category_ctx else None,
                offer_key=(trigger_ctx.payload or {}).get("offer_key"),
            )

            conversation_id = f"conv_{merchant_id}_{trigger_ctx.trigger_id}"
            convo = store.get_or_create_conversation(conversation_id, merchant_id=merchant_id, customer_id=customer_id)

            composed = compose_proactive_message(
                category_ctx=category_ctx,
                merchant_ctx=merchant_ctx,
                customer_ctx=customer_ctx,
                trigger_ctx=trigger_ctx,
                route=route,
                addressing=addressing,
                facts=facts,
                previous_bodies=convo.sent_bodies,
            )

            action = Action(
                conversation_id=conversation_id,
                merchant_id=merchant_id,
                customer_id=customer_id,
                send_as=route.send_as,
                trigger_id=trigger_id,
                template_name=composed.template_name,
                template_params=composed.template_params,
                body=composed.body,
                cta=composed.cta,
                suppression_key=suppression_key,
                rationale=composed.rationale,
            )
            actions.append(action)

            convo.sent_bodies.append(composed.body)
            convo.status = "qualifying"
            store.save_conversation(convo)
            store.suppress(suppression_key, until=None) 

    return TickResponse(actions=actions)


_COMMIT_RE = re.compile(
    r"\b(ok(ay)?,?\s*let'?s do (it|this)|confirm(ed)?|go ahead|sounds good,?\s*(do it|proceed)|"
    r"yes,?\s*(please\s*)?(do it|proceed|go ahead)?|let'?s (go|proceed)|sure,?\s*go ahead)\b",
    re.IGNORECASE,
)


def _is_commit_message(text: str) -> bool:
    return bool(_COMMIT_RE.search(text or ""))


@app.post("/v1/reply", response_model=ReplyResponse)
def reply(req: ReplyRequest):
    convo = store.get_or_create_conversation(
        req.conversation_id, merchant_id=req.merchant_id, customer_id=req.customer_id
    )
    merchant_id = convo.merchant_id or req.merchant_id
    customer_id = convo.customer_id or req.customer_id

    classification = classify_reply(req.message)

    if classification == "opt_out":
        convo.turns.append(
            _turn(req, is_canned=False)
        )
        convo.status = "ended"
        store.save_conversation(convo)
        if merchant_id:
            store.suppress_for_days(f"optout:{merchant_id}", settings.OPT_OUT_SUPPRESSION_DAYS)
        return ReplyResponse(
            action="end",
            body=None,
            cta=None,
            rationale="Explicit opt-out / hostility detected. Ending conversation and suppressing this "
            f"merchant for {settings.OPT_OUT_SUPPRESSION_DAYS} days.",
            wait_seconds=None,
        )

    if classification == "canned":
        convo.turns.append(_turn(req, is_canned=True))

        if req.turn_number >= 4:
            convo.status = "ended"
            store.save_conversation(convo)
            return ReplyResponse(
                action="end",
                body=None,
                cta=None,
                rationale="Auto-reply loop persisted past turn 4 with no human response. Closing conversation.",
                wait_seconds=None,
            )

        if req.turn_number == 3 and texts_identical(req.message, convo.last_canned_text):
            convo.status = "waiting"
            store.save_conversation(convo)
            return ReplyResponse(
                action="wait",
                body=None,
                cta=None,
                rationale="Identical canned auto-reply seen again at turn 3 — backing off instead of spamming.",
                wait_seconds=settings.CANNED_WAIT_SECONDS,
            )

        convo.last_canned_text = req.message
        convo.canned_streak += 1
        route = route_for_reply(req.from_role, merchant_id, customer_id)
        addressing = _addressing_for(route, merchant_id, customer_id)
        composed = compose_reply_message(
            stage="canned_followup",
            category_ctx=_category_ctx_for(merchant_id),
            merchant_ctx=_merchant_ctx(merchant_id),
            customer_ctx=_customer_ctx(customer_id),
            route=route,
            addressing=addressing,
            facts=build_grounding_facts(),
            incoming_message=req.message,
            previous_bodies=convo.sent_bodies,
        )
        convo.sent_bodies.append(composed.body)
        store.save_conversation(convo)
        return ReplyResponse(
            action="send",
            body=composed.body,
            cta=composed.cta,  
            rationale=composed.rationale,
            wait_seconds=None,
        )

    convo.turns.append(_turn(req, is_canned=False))
    convo.canned_streak = 0

    route = route_for_reply(req.from_role, merchant_id, customer_id)
    addressing = _addressing_for(route, merchant_id, customer_id)
    merchant_ctx = _merchant_ctx(merchant_id)
    customer_ctx = _customer_ctx(customer_id)
    category_ctx = _category_ctx_for(merchant_id)

    facts = build_grounding_facts(
        merchant_customer_aggregate=merchant_ctx.customer_aggregate if merchant_ctx else None,
        last_visit_iso=customer_ctx.relationship.last_visit if customer_ctx else None,
        merchant_offers=merchant_ctx.offers if merchant_ctx else None,
        category_offer_catalog=category_ctx.offer_catalog if category_ctx else None,
    )

    if convo.status != "executing" and _is_commit_message(req.message):
        stage = "intent_transition"
        convo.status = "executing"
    elif convo.status == "executing" and _is_commit_message(req.message):
        stage = "closure"
        convo.status = "ended"
    else:
        stage = "qualifying_followup"

    composed = compose_reply_message(
        stage=stage,
        category_ctx=category_ctx,
        merchant_ctx=merchant_ctx,
        customer_ctx=customer_ctx,
        route=route,
        addressing=addressing,
        facts=facts,
        incoming_message=req.message,
        previous_bodies=convo.sent_bodies,
    )
    convo.sent_bodies.append(composed.body)
    store.save_conversation(convo)

    return ReplyResponse(
        action="send",
        body=composed.body,
        cta=composed.cta, 
        rationale=composed.rationale,
        wait_seconds=None,
    )


def _turn(req: ReplyRequest, is_canned: bool):
    from store.state_store import TurnRecord

    return TurnRecord(
        turn_number=req.turn_number,
        from_role=req.from_role,
        message=req.message,
        received_at=req.received_at,
        is_canned=is_canned,
    )


def _merchant_ctx(merchant_id: Optional[str]):
    if not merchant_id:
        return None
    rec = store.get_context("merchant", merchant_id)
    return rec.parsed if rec else None


def _customer_ctx(customer_id: Optional[str]):
    if not customer_id:
        return None
    rec = store.get_context("customer", customer_id)
    return rec.parsed if rec else None


def _category_ctx_for(merchant_id: Optional[str]):
    merchant_ctx = _merchant_ctx(merchant_id)
    category_id = merchant_ctx.category if merchant_ctx else None
    if not category_id:
        return None
    rec = store.get_context("category", category_id)
    return rec.parsed if rec else None


def _addressing_for(route, merchant_id: Optional[str], customer_id: Optional[str]):
    if route.scope == "customer":
        return customer_addressing(_customer_ctx(customer_id))
    return merchant_addressing(_merchant_ctx(merchant_id))


@app.exception_handler(Exception)
async def unhandled_exception_handler(request, exc):
    return JSONResponse(status_code=500, content={"error": "internal_error", "detail": str(exc)})
