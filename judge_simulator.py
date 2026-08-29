"""
judge_simulator.py — Local, headless benchmarking stand-in.

This is NOT the official magicpin judge harness (we don't have access to
it) — it's a local script that exercises the same 5-endpoint contract the
way an automated judge would: seed contexts, drive /v1/tick, then run a
full /v1/reply conversation through several scenarios, printing a pass/fail
summary against the shapes and behaviors defined in the spec. Use it to
sanity-check a running instance before submitting.

Usage:
    # In one terminal:
    uvicorn main:app --host 0.0.0.0 --port 8080

    # In another terminal:
    python judge_simulator.py --base-url http://localhost:8080
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict

import httpx

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"

results = []


def check(name: str, condition: bool, detail: str = "") -> None:
    results.append((name, condition, detail))
    tag = PASS if condition else FAIL
    print(f"[{tag}] {name}" + (f" — {detail}" if detail and not condition else ""))


def run(base_url: str) -> int:
    client = httpx.Client(base_url=base_url, timeout=60.0)

    r = client.get("/v1/healthz")
    check("GET /v1/healthz returns 200", r.status_code == 200, str(r.text))
    if r.status_code == 200:
        body = r.json()
        check("healthz.status == 'ok'", body.get("status") == "ok")
        check(
            "healthz.contexts_loaded has all 4 scope keys",
            all(k in body.get("contexts_loaded", {}) for k in ("category", "merchant", "customer", "trigger")),
        )

    r = client.get("/v1/metadata")
    check("GET /v1/metadata returns 200", r.status_code == 200)
    if r.status_code == 200:
        body = r.json()
        required = {"team_name", "team_members", "model", "approach", "contact_email", "version", "submitted_at"}
        check("metadata has all required fields", required.issubset(body.keys()), str(body.keys()))

    category_payload = {
        "scope": "category",
        "context_id": "dentists_sim",
        "version": 1,
        "payload": {
            "voice": {
                "tone": "Clinical peer.",
                "vocab_taboo": ["guaranteed", "100% safe", "cure"],
                "persuasion_levers": ["authority", "social_proof"],
            }
        },
    }
    r = client.post("/v1/context", json=category_payload)
    check("POST /v1/context (new, v1) returns 200 accepted", r.status_code == 200 and r.json().get("accepted") is True)

    r = client.post("/v1/context", json=category_payload)
    check(
        "POST /v1/context (stale version) returns 409",
        r.status_code == 409 and r.json().get("reason") == "stale_version",
    )

    merchant_payload = {
        "scope": "merchant",
        "context_id": "m_sim_drmeera",
        "version": 1,
        "payload": {
            "category": "dentists_sim",
            "first_name": "Meera",
            "customer_aggregate": {"high_risk_adult_patients": 22, "total_customers": 240},
        },
    }
    r = client.post("/v1/context", json=merchant_payload)
    check("POST /v1/context (merchant) returns 200", r.status_code == 200)

    trigger_payload = {
        "scope": "trigger",
        "context_id": "trg_sim_research_digest",
        "version": 1,
        "payload": {
            "merchant_id": "m_sim_drmeera",
            "category_id": "dentists_sim",
            "trigger_type": "research_digest",
            "payload": {
                "segment_key": "high_risk_adult_patients",
                "segment_total_key": "total_customers",
                "suppression_key": "research:dentists_sim:sim-run",
            },
        },
    }
    r = client.post("/v1/context", json=trigger_payload)
    check("POST /v1/context (trigger) returns 200", r.status_code == 200)

    r = client.post("/v1/tick", json={"now": "2026-04-26T09:00:00Z", "available_triggers": []})
    check("POST /v1/tick with no triggers returns empty actions", r.status_code == 200 and r.json() == {"actions": []})

    r = client.post(
        "/v1/tick",
        json={"now": "2026-04-26T09:00:00Z", "available_triggers": ["trg_sim_research_digest"]},
    )
    check("POST /v1/tick dispatches an action", r.status_code == 200 and len(r.json().get("actions", [])) == 1)
    if r.status_code == 200 and r.json().get("actions"):
        action = r.json()["actions"][0]
        required_fields = {
            "conversation_id",
            "merchant_id",
            "customer_id",
            "send_as",
            "trigger_id",
            "template_name",
            "template_params",
            "body",
            "cta",
            "suppression_key",
            "rationale",
        }
        check("Action has all required fields", required_fields.issubset(action.keys()), str(action.keys()))
        check("Action.send_as == 'vera' for merchant-scoped trigger", action.get("send_as") == "vera")
        check(
            "Action.cta is a valid CTA type",
            action.get("cta") in {"binary_yes_no", "binary_confirm_cancel", "multi_choice_slot", "open_ended", "none"},
        )
        check("Action.body contains no bare URLs", "http://" not in action["body"] and "https://" not in action["body"])
        check(
            "Action.body avoids category taboo vocabulary",
            not any(t.lower() in action["body"].lower() for t in ["guaranteed", "100% safe", "cure"]),
        )

    r = client.post(
        "/v1/tick",
        json={"now": "2026-04-26T09:05:00Z", "available_triggers": ["trg_sim_research_digest"]},
    )
    check(
        "POST /v1/tick (same trigger again) is suppressed — algorithmic restraint",
        r.status_code == 200 and r.json() == {"actions": []},
    )

    r = client.post(
        "/v1/reply",
        json={
            "conversation_id": "conv_sim_optout",
            "merchant_id": "m_sim_optout",
            "from_role": "merchant",
            "message": "Stop messaging me, this is harassment",
            "received_at": "2026-04-26T10:00:00Z",
            "turn_number": 1,
        },
    )
    check(
        "POST /v1/reply opt-out returns action=end",
        r.status_code == 200 and r.json().get("action") == "end",
        str(r.text),
    )

    canned = "I'm currently unavailable and will respond shortly."
    conv_id = "conv_sim_canned"
    r2 = client.post(
        "/v1/reply",
        json={
            "conversation_id": conv_id,
            "merchant_id": "m_sim_canned",
            "from_role": "merchant",
            "message": canned,
            "received_at": "2026-04-26T10:00:00Z",
            "turn_number": 2,
        },
    )
    check("reply turn 2 (canned) -> action=send follow-up", r2.status_code == 200 and r2.json().get("action") == "send")

    r3 = client.post(
        "/v1/reply",
        json={
            "conversation_id": conv_id,
            "merchant_id": "m_sim_canned",
            "from_role": "merchant",
            "message": canned,
            "received_at": "2026-04-26T11:00:00Z",
            "turn_number": 3,
        },
    )
    check(
        "reply turn 3 (identical canned) -> action=wait, wait_seconds=86400",
        r3.status_code == 200 and r3.json().get("action") == "wait" and r3.json().get("wait_seconds") == 86400,
    )

    r4 = client.post(
        "/v1/reply",
        json={
            "conversation_id": conv_id,
            "merchant_id": "m_sim_canned",
            "from_role": "merchant",
            "message": canned,
            "received_at": "2026-04-26T12:00:00Z",
            "turn_number": 4,
        },
    )
    check("reply turn 4 (still canned) -> action=end", r4.status_code == 200 and r4.json().get("action") == "end")

    conv_id2 = "conv_sim_intent"
    client.post(
        "/v1/reply",
        json={
            "conversation_id": conv_id2,
            "merchant_id": "m_sim_intent",
            "from_role": "merchant",
            "message": "Kitna kharcha aayega?",
            "received_at": "2026-04-26T10:00:00Z",
            "turn_number": 1,
        },
    )
    r = client.post(
        "/v1/reply",
        json={
            "conversation_id": conv_id2,
            "merchant_id": "m_sim_intent",
            "from_role": "merchant",
            "message": "Ok, let's do it",
            "received_at": "2026-04-26T10:05:00Z",
            "turn_number": 2,
        },
    )
    check(
        "reply commit message -> action=send, cta=binary_confirm_cancel",
        r.status_code == 200 and r.json().get("action") == "send" and r.json().get("cta") == "binary_confirm_cancel",
    )

    total = len(results)
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{passed}/{total} checks passed.")
    return 0 if passed == total else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Local judge simulator for the Vera API contract.")
    parser.add_argument("--base-url", default="http://localhost:8080", help="Base URL of the running Vera service.")
    args = parser.parse_args()
    sys.exit(run(args.base_url))
