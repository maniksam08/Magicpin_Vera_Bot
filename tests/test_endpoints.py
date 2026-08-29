"""
tests/test_endpoints.py — Contract-compatibility test suite.

Exercises all 5 mandatory endpoints against the exact response shapes in
the API spec: healthz, metadata, context versioning (accept + 409
conflict), tick (dispatch + suppression), and reply (opt-out, canned
auto-reply loop, and intent transition).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from main import app

client = TestClient(app)



def test_healthz_shape():
    resp = client.get("/v1/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert isinstance(body["uptime_seconds"], int)
    for key in ("category", "merchant", "customer", "trigger"):
        assert key in body["contexts_loaded"]


def test_metadata_shape():
    resp = client.get("/v1/metadata")
    assert resp.status_code == 200
    body = resp.json()
    for key in ("team_name", "team_members", "model", "approach", "contact_email", "version", "submitted_at"):
        assert key in body



def test_context_ingest_accept_then_conflict_then_upgrade():
    payload = {
        "scope": "category",
        "context_id": "dentists_test",
        "version": 1,
        "delivered_at": "2026-04-26T08:00:00Z",
        "payload": {
            "voice": {
                "tone": "Clinical peer.",
                "vocab_taboo": ["guaranteed", "100% safe"],
            }
        },
    }
    r1 = client.post("/v1/context", json=payload)
    assert r1.status_code == 200
    b1 = r1.json()
    assert b1["accepted"] is True
    assert "ack_id" in b1 and "stored_at" in b1

    r2 = client.post("/v1/context", json=payload)
    assert r2.status_code == 409
    b2 = r2.json()
    assert b2["accepted"] is False
    assert b2["reason"] == "stale_version"
    assert b2["current_version"] == 1

    payload_v2 = dict(payload, version=2)
    r3 = client.post("/v1/context", json=payload_v2)
    assert r3.status_code == 200
    assert r3.json()["accepted"] is True



def _seed_dentist_merchant_and_trigger():
    client.post(
        "/v1/context",
        json={
            "scope": "category",
            "context_id": "dentists",
            "version": 1,
            "payload": {
                "voice": {
                    "tone": "Clinical peer.",
                    "vocab_taboo": ["guaranteed", "100% safe"],
                    "persuasion_levers": ["authority"],
                }
            },
        },
    )
    client.post(
        "/v1/context",
        json={
            "scope": "merchant",
            "context_id": "m_001_drmeera_dentist_delhi",
            "version": 1,
            "payload": {
                "category": "dentists",
                "first_name": "Meera",
                "name": "Dr. Meera",
                "customer_aggregate": {"high_risk_adult_patients": 40, "total_customers": 240},
            },
        },
    )
    client.post(
        "/v1/context",
        json={
            "scope": "trigger",
            "context_id": "trg_001_research_digest_dentists",
            "version": 1,
            "payload": {
                "merchant_id": "m_001_drmeera_dentist_delhi",
                "category_id": "dentists",
                "trigger_type": "research_digest",
                "payload": {
                    "segment_key": "high_risk_adult_patients",
                    "segment_total_key": "total_customers",
                    "suppression_key": "research:dentists:2026-W17-test",
                },
            },
        },
    )


def test_tick_empty_when_no_triggers():
    resp = client.post("/v1/tick", json={"now": "2026-04-26T09:00:00Z", "available_triggers": []})
    assert resp.status_code == 200
    assert resp.json() == {"actions": []}


def test_tick_dispatches_then_suppresses_duplicate():
    _seed_dentist_merchant_and_trigger()

    resp1 = client.post(
        "/v1/tick",
        json={"now": "2026-04-26T09:00:00Z", "available_triggers": ["trg_001_research_digest_dentists"]},
    )
    assert resp1.status_code == 200
    actions1 = resp1.json()["actions"]
    assert len(actions1) == 1
    action = actions1[0]
    assert action["send_as"] == "vera"
    assert action["merchant_id"] == "m_001_drmeera_dentist_delhi"
    assert action["cta"] in {
        "binary_yes_no",
        "binary_confirm_cancel",
        "multi_choice_slot",
        "open_ended",
        "none",
    }
    assert "http://" not in action["body"] and "https://" not in action["body"]
    for taboo in ("guaranteed", "100% safe"):
        assert taboo.lower() not in action["body"].lower()

    resp2 = client.post(
        "/v1/tick",
        json={"now": "2026-04-26T09:05:00Z", "available_triggers": ["trg_001_research_digest_dentists"]},
    )
    assert resp2.status_code == 200
    assert resp2.json() == {"actions": []}


def test_tick_unknown_trigger_ignored_gracefully():
    resp = client.post("/v1/tick", json={"now": "2026-04-26T09:00:00Z", "available_triggers": ["trg_does_not_exist"]})
    assert resp.status_code == 200
    assert resp.json() == {"actions": []}



def test_reply_opt_out_ends_and_suppresses():
    resp = client.post(
        "/v1/reply",
        json={
            "conversation_id": "conv_test_optout",
            "merchant_id": "m_optout_test",
            "from_role": "merchant",
            "message": "Stop messaging me please",
            "received_at": "2026-04-26T10:00:00Z",
            "turn_number": 1,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["action"] == "end"

    _seed_dentist_merchant_and_trigger()
    resp2 = client.post(
        "/v1/context",
        json={
            "scope": "trigger",
            "context_id": "trg_optout_check",
            "version": 1,
            "payload": {
                "merchant_id": "m_optout_test",
                "category_id": "dentists",
                "trigger_type": "research_digest",
                "payload": {"suppression_key": "research:dentists:optout-check"},
            },
        },
    )
    assert resp2.status_code == 200
    tick_resp = client.post(
        "/v1/tick",
        json={"now": "2026-04-26T10:05:00Z", "available_triggers": ["trg_optout_check"]},
    )
    assert tick_resp.json() == {"actions": []}


def test_reply_canned_auto_reply_loop():
    conv_id = "conv_test_canned"
    canned_text = "I'm currently unavailable and will respond shortly."

    r2 = client.post(
        "/v1/reply",
        json={
            "conversation_id": conv_id,
            "merchant_id": "m_canned_test",
            "from_role": "merchant",
            "message": canned_text,
            "received_at": "2026-04-26T10:00:00Z",
            "turn_number": 2,
        },
    )
    assert r2.status_code == 200
    b2 = r2.json()
    assert b2["action"] == "send"
    assert b2["body"]

    r3 = client.post(
        "/v1/reply",
        json={
            "conversation_id": conv_id,
            "merchant_id": "m_canned_test",
            "from_role": "merchant",
            "message": canned_text,
            "received_at": "2026-04-26T11:00:00Z",
            "turn_number": 3,
        },
    )
    assert r3.status_code == 200
    b3 = r3.json()
    assert b3["action"] == "wait"
    assert b3["wait_seconds"] == 86400

    r4 = client.post(
        "/v1/reply",
        json={
            "conversation_id": conv_id,
            "merchant_id": "m_canned_test",
            "from_role": "merchant",
            "message": canned_text,
            "received_at": "2026-04-26T12:00:00Z",
            "turn_number": 4,
        },
    )
    assert r4.status_code == 200
    assert r4.json()["action"] == "end"


def test_reply_intent_transition_to_execution():
    conv_id = "conv_test_intent"
    r1 = client.post(
        "/v1/reply",
        json={
            "conversation_id": conv_id,
            "merchant_id": "m_intent_test",
            "from_role": "merchant",
            "message": "What would this cost me?",
            "received_at": "2026-04-26T10:00:00Z",
            "turn_number": 1,
        },
    )
    assert r1.status_code == 200
    assert r1.json()["action"] == "send"

    r2 = client.post(
        "/v1/reply",
        json={
            "conversation_id": conv_id,
            "merchant_id": "m_intent_test",
            "from_role": "merchant",
            "message": "Ok, let's do it",
            "received_at": "2026-04-26T10:05:00Z",
            "turn_number": 2,
        },
    )
    assert r2.status_code == 200
    b2 = r2.json()
    assert b2["action"] == "send"
    assert b2["cta"] == "binary_confirm_cancel"
