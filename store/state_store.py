"""
store/state_store.py — Atomic, version-controlled in-memory context store,
suppression ledger, and per-conversation dialogue state.

Everything here is protected by a single re-entrant lock. For a benchmarking
harness this is simpler and safer than fine-grained locking, and lookups
stay effectively O(1) (dict indexing) which keeps us well inside the
latency budgets even under repeated /tick and /reply calls.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from schemas.context_models import parse_context


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")



@dataclass
class ContextRecord:
    scope: str
    context_id: str
    version: int
    payload: Dict[str, Any]
    delivered_at: Optional[str]
    stored_at: str
    parsed: Any  



@dataclass
class TurnRecord:
    turn_number: int
    from_role: str
    message: str
    received_at: str
    is_canned: bool = False


@dataclass
class ConversationState:
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    status: str = "qualifying" 
    turns: List[TurnRecord] = field(default_factory=list)
    sent_bodies: List[str] = field(default_factory=list)
    canned_streak: int = 0
    last_canned_text: Optional[str] = None
    waiting_until: Optional[datetime] = None
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)


class StateStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._start_time = time.time()

        self._contexts: Dict[str, Dict[str, ContextRecord]] = {
            "category": {},
            "merchant": {},
            "customer": {},
            "trigger": {},
        }
        self._suppression_ledger: Dict[str, Optional[datetime]] = {}
        self._conversations: Dict[str, ConversationState] = {}
        self._ack_counter = 0

    def uptime_seconds(self) -> int:
        return int(time.time() - self._start_time)


    def ingest_context(
        self,
        scope: str,
        context_id: str,
        version: int,
        delivered_at: Optional[str],
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Atomically compare-and-set a context record.
        Returns a dict describing the outcome:
          {"accepted": True, "ack_id": ..., "stored_at": ...}
          {"accepted": False, "reason": "stale_version", "current_version": N}
        """
        with self._lock:
            bucket = self._contexts.setdefault(scope, {})
            existing = bucket.get(context_id)
            current_version = existing.version if existing else 0

            if version <= current_version:
                return {
                    "accepted": False,
                    "reason": "stale_version",
                    "current_version": current_version,
                }

            parsed = parse_context(scope, context_id, payload)
            stored_at = _iso(_utcnow())
            bucket[context_id] = ContextRecord(
                scope=scope,
                context_id=context_id,
                version=version,
                payload=payload,
                delivered_at=delivered_at,
                stored_at=stored_at,
                parsed=parsed,
            )
            self._ack_counter += 1
            ack_id = f"ack_{context_id}_v{version}"
            return {"accepted": True, "ack_id": ack_id, "stored_at": stored_at}

    def get_context(self, scope: str, context_id: str) -> Optional[ContextRecord]:
        with self._lock:
            return self._contexts.get(scope, {}).get(context_id)

    def get_parsed(self, scope: str, context_id: str) -> Optional[Any]:
        rec = self.get_context(scope, context_id)
        return rec.parsed if rec else None

    def all_contexts(self, scope: str) -> Dict[str, ContextRecord]:
        with self._lock:
            return dict(self._contexts.get(scope, {}))

    def counts(self) -> Dict[str, int]:
        with self._lock:
            return {s: len(b) for s, b in self._contexts.items()}

    def find_merchants_by_category(self, category_id: str) -> List[ContextRecord]:
        with self._lock:
            return [
                rec
                for rec in self._contexts.get("merchant", {}).values()
                if getattr(rec.parsed, "category", None) == category_id
            ]

    def find_customers_by_merchant(self, merchant_id: str) -> List[ContextRecord]:
        with self._lock:
            return [
                rec
                for rec in self._contexts.get("customer", {}).values()
                if getattr(rec.parsed, "merchant_id", None) == merchant_id
            ]


    def is_suppressed(self, key: str) -> bool:
        with self._lock:
            if key not in self._suppression_ledger:
                return False
            expiry = self._suppression_ledger[key]
            if expiry is None:
                return True
            if _utcnow() >= expiry:
                del self._suppression_ledger[key]
                return False
            return True

    def suppress(self, key: str, until: Optional[datetime] = None) -> None:
        with self._lock:
            self._suppression_ledger[key] = until

    def suppress_for_days(self, key: str, days: int) -> None:
        self.suppress(key, _utcnow() + timedelta(days=days))


    def get_or_create_conversation(
        self, conversation_id: str, merchant_id: Optional[str] = None, customer_id: Optional[str] = None
    ) -> ConversationState:
        with self._lock:
            convo = self._conversations.get(conversation_id)
            if convo is None:
                convo = ConversationState(
                    conversation_id=conversation_id,
                    merchant_id=merchant_id,
                    customer_id=customer_id,
                )
                self._conversations[conversation_id] = convo
            else:
                if merchant_id and not convo.merchant_id:
                    convo.merchant_id = merchant_id
                if customer_id and not convo.customer_id:
                    convo.customer_id = customer_id
            return convo

    def save_conversation(self, convo: ConversationState) -> None:
        with self._lock:
            convo.updated_at = _utcnow()
            self._conversations[convo.conversation_id] = convo


store = StateStore()
