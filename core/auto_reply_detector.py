"""
core/auto_reply_detector.py — WhatsApp canned auto-reply & opt-out detector.

Two independent classifiers used by the /v1/reply state machine:
  - is_canned_autoreply(): "I'm currently unavailable", "away from my phone",
    WhatsApp Business auto-responder boilerplate, etc.
  - is_opt_out(): explicit hostility or "stop messaging me" style requests.
"""

from __future__ import annotations

import re
from typing import Optional

_CANNED_PATTERNS = [
    r"\bcurrently (unavailable|away|out of office)\b",
    r"\bi(?:'| a)m away from my phone\b",
    r"\bthis is an automated (response|reply|message)\b",
    r"\bauto[- ]?reply\b",
    r"\bwill (respond|reply|get back to you) (shortly|soon|as soon as possible)\b",
    r"\bout of office\b",
    r"\bbusy right now\b.*\b(reply|respond)\b",
    r"\bthanks for (your|the) message,? i('| a)ll get back\b",
    r"\bcurrently on leave\b",
    r"\bdo not disturb\b",
]

_OPT_OUT_PATTERNS = [
    r"\bstop messaging me\b",
    r"\bstop contacting me\b",
    r"\bdo not contact me\b",
    r"\bdon'?t (message|text|contact) me\b",
    r"\bunsubscribe\b",
    r"\bremove me\b",
    r"\bleave me alone\b",
    r"\bstop\b\s*!*$",
    r"\bblock(ed)? this number\b",
    r"\bwho (gave|shared) you my number\b",
    r"\bharass(ment|ing)?\b",
    r"\bnever message me again\b",
]

_CANNED_RE = re.compile("|".join(_CANNED_PATTERNS), re.IGNORECASE)
_OPT_OUT_RE = re.compile("|".join(_OPT_OUT_PATTERNS), re.IGNORECASE)


def normalize(text: str) -> str:
    """Whitespace/case-normalized form, used for the 'identical canned text' check."""
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def is_canned_autoreply(text: str) -> bool:
    if not text:
        return False
    return bool(_CANNED_RE.search(text))


def is_opt_out(text: str) -> bool:
    if not text:
        return False
    return bool(_OPT_OUT_RE.search(text))


def classify(text: str) -> str:
    """Returns 'opt_out' | 'canned' | 'normal', opt-out taking priority."""
    if is_opt_out(text):
        return "opt_out"
    if is_canned_autoreply(text):
        return "canned"
    return "normal"


def texts_identical(a: Optional[str], b: Optional[str]) -> bool:
    if a is None or b is None:
        return False
    return normalize(a) == normalize(b)
