"""Tests for the audit log.

The append-only tests are the ones that matter. "Every score, rule, threshold,
override and final delivery outcome recorded in an audit trail" is in the locked
pitch, and a trail that can be quietly edited is not a trail. So the tests go
around the Python API and hit the database directly with raw UPDATE and DELETE -
if the guarantee only holds when callers are polite, it is not a guarantee.
"""

from datetime import datetime
import concurrent.futures
import sqlite3

import pytest

from app.audit_service import AUDIT_SCHEMA_VERSION, AuditLog, UnknownOrderError
from app.policy_engine import DEFAULT_POLICY, Action, apply_override, decide
from app.rule_engine import OrderFeatures, assess

REFUSER = dict(order_id="ORD-001", payment_method="cod", order_value=3500,
               category="apparel", completed_orders=3, returned_orders=2,
               refusals_in_last_3=2)
CLEAN = dict(order_id="ORD-002", payment_method="prepaid", order_value=800,
             category="books", completed_orders=8, returned_orders=0)

GOOD_REASON = "regular wholesale buyer, verified by phone"


@pytest.fixture
def log():
    with AuditLog(":memory:") as audit:
        yield audit


def decision_for(**kwargs):
    features = OrderFeatures(**kwargs)
    return decide(assess(features), DEFAULT_POLICY, features.order_id)


# --------------------------------------------------------------------------
# Append-only - enforced by the database, not by callers
# --------------------------------------------------------------------------
@pytest.mark.parametrize("table", ["decisions", "overrides", "outcomes"])
def test_rows_cannot_be_updated_even_by_raw_sql(log, table):
    did = log.record_decision(decision_for(**REFUSER))
    log.record_override(did, Action.SHIP, GOOD_REASON, actor="anushree")
    log.record_outcome("ORD-001", is_rto=False)

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        log._conn.execute("UPDATE %s SET order_id = 'TAMPERED'" % table)


@pytest.mark.parametrize("table", ["decisions", "overrides", "outcomes"])
def test_rows_cannot_be_deleted_even_by_raw_sql(log, table):
    did = log.record_decision(decision_for(**REFUSER))
    log.record_override(did, Action.SHIP, GOOD_REASON, actor="anushree")
    log.record_outcome("ORD-001", is_rto=False)

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        log._conn.execute("DELETE FROM %s" % table)


def test_short_override_reason_is_refused_by_the_database_too(log):
    """Not only by the Python guard - the constraint is in the schema."""
    log.record_decision(decision_for(**REFUSER))
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
        log._conn.execute(
            """INSERT INTO overrides
                   (decision_id, order_id, overridden_at, action, actor, reason)
               VALUES (1, 'ORD-001', '2026-09-01T10:00:00', 'ship', 'me', 'ok')""")


def test_schema_version_is_recorded(log):
    assert log.schema_version == AUDIT_SCHEMA_VERSION


# --------------------------------------------------------------------------
# Decisions
# --------------------------------------------------------------------------
def test_decision_round_trips_with_everything_the_brief_requires(log):
    log.record_decision(decision_for(**REFUSER), decided_at=datetime(2026, 8, 20, 10))
    stored = log.get_audit("ORD-001")["current_decision"]

    for required in ("rule_version", "policy_version", "raw_score", "score",
                     "evidence_score", "tier_before_safeguards", "engine_tier",
                     "policy_tier", "recommended_action", "fired_rules",
                     "reasons", "safeguards_applied", "policy_adjustments"):
        assert required in stored, "audit row is missing %s" % required

    assert stored["recommended_action"] == "nudge"
    assert stored["score"] == 85
    assert isinstance(stored["fired_rules"], list)
    assert {r["id"] for r in stored["fired_rules"]} >= {"H1", "P1"}
    assert isinstance(stored["reasons"], list) and stored["reasons"]


def test_reassessment_appends_rather_than_replacing(log):
    """An order can legitimately be scored twice - both must survive."""
    log.record_decision(decision_for(**REFUSER), decided_at=datetime(2026, 8, 20, 10))
    improved = dict(REFUSER, refusals_in_last_3=0, returned_orders=0, completed_orders=6)
    log.record_decision(decision_for(**improved), decided_at=datetime(2026, 8, 21, 10))

    trail = log.get_audit("ORD-001")
    assert len(trail["decisions"]) == 2
    # The re-scored order is COD 3,500 apparel with a clean record: P1 25 +
    # P2 10 + C1 10 = 45, with the trust discount clamped to 0 at the group
    # floor. 45 is Medium, so it steps down from nudge to confirm - not all the
    # way to ship, because it is still a mid-value COD apparel order.
    assert trail["decisions"][0]["recommended_action"] == "nudge"
    assert trail["current_decision"]["recommended_action"] == "confirm"
    assert trail["current_decision"]["score"] == 45
    assert trail["final_action"] == "confirm"


def test_unknown_order_reports_not_found(log):
    trail = log.get_audit("ORD-NOPE")
    assert trail["found"] is False
    assert trail["decisions"] == []
    assert trail["final_action"] is None


# --------------------------------------------------------------------------
# Overrides
# --------------------------------------------------------------------------
def test_override_never_erases_the_recommendation(log):
    did = log.record_decision(decision_for(**REFUSER))
    log.record_override(did, Action.SHIP, GOOD_REASON, actor="anushree")

    trail = log.get_audit("ORD-001")
    assert trail["current_decision"]["recommended_action"] == "nudge"
    assert trail["final_action"] == "ship"
    assert trail["overrides"][0]["reason"] == GOOD_REASON
    assert trail["overrides"][0]["actor"] == "anushree"


def test_decision_carrying_an_override_records_both_events(log):
    """apply_override then record_decision must not lose the override."""
    overridden = apply_override(decision_for(**REFUSER), Action.SHIP,
                                GOOD_REASON, actor="anushree")
    log.record_decision(overridden)

    trail = log.get_audit("ORD-001")
    assert len(trail["overrides"]) == 1
    assert trail["final_action"] == "ship"
    assert [e["kind"] for e in trail["timeline"]] == ["decision", "override"]


@pytest.mark.parametrize("reason", ["", "   ", "ok", "too short"])
def test_override_demands_a_real_reason(log, reason):
    did = log.record_decision(decision_for(**REFUSER))
    with pytest.raises(ValueError, match="reason"):
        log.record_override(did, Action.SHIP, reason, actor="anushree")


def test_override_demands_an_actor(log):
    did = log.record_decision(decision_for(**REFUSER))
    with pytest.raises(ValueError, match="who made it"):
        log.record_override(did, Action.SHIP, GOOD_REASON, actor="  ")


def test_override_rejects_a_non_action(log):
    did = log.record_decision(decision_for(**REFUSER))
    with pytest.raises(ValueError, match="must be an Action"):
        log.record_override(did, "ship", GOOD_REASON, actor="anushree")


def test_cannot_override_a_decision_that_does_not_exist(log):
    with pytest.raises(ValueError, match="no decision"):
        log.record_override(999, Action.SHIP, GOOD_REASON, actor="anushree")


def test_an_old_override_does_not_leak_into_a_newer_decision(log):
    """Overriding assessment #1 must not silently apply to assessment #2."""
    first = log.record_decision(decision_for(**REFUSER),
                                decided_at=datetime(2026, 8, 20, 10))
    log.record_override(first, Action.SHIP, GOOD_REASON, actor="anushree",
                        at=datetime(2026, 8, 20, 11))
    log.record_decision(decision_for(**REFUSER), decided_at=datetime(2026, 8, 25, 10))

    trail = log.get_audit("ORD-001")
    assert len(trail["overrides"]) == 1
    assert trail["final_action"] == "nudge", "the newer decision stands on its own"


# --------------------------------------------------------------------------
# Outcomes
# --------------------------------------------------------------------------
def test_outcome_correction_appends_and_keeps_the_original(log):
    log.record_decision(decision_for(**REFUSER))
    log.record_outcome("ORD-001", is_rto=False, source="courier",
                       at=datetime(2026, 8, 25, 9))
    log.record_outcome("ORD-001", is_rto=True, source="merchant",
                       note="courier feed was wrong", at=datetime(2026, 8, 27, 16))

    trail = log.get_audit("ORD-001")
    assert len(trail["outcomes"]) == 2
    assert trail["outcomes"][0]["is_rto"] is False
    assert trail["current_outcome"]["is_rto"] is True
    assert trail["current_outcome"]["note"] == "courier feed was wrong"


def test_outcome_requires_a_source(log):
    with pytest.raises(ValueError, match="where it came from"):
        log.record_outcome("ORD-001", is_rto=True, source=" ")


def test_outcome_is_stored_as_a_real_boolean(log):
    log.record_decision(decision_for(**REFUSER))
    log.record_outcome("ORD-001", is_rto=True)
    assert log.get_audit("ORD-001")["current_outcome"]["is_rto"] is True


# --------------------------------------------------------------------------
# Timeline and summaries
# --------------------------------------------------------------------------
def test_timeline_is_ordered_and_readable(log):
    did = log.record_decision(decision_for(**REFUSER), decided_at=datetime(2026, 8, 20, 10))
    log.record_override(did, Action.SHIP, GOOD_REASON, actor="anushree",
                        at=datetime(2026, 8, 20, 11, 30))
    log.record_outcome("ORD-001", is_rto=False, source="courier",
                       at=datetime(2026, 8, 25, 9))

    events = log.get_audit("ORD-001")["timeline"]
    assert [e["kind"] for e in events] == ["decision", "override", "outcome"]
    assert [e["at"] for e in events] == sorted(e["at"] for e in events)
    assert "recommended nudge" in events[0]["summary"]
    assert "anushree" in events[1]["summary"]
    assert "delivered" in events[2]["summary"]


def test_same_timestamp_still_orders_decision_before_override(log):
    at = datetime(2026, 8, 20, 10)
    did = log.record_decision(decision_for(**REFUSER), decided_at=at)
    log.record_override(did, Action.SHIP, GOOD_REASON, actor="anushree", at=at)
    assert [e["kind"] for e in log.get_audit("ORD-001")["timeline"]] == \
        ["decision", "override"]


def test_counts_and_action_mix(log):
    log.record_decision(decision_for(**REFUSER))
    log.record_decision(decision_for(**CLEAN))
    log.record_outcome("ORD-001", is_rto=True)

    assert log.counts() == {"decisions": 2, "orders": 2, "overrides": 0, "outcomes": 1}
    assert log.action_mix() == {"nudge": 1, "ship": 1}


def test_review_queue_holds_unhandled_reviews_only(log):
    severe = dict(order_id="ORD-003", payment_method="cod", order_value=12000,
                  category="apparel", completed_orders=5, returned_orders=4,
                  refusals_in_last_3=3, address_quality="severe")
    did = log.record_decision(decision_for(**severe))
    assert [r["order_id"] for r in log.review_queue()] == ["ORD-003"]

    log.record_override(did, Action.SHIP, GOOD_REASON, actor="anushree")
    assert log.review_queue() == [], "a handled review must leave the queue"


def test_review_queue_is_ordered_by_score(log):
    worst = dict(order_id="ORD-BAD", payment_method="cod", order_value=12000,
                 category="apparel", variant_count=3, completed_orders=5,
                 returned_orders=4, refusals_in_last_3=3, address_quality="severe")
    milder = dict(worst, order_id="ORD-MILD", order_value=6000, variant_count=1,
                  address_quality="major_gap")
    log.record_decision(decision_for(**milder))
    log.record_decision(decision_for(**worst))

    queue = log.review_queue()
    assert [r["order_id"] for r in queue] == ["ORD-BAD", "ORD-MILD"]
    assert queue[0]["score"] >= queue[1]["score"]


# --------------------------------------------------------------------------
# Orphan protection
# --------------------------------------------------------------------------
def test_outcome_for_an_unscored_order_is_refused(log):
    """A typo in an order id must not become a permanent orphan row."""
    with pytest.raises(UnknownOrderError, match="check the order id"):
        log.record_outcome("ORD-TYPO", is_rto=True, source="courier")
    assert log.counts()["outcomes"] == 0


def test_the_orphan_guard_can_be_waived_deliberately(log):
    """A merchant back-loading history may genuinely have no decision for it."""
    log.record_outcome("ORD-HISTORIC", is_rto=True, source="backfill",
                       require_known_order=False)
    assert log.counts()["outcomes"] == 1


def test_unknown_order_error_is_still_a_value_error(log):
    """Callers catching ValueError must keep working."""
    assert issubclass(UnknownOrderError, ValueError)
    with pytest.raises(ValueError):
        log.record_outcome("ORD-TYPO", is_rto=True, source="courier")


def test_has_order_tracks_decisions(log):
    assert log.has_order("ORD-001") is False
    log.record_decision(decision_for(**REFUSER))
    assert log.has_order("ORD-001") is True


def test_latest_decision_id_refuses_an_unknown_order(log):
    with pytest.raises(UnknownOrderError, match="nothing to override"):
        log.latest_decision_id("ORD-NOPE")


def test_latest_decision_id_returns_the_most_recent(log):
    first = log.record_decision(decision_for(**REFUSER))
    second = log.record_decision(decision_for(**REFUSER))
    assert first != second
    assert log.latest_decision_id("ORD-001") == second


# --------------------------------------------------------------------------
# Batched writes
# --------------------------------------------------------------------------
def test_batch_commits_everything_in_one_go(log):
    with log.batch():
        for i in range(5):
            log.record_decision(decision_for(**dict(REFUSER, order_id="ORD-%03d" % i)))
    assert log.counts()["decisions"] == 5


def test_a_failed_batch_leaves_no_half_written_trail(log):
    """For an audit log, all-or-nothing beats a partial write."""
    with pytest.raises(ValueError):
        with log.batch():
            log.record_decision(decision_for(**REFUSER))
            log.record_outcome("ORD-NOPE", is_rto=True, source="courier")
    assert log.counts() == {"decisions": 0, "orders": 0, "overrides": 0, "outcomes": 0}


def test_batches_do_not_nest(log):
    with pytest.raises(RuntimeError, match="nested batches"):
        with log.batch():
            with log.batch():
                pass


def test_append_only_still_holds_inside_a_batch(log):
    """Batching changes when we commit, never what is allowed."""
    log.record_decision(decision_for(**REFUSER))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with log.batch():
            log._conn.execute("UPDATE decisions SET score = 0")


def test_batching_survives_being_used_twice(log):
    with log.batch():
        log.record_decision(decision_for(**REFUSER))
    with log.batch():
        log.record_decision(decision_for(**CLEAN))
    assert log.counts()["decisions"] == 2


# --------------------------------------------------------------------------
# Thread safety
# --------------------------------------------------------------------------
def test_a_connection_survives_moving_between_threads(tmp_path):
    """Regression: the dashboard 500ed on load because of this.

    FastAPI runs a generator dependency's setup, the endpoint body and its
    teardown through the threadpool, and those three steps can land on
    different threads. The connection was therefore created on one thread, used
    on another and closed on a third, raising ProgrammingError.

    It only appeared under concurrency - a quiet server reuses one pool thread -
    which is why every existing test missed it: TestClient issues requests
    sequentially. This test moves the connection between threads deliberately.
    """
    db = tmp_path / "threads.db"

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        log = pool.submit(AuditLog, str(db)).result()
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(log.record_decision, decision_for(**REFUSER)).result()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            total = pool.submit(lambda: log.list_orders(limit=1)["total"]).result()
        assert total == 1
    finally:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(log.close).result()


def test_many_readers_at_once_all_succeed(tmp_path):
    """The dashboard fires about a dozen counting queries on page load."""
    db = tmp_path / "concurrent.db"
    with AuditLog(str(db)) as log, log.batch():
        for i in range(40):
            log.record_decision(decision_for(**dict(REFUSER, order_id="ORD-%03d" % i)))

    def read(_):
        reader = AuditLog(str(db))
        try:
            return reader.list_orders(limit=1)["total"]
        finally:
            reader.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        totals = list(pool.map(read, range(12)))
    assert totals == [40] * 12
