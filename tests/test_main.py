"""Tests for the four API endpoints.

The validation tests carry the most weight. An API that accepts an outcome for
an order it never scored, or an override with no reason, silently degrades the
audit log - and the audit log is the thing the whole pitch rests on. Every one
of those paths is checked for the right status code AND a message that tells the
caller what to do about it.
"""

import pytest
from fastapi.testclient import TestClient

from app import main
from app.audit_service import AuditLog


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A client backed by a throwaway database file."""
    db = tmp_path / "audit.db"
    monkeypatch.setattr(main, "_db_path", str(db))
    with TestClient(main.app) as test_client:
        yield test_client


REFUSER = {
    "order_id": "ORD-001", "payment_method": "cod", "order_value": 3500,
    "category": "apparel", "completed_orders": 3, "returned_orders": 2,
    "refusals_in_last_3": 2,
}
CLEAN = {
    "order_id": "ORD-002", "payment_method": "prepaid", "order_value": 800,
    "category": "books", "completed_orders": 8, "returned_orders": 0,
}
GOOD_REASON = "regular wholesale buyer, verified by phone"


# --------------------------------------------------------------------------
# POST /risk/assess
# --------------------------------------------------------------------------
def test_assess_returns_action_tier_and_reasons(client):
    r = client.post("/risk/assess", json=REFUSER)
    assert r.status_code == 200
    body = r.json()

    assert body["recommended_action"] == "nudge"
    assert body["policy_tier"] == "high"
    assert body["score"] == 85
    assert body["evidence_score"] == 40
    assert any("Repeat refusal" in reason for reason in body["reasons"])
    assert {h["id"] for h in body["fired_rules"]} >= {"H1", "P1", "P2"}
    assert "synthetic" in body["disclaimer"].lower()


def test_assess_writes_the_decision_to_the_audit_log(client):
    client.post("/risk/assess", json=REFUSER)
    trail = client.get("/orders/ORD-001/audit").json()
    assert trail["found"] is True
    assert trail["current_decision"]["recommended_action"] == "nudge"


def test_a_clean_prepaid_order_ships(client):
    body = client.post("/risk/assess", json=CLEAN).json()
    assert body["recommended_action"] == "ship"
    assert body["score"] == 0


def test_policy_choice_changes_the_action(client):
    gentle = client.post("/risk/assess", json=dict(REFUSER, merchant_id="gentle")).json()
    strict = client.post("/risk/assess", json=dict(REFUSER, merchant_id="strict")).json()
    assert gentle["recommended_action"] == "confirm"
    assert strict["recommended_action"] == "review"


def test_unknown_policy_is_refused_with_the_known_ones_listed(client):
    r = client.post("/risk/assess", json=dict(REFUSER, merchant_id="nope"))
    assert r.status_code == 422
    assert "unknown merchant policy" in r.json()["detail"]
    assert "default" in r.json()["detail"]


@pytest.mark.parametrize("bad", [
    {"payment_method": "bitcoin"},
    {"address_quality": "terrible"},
    {"order_value": -5},
    {"variant_count": 0},
    {"refusals_in_last_3": 9},
    {"baseline_rto_rate": 0},
    {"order_id": ""},
])
def test_malformed_assess_payloads_are_rejected(client, bad):
    r = client.post("/risk/assess", json=dict(REFUSER, **bad))
    assert r.status_code == 422


# --------------------------------------------------------------------------
# POST /orders/{id}/outcome - the validation the orphan problem needed
# --------------------------------------------------------------------------
def test_outcome_for_an_unscored_order_is_refused(client):
    r = client.post("/orders/ORD-NEVER-SEEN/outcome",
                    json={"is_rto": True, "source": "courier"})
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert "ORD-NEVER-SEEN" in detail
    assert "check the order id" in detail


def test_a_refused_outcome_leaves_nothing_behind(client, tmp_path):
    """The point of the check: no orphan row, not merely an error response."""
    client.post("/orders/ORD-TYPO/outcome", json={"is_rto": True, "source": "courier"})
    log = AuditLog(main._db_path)
    assert log.counts()["outcomes"] == 0
    log.close()


def test_outcome_is_recorded_for_a_known_order(client):
    client.post("/risk/assess", json=REFUSER)
    r = client.post("/orders/ORD-001/outcome",
                    json={"is_rto": True, "source": "courier"})
    assert r.status_code == 200
    assert r.json()["outcomes_on_record"] == 1
    assert r.json()["superseded_earlier_outcome"] is False

    trail = client.get("/orders/ORD-001/audit").json()
    assert trail["current_outcome"]["is_rto"] is True


def test_a_corrected_outcome_appends_and_says_so(client):
    client.post("/risk/assess", json=REFUSER)
    client.post("/orders/ORD-001/outcome", json={"is_rto": False, "source": "courier"})
    r = client.post("/orders/ORD-001/outcome",
                    json={"is_rto": True, "source": "merchant",
                          "note": "courier feed was wrong"})

    assert r.json()["outcomes_on_record"] == 2
    assert r.json()["superseded_earlier_outcome"] is True
    trail = client.get("/orders/ORD-001/audit").json()
    assert trail["outcomes"][0]["is_rto"] is False
    assert trail["current_outcome"]["is_rto"] is True


def test_outcome_needs_a_source(client):
    client.post("/risk/assess", json=REFUSER)
    r = client.post("/orders/ORD-001/outcome", json={"is_rto": True, "source": ""})
    assert r.status_code == 422


def test_outcome_needs_is_rto(client):
    client.post("/risk/assess", json=REFUSER)
    assert client.post("/orders/ORD-001/outcome", json={"source": "courier"}).status_code == 422


# --------------------------------------------------------------------------
# POST /orders/{id}/override
# --------------------------------------------------------------------------
def test_override_for_an_unscored_order_is_refused(client):
    r = client.post("/orders/ORD-NEVER-SEEN/override",
                    json={"action": "ship", "reason": GOOD_REASON, "actor": "anushree"})
    assert r.status_code == 404
    assert "nothing to override" in r.json()["detail"]


def test_override_records_both_the_recommendation_and_the_human_call(client):
    client.post("/risk/assess", json=REFUSER)
    r = client.post("/orders/ORD-001/override",
                    json={"action": "ship", "reason": GOOD_REASON, "actor": "anushree"})

    assert r.status_code == 200
    assert r.json()["recommended_action"] == "nudge"
    assert r.json()["final_action"] == "ship"

    trail = client.get("/orders/ORD-001/audit").json()
    assert trail["current_decision"]["recommended_action"] == "nudge"
    assert trail["final_action"] == "ship"
    assert trail["overrides"][0]["reason"] == GOOD_REASON


@pytest.mark.parametrize("reason", ["", "ok", "too short"])
def test_override_without_a_real_reason_is_refused(client, reason):
    client.post("/risk/assess", json=REFUSER)
    r = client.post("/orders/ORD-001/override",
                    json={"action": "ship", "reason": reason, "actor": "anushree"})
    assert r.status_code == 422
    # detail is a plain string from HTTPException here, not Pydantic's error list
    assert "reason" in str(r.json()["detail"]).lower()


def test_override_needs_an_actor(client):
    client.post("/risk/assess", json=REFUSER)
    r = client.post("/orders/ORD-001/override",
                    json={"action": "ship", "reason": GOOD_REASON, "actor": ""})
    assert r.status_code == 422


def test_override_rejects_an_action_that_does_not_exist(client):
    """There is no block action, and the API must not invent one."""
    client.post("/risk/assess", json=REFUSER)
    r = client.post("/orders/ORD-001/override",
                    json={"action": "block", "reason": GOOD_REASON, "actor": "anushree"})
    assert r.status_code == 422


def test_override_attaches_to_the_latest_decision_only(client):
    client.post("/risk/assess", json=REFUSER)
    client.post("/orders/ORD-001/override",
                json={"action": "ship", "reason": GOOD_REASON, "actor": "anushree"})
    # Re-scored later; the earlier override must not carry over.
    client.post("/risk/assess", json=REFUSER)

    trail = client.get("/orders/ORD-001/audit").json()
    assert len(trail["decisions"]) == 2
    assert len(trail["overrides"]) == 1
    assert trail["final_action"] == "nudge"


# --------------------------------------------------------------------------
# GET /orders/{id}/audit
# --------------------------------------------------------------------------
def test_audit_for_an_unknown_order_is_404(client):
    r = client.get("/orders/ORD-NEVER-SEEN/audit")
    assert r.status_code == 404
    assert "ORD-NEVER-SEEN" in r.json()["detail"]


def test_audit_returns_the_full_timeline_in_order(client):
    client.post("/risk/assess", json=REFUSER)
    client.post("/orders/ORD-001/override",
                json={"action": "ship", "reason": GOOD_REASON, "actor": "anushree"})
    client.post("/orders/ORD-001/outcome", json={"is_rto": True, "source": "courier"})

    trail = client.get("/orders/ORD-001/audit").json()
    assert [e["kind"] for e in trail["timeline"]] == ["decision", "override", "outcome"]
    assert "synthetic" in trail["disclaimer"].lower()


# --------------------------------------------------------------------------
# Shape of the API itself
# --------------------------------------------------------------------------
def test_only_the_five_endpoints_exist(client):
    """Scope freeze is a working rule, so it is enforced by a test.

    GET /orders was added deliberately in Step 8 and CLAUDE.md updated to match;
    the list is frozen again at these five. Anything new has to be argued for.
    """
    paths = {r.path for r in main.app.routes if r.path.startswith(("/risk", "/orders"))}
    assert paths == {
        "/risk/assess",
        "/orders",
        "/orders/{order_id}/outcome",
        "/orders/{order_id}/override",
        "/orders/{order_id}/audit",
    }


def test_openapi_schema_builds(client):
    assert client.get("/openapi.json").status_code == 200


# --------------------------------------------------------------------------
# GET /orders - the queue
# --------------------------------------------------------------------------
def test_queue_lists_scored_orders_newest_state_first(client):
    client.post("/risk/assess", json=REFUSER)
    client.post("/risk/assess", json=CLEAN)

    page = client.get("/orders").json()
    assert page["total"] == 2
    assert {i["order_id"] for i in page["items"]} == {"ORD-001", "ORD-002"}
    assert page["items"][0]["score"] >= page["items"][1]["score"], "default sort is by score"


def test_queue_shows_one_row_per_order_even_after_rescoring(client):
    client.post("/risk/assess", json=REFUSER)
    client.post("/risk/assess", json=REFUSER)
    page = client.get("/orders").json()
    assert page["total"] == 1
    assert page["items"][0]["order_id"] == "ORD-001"


def test_queue_filters_by_action(client):
    client.post("/risk/assess", json=REFUSER)
    client.post("/risk/assess", json=CLEAN)
    assert client.get("/orders?action=nudge").json()["total"] == 1
    assert client.get("/orders?action=review").json()["total"] == 0


def test_queue_filters_by_outcome_state(client):
    client.post("/risk/assess", json=REFUSER)
    client.post("/risk/assess", json=CLEAN)
    client.post("/orders/ORD-001/outcome", json={"is_rto": True, "source": "courier"})

    assert client.get("/orders?outcome=rto").json()["total"] == 1
    assert client.get("/orders?outcome=pending").json()["total"] == 1
    assert client.get("/orders?outcome=delivered").json()["total"] == 0


def test_queue_shows_overrides(client):
    client.post("/risk/assess", json=REFUSER)
    client.post("/orders/ORD-001/override",
                json={"action": "ship", "reason": GOOD_REASON, "actor": "anushree"})

    row = client.get("/orders?overridden=true").json()["items"][0]
    assert row["was_overridden"] is True
    assert row["recommended_action"] == "nudge"
    assert row["final_action"] == "ship"
    assert row["override_actor"] == "anushree"
    assert client.get("/orders?overridden=false").json()["total"] == 0


def test_queue_paginates_honestly(client):
    for i in range(5):
        client.post("/risk/assess", json=dict(REFUSER, order_id="ORD-%03d" % i))
    page = client.get("/orders?limit=2&offset=2").json()
    assert page["total"] == 5, "total is the whole set, not the page"
    assert len(page["items"]) == 2
    assert page["offset"] == 2


@pytest.mark.parametrize("query", ["action=block", "tier=extreme", "outcome=maybe",
                                   "sort=random", "limit=0", "limit=9999", "offset=-1"])
def test_queue_rejects_nonsense_filters(client, query):
    assert client.get("/orders?" + query).status_code == 422
