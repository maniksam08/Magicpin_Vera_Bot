"""
core/composer.py — LLM prompt assembler: category voice + Cialdini
persuasion levers + deterministic grounding facts -> a WhatsApp message.

Two public entry points:
  - compose_proactive_message(): used by /v1/tick to turn a trigger into
    an outbound nudge.
  - compose_reply_message(): used by /v1/reply to continue a live
    conversation (follow-ups, negotiation, closure, execution handoff).

Both ALWAYS run their LLM (or mock-template) output through
core/guardrails.lint_message() before returning, and both degrade to a
fully deterministic template path if no LLM is configured — the service
never fails to produce a compliant message.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from config import settings
from core import guardrails
from core.math_engine import GroundingFacts
from core.router import AddressingPlan, RouteDecision
from schemas.context_models import CategoryContext, CustomerContext, MerchantContext, TriggerContext

CATEGORY_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "dentists": {
        "tone": "Clinical peer — sharp, evidence-led, never salesy.",
        "vocab_taboo": ["guaranteed", "100% safe", "cure", "risk-free", "painless"],
        "grounding_style": "Cite medical journals (e.g. JIDA) or DCI regulations when referencing research.",
        "persuasion_levers": ["authority", "social_proof"],
        "opener_bank": [
            "Dr. {name}, quick clinical flag —",
            "Dr. {name}, sharing something relevant to your patient panel —",
        ],
    },
    "pharmacies": {
        "tone": "Trustworthy, precise, fully compliant.",
        "vocab_taboo": ["guaranteed cure", "miracle", "no side effects", "instant relief"],
        "grounding_style": "Cite exact batch numbers, recall notices, and exact molecule names.",
        "persuasion_levers": ["authority", "scarcity"],
        "opener_bank": [
            "{name}, an important update for your store —",
            "{name}, flagging this before your next order —",
        ],
    },
    "gyms": {
        "tone": "Evidence-based coach, zero-shame win-back framing.",
        "vocab_taboo": ["lazy", "failed", "shameful", "guilt"],
        "grounding_style": "Reframe seasonal dips as industry-wide acquisition lulls; emphasize retention.",
        "persuasion_levers": ["social_proof", "commitment_consistency", "unity"],
        "opener_bank": [
            "{name}, member-retention flag for this week —",
            "{name}, quick win-back opportunity —",
        ],
    },
    "salons": {
        "tone": "Warm operator, stylist-specific personalization.",
        "vocab_taboo": ["desperate", "cheap", "last chance ever"],
        "grounding_style": "Reference specific dates (e.g. bridal countdowns) and low-friction asks.",
        "persuasion_levers": ["liking", "reciprocity", "scarcity"],
        "opener_bank": [
            "{name}, quick personal note —",
            "{name}, thought of one of your regulars —",
        ],
    },
    "restaurants": {
        "tone": "High-energy operator.",
        "vocab_taboo": ["guaranteed", "best in the city", "never fails"],
        "grounding_style": "Distinguish dine-in vs delivery shifts explicitly.",
        "persuasion_levers": ["scarcity", "social_proof"],
        "opener_bank": [
            "{name}, footfall flag for tonight —",
            "{name}, quick shift call —",
        ],
    },
}

CIALDINI_PHRASES = {
    "authority": "grounded in {source}",
    "scarcity": "only {n} slots left today",
    "social_proof": "other {category} merchants nearby are already seeing this",
    "reciprocity": "I've already drafted this for you — just needs your go-ahead",
    "commitment_consistency": "you flagged this as a priority last month",
    "unity": "let's fix this together, this week",
    "liking": "I know your regulars well, and this one felt right for them",
}


@dataclass
class ComposedMessage:
    body: str
    cta: str
    rationale: str
    template_name: str = "vera_generic_v1"
    template_params: Optional[List[str]] = None

    def __post_init__(self):
        if self.template_params is None:
            self.template_params = []


def _get_category_config(category_ctx: Optional[CategoryContext], category_id: Optional[str]) -> Dict[str, Any]:
    defaults = CATEGORY_DEFAULTS.get(category_id or "", CATEGORY_DEFAULTS["restaurants"])
    if category_ctx is None:
        return defaults
    voice = category_ctx.voice
    return {
        "tone": voice.tone or defaults["tone"],
        "vocab_taboo": voice.vocab_taboo or defaults["vocab_taboo"],
        "grounding_style": voice.grounding_style or defaults["grounding_style"],
        "persuasion_levers": voice.persuasion_levers or defaults["persuasion_levers"],
        "opener_bank": defaults["opener_bank"],
    }


def _get_chat_model():
    provider = (settings.LLM_PROVIDER or "mock").lower()
    
    if provider == "mock" or not settings.LLM_API_KEY:
        return None
        
    try:
            
        if provider == "groq":
            from langchain_openai import ChatOpenAI
            
            return ChatOpenAI(
                model=settings.LLM_MODEL,
                temperature=settings.LLM_TEMPERATURE,
                api_key=settings.LLM_API_KEY,
                base_url="https://api.groq.com/openai/v1",
                timeout=settings.LLM_TIMEOUT_SECONDS,
            )
            
    except Exception:
        return None
        
    return None


def _llm_generate(system_prompt: str, user_prompt: str, fallback: str) -> str:
    model = _get_chat_model()
    if model is None:
        return fallback
        
    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        resp = model.invoke([SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)])
        text = getattr(resp, "content", None)
        if isinstance(text, str) and text.strip():
            return text.strip()
        return fallback
    except Exception:
        return fallback

def _system_prompt(cfg: Dict[str, Any], addressing: AddressingPlan, send_as: str) -> str:
    voice_line = "You are Vera, magicpin's AI business partner." if send_as == "vera" else (
        "You are Vera, writing WhatsApp messages on behalf of the merchant, directly to their customer."
    )
    lang_note = {
        "hinglish": "Write in natural, conversational Hinglish.",
        "hindi": "Write in respectful Hindi, opening with Namaste.",
        "english": "Write in clear, warm English.",
    }[addressing.language_mode]
    return (
        f"{voice_line} Category voice: {cfg['tone']} {lang_note}\n"
        f"Grounding rule: {cfg['grounding_style']} Never state a number, date, price, or slot that "
        f"was not explicitly given to you in the facts below — omit it instead of guessing.\n"
        f"NEVER use these words: {', '.join(cfg['vocab_taboo'])}.\n"
        f"Keep the message under 4 sentences, WhatsApp-length. End with one clear call to action.\n"
        f"Apply persuasive techniques from: {', '.join(cfg['persuasion_levers'])} — but only where the "
        f"facts genuinely support them; never fabricate urgency or social proof that isn't grounded."
    )


def _fallback_proactive_body(
    cfg: Dict[str, Any],
    addressing: AddressingPlan,
    trigger: Optional[TriggerContext],
    facts: GroundingFacts,
) -> str:
    opener = cfg["opener_bank"][0].format(name=addressing.address_name)
    fact_bits = "; ".join(facts.citable_facts[:2]) if facts.citable_facts else None
    lever = cfg["persuasion_levers"][0] if cfg["persuasion_levers"] else "authority"
    lever_phrase = CIALDINI_PHRASES.get(lever, "").format(
        source="the latest data", n=len(facts.available_slots) or 3, category=trigger.category_id if trigger else "local"
    )
    body = f"{opener} "
    if fact_bits:
        body += f"{fact_bits}. "
    if lever_phrase:
        body += f"{lever_phrase.capitalize()}. "
    body += "Want me to take the next step for you?"
    return body


def compose_proactive_message(
    *,
    category_ctx: Optional[CategoryContext],
    merchant_ctx: Optional[MerchantContext],
    customer_ctx: Optional[CustomerContext],
    trigger_ctx: TriggerContext,
    route: RouteDecision,
    addressing: AddressingPlan,
    facts: GroundingFacts,
    previous_bodies: Optional[List[str]] = None,
) -> ComposedMessage:
    category_id = (merchant_ctx.category if merchant_ctx else None) or trigger_ctx.category_id
    cfg = _get_category_config(category_ctx, category_id)

    system_prompt = _system_prompt(cfg, addressing, route.send_as)
    facts_block = "\n".join(f"- {f}" for f in facts.citable_facts) or "(no numeric facts available — do not cite any numbers)"
    user_prompt = (
        f"Trigger type: {trigger_ctx.trigger_type}\n"
        f"Trigger payload: {trigger_ctx.payload}\n"
        f"Grounded facts you may cite:\n{facts_block}\n\n"
        f"Write ONLY the WhatsApp message body."
    )
    fallback = _fallback_proactive_body(cfg, addressing, trigger_ctx, facts)
    body = _llm_generate(system_prompt, user_prompt, fallback)

    lint = guardrails.lint_message(body, taboo_words=cfg["vocab_taboo"], previous_bodies=previous_bodies or [])
    if not lint.passed and lint.taboo_hits:
        body = guardrails.sanitize_taboo(lint.clean_body, lint.taboo_hits)
    else:
        body = lint.clean_body

    cta = "open_ended"
    if facts.available_slots:
        cta = "multi_choice_slot"

    rationale = (
        f"{cfg['tone']} Cialdini lever applied: {', '.join(cfg['persuasion_levers'][:1])}. "
        f"Grounded on {len(facts.citable_facts)} deterministic fact(s)."
    )

    template_params = [str(f) for f in facts.citable_facts[:3]]

    return ComposedMessage(
        body=body,
        cta=cta,
        rationale=rationale,
        template_name=f"vera_{trigger_ctx.trigger_type or 'generic'}_v1",
        template_params=template_params,
    )


REPLY_STAGE_INSTRUCTIONS = {
    "qualifying_followup": (
        "Continue qualifying the merchant/customer's interest. Ask one short clarifying "
        "question that moves the conversation toward a concrete next step."
    ),
    "intent_transition": (
        "The other party just committed / said yes. Switch tone from qualifying to execution: "
        "confirm concretely what you're about to do, cite any grounded numbers available, and "
        "ask for final confirmation."
    ),
    "canned_followup": (
        "Your last message likely hit an auto-reply. Send one brief, low-pressure nudge "
        "checking if they saw your earlier message, with no new information."
    ),
    "closure": (
        "Gracefully end the conversation. Thank them, leave the door open for later, no CTA."
    ),
}


def _fallback_reply_body(stage: str, addressing: AddressingPlan, facts: GroundingFacts) -> str:
    if stage == "intent_transition":
        fact_bit = f" {facts.citable_facts[0]}." if facts.citable_facts else ""
        return (
            f"Great, {addressing.address_name}. Locking that in now.{fact_bit} "
            f"Reply CONFIRM and I'll send it through."
        )
    if stage == "canned_followup":
        return f"Hi {addressing.address_name}, just following up in case my last message got missed — no rush, whenever works."
    if stage == "closure":
        return f"No worries, {addressing.address_name} — I'll leave it here for now. Ping me anytime you need a hand."
    return f"{addressing.address_name}, happy to help — could you tell me a bit more about what you're looking for?"


def compose_reply_message(
    *,
    stage: str,
    category_ctx: Optional[CategoryContext],
    merchant_ctx: Optional[MerchantContext],
    customer_ctx: Optional[CustomerContext],
    route: RouteDecision,
    addressing: AddressingPlan,
    facts: GroundingFacts,
    incoming_message: str,
    previous_bodies: Optional[List[str]] = None,
) -> ComposedMessage:
    category_id = merchant_ctx.category if merchant_ctx else None
    cfg = _get_category_config(category_ctx, category_id)

    system_prompt = _system_prompt(cfg, addressing, route.send_as) + "\n" + REPLY_STAGE_INSTRUCTIONS.get(
        stage, REPLY_STAGE_INSTRUCTIONS["qualifying_followup"]
    )
    facts_block = "\n".join(f"- {f}" for f in facts.citable_facts) or "(no numeric facts available)"
    user_prompt = (
        f"Their message: \"{incoming_message}\"\n"
        f"Grounded facts you may cite:\n{facts_block}\n\n"
        f"Write ONLY the WhatsApp message body."
    )
    fallback = _fallback_reply_body(stage, addressing, facts)
    body = _llm_generate(system_prompt, user_prompt, fallback)

    lint = guardrails.lint_message(body, taboo_words=cfg["vocab_taboo"], previous_bodies=previous_bodies or [])
    if not lint.passed and lint.taboo_hits:
        body = guardrails.sanitize_taboo(lint.clean_body, lint.taboo_hits)
    else:
        body = lint.clean_body

    cta = {
        "qualifying_followup": "open_ended",
        "intent_transition": "binary_confirm_cancel",
        "canned_followup": "open_ended",
        "closure": "none",
    }.get(stage, "open_ended")

    rationale_map = {
        "qualifying_followup": "Continuing qualification with a low-friction open question.",
        "intent_transition": "Merchant/customer committed; switching from qualifying to action execution.",
        "canned_followup": "Prior message likely hit an auto-responder; sending a light no-pressure nudge.",
        "closure": "Conversation ending gracefully; no further pressure applied.",
    }

    return ComposedMessage(
        body=body,
        cta=cta,
        rationale=rationale_map.get(stage, "Continuing conversation."),
    )
