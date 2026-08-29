"""
core/guardrails.py — Output linter run on every generated message body
before it leaves the service.

Checks (in order):
  1. URL Elimination   — strip http(s)://, bare domains ending .com/.in/etc.
  2. Taboo Word Filter  — reject/flag category-specific prohibited vocabulary.
  3. Repetition Check    — never send an identical body twice in one conversation.
  4. CTA Classification  — cta must be one of the five allowed values.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

from core.auto_reply_detector import normalize

_URL_RE = re.compile(
    r"(https?://\S+)"
    r"|(\bwww\.\S+)"
    r"|(\b[a-zA-Z0-9-]+\.(?:com|in|co|io|org|net)\b\S*)",
    re.IGNORECASE,
)

ALLOWED_CTA = {
    "binary_yes_no",
    "binary_confirm_cancel",
    "multi_choice_slot",
    "open_ended",
    "none",
}


@dataclass
class LintResult:
    clean_body: str
    url_count_stripped: int = 0
    taboo_hits: List[str] = field(default_factory=list)
    is_repetition: bool = False
    cta_valid: bool = True
    passed: bool = True
    penalty_points: float = 0.0
    notes: List[str] = field(default_factory=list)


def strip_urls(text: str) -> tuple[str, int]:
    if not text:
        return text, 0
    count = len(_URL_RE.findall(text))
    cleaned = _URL_RE.sub("", text)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    cleaned = re.sub(r"\s+([.,!?])", r"\1", cleaned)
    return cleaned, count


def find_taboo_words(text: str, taboo_words: List[str]) -> List[str]:
    if not text or not taboo_words:
        return []
    hits = []
    lowered = text.lower()
    for word in taboo_words:
        w = word.lower().strip()
        if not w:
            continue
        pattern = r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])"
        if re.search(pattern, lowered):
            hits.append(word)
    return hits


def is_repeated_in_conversation(body: str, previous_bodies: List[str]) -> bool:
    norm = normalize(body)
    return any(normalize(prev) == norm for prev in previous_bodies)


def validate_cta(cta: Optional[str]) -> bool:
    return cta in ALLOWED_CTA


def lint_message(
    body: str,
    taboo_words: Optional[List[str]] = None,
    previous_bodies: Optional[List[str]] = None,
    cta: Optional[str] = None,
) -> LintResult:
    taboo_words = taboo_words or []
    previous_bodies = previous_bodies or []

    clean_body, url_count = strip_urls(body)
    taboo_hits = find_taboo_words(clean_body, taboo_words)
    repetition = is_repeated_in_conversation(clean_body, previous_bodies)
    cta_valid = validate_cta(cta) if cta is not None else True

    penalty = 0.0
    notes: List[str] = []

    if url_count:
        penalty += 3.0 * url_count
        notes.append(f"Stripped {url_count} URL(s) from message body (-{3.0 * url_count:.1f} pts).")

    if taboo_hits:
        notes.append(f"Taboo vocabulary detected and must be rewritten: {taboo_hits}")

    if repetition:
        notes.append("Body is identical to a previously sent message in this conversation.")

    if not cta_valid:
        notes.append(f"Invalid cta value: {cta!r}. Must be one of {sorted(ALLOWED_CTA)}.")

    passed = not taboo_hits and not repetition and cta_valid

    return LintResult(
        clean_body=clean_body,
        url_count_stripped=url_count,
        taboo_hits=taboo_hits,
        is_repetition=repetition,
        cta_valid=cta_valid,
        passed=passed,
        penalty_points=penalty,
        notes=notes,
    )


def sanitize_taboo(text: str, taboo_words: List[str]) -> str:
    """
    Last-resort mechanical scrub used only if a regenerated body still trips
    the taboo filter (e.g. the mock/template path): removes the exact taboo
    tokens rather than sending a blocked message.
    """
    cleaned = text
    for word in taboo_words:
        pattern = r"(?<![a-zA-Z0-9])" + re.escape(word) + r"(?![a-zA-Z0-9])"
        cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned
