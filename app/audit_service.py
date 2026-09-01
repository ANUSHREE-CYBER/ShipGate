"""Append-only audit log for every decision, override and outcome.

CLAUDE.md requires each record to carry the rule version, the fired rules, the
score before and after caps, the recommended action, any merchant override plus
its required reason, and the eventual delivery outcome. All of that lives here.

WHY APPEND-ONLY, AND WHY THE DATABASE ENFORCES IT
-------------------------------------------------
An audit trail you can quietly edit is not an audit trail. So nothing here ever
updates or deletes a row - a correction is a new row, and the old one stays
visible forever. That is enforced by SQLite triggers that ABORT on any UPDATE or
DELETE against the three record tables, not by everyone remembering to be
careful. Someone opening the database by hand and running an UPDATE gets an
error, which is the point.

The practical consequences:

  * Re-assessing an order (say, after the customer fixes their address) appends
    a second decision. Both are kept. The newest is "current"; the older one
    still shows what was decided and why at the time.
  * Correcting an outcome appends a second outcome. The trail shows the
    correction happened, and when, and who said so.
  * An override never replaces the recommendation it overrules. The trail always
    shows both what the system said and what the human did instead.

WHAT IS DELIBERATELY NOT STORED
-------------------------------
No customer names, phone numbers, or full addresses. The log stores an order id,
a merchant id, the scoring inputs' *effects* (which rules fired), and the
decisions taken. The address is represented only by its quality band, which is
what the rule actually used. That keeps the audit trail useful for answering
"why was this order treated this way" without turning it into a second copy of
the customer database.
"""

from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime
import json
import sqlite3

from app.policy_engine import Action, PolicyDecision

AUDIT_SCHEMA_VERSION = "1.0.0"

DEFAULT_DB_PATH = "data/audit.db"

# Mirrors policy_engine.MIN_OVERRIDE_REASON_CHARS. Enforced a second time here,
# at the storage boundary, because "the reason is required" is a promise the
# audit trail makes to whoever reads it later - not just a UI validation.
MIN_OVERRIDE_REASON_CHARS = 10

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
    decision_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id               TEXT    NOT NULL,
    merchant_id            TEXT    NOT NULL,
    decided_at             TEXT    NOT NULL,
    rule_version           TEXT    NOT NULL,
    policy_version         TEXT    NOT NULL,
    raw_score              INTEGER NOT NULL,
    score                  INTEGER NOT NULL,
    evidence_score         INTEGER NOT NULL,
    tier_before_safeguards TEXT    NOT NULL,
    engine_tier            TEXT    NOT NULL,
    policy_tier            TEXT    NOT NULL,
    recommended_action     TEXT    NOT NULL,
    fired_rules            TEXT    NOT NULL,   -- json
    reasons                TEXT    NOT NULL,   -- json
    safeguards_applied     TEXT    NOT NULL,   -- json
    policy_adjustments     TEXT    NOT NULL    -- json
);

CREATE TABLE IF NOT EXISTS overrides (
    override_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id  INTEGER NOT NULL REFERENCES decisions(decision_id),
    order_id     TEXT    NOT NULL,
    overridden_at TEXT   NOT NULL,
    action       TEXT    NOT NULL,
    actor        TEXT    NOT NULL,
    reason       TEXT    NOT NULL CHECK (length(trim(reason)) >= 10)
);

CREATE TABLE IF NOT EXISTS outcomes (
    outcome_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id     TEXT    NOT NULL,
    recorded_at  TEXT    NOT NULL,
    is_rto       INTEGER NOT NULL CHECK (is_rto IN (0, 1)),
    source       TEXT    NOT NULL,
    note         TEXT
);

CREATE INDEX IF NOT EXISTS idx_decisions_order ON decisions(order_id);
CREATE INDEX IF NOT EXISTS idx_overrides_order ON overrides(order_id);
CREATE INDEX IF NOT EXISTS idx_outcomes_order  ON outcomes(order_id);
"""

# The append-only guarantee. Convention is not enough for something the whole
# product claims as a feature, so the database itself refuses.
APPEND_ONLY_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS decisions_no_update BEFORE UPDATE ON decisions
BEGIN SELECT RAISE(ABORT, 'audit log is append-only: decisions cannot be updated'); END;
CREATE TRIGGER IF NOT EXISTS decisions_no_delete BEFORE DELETE ON decisions
BEGIN SELECT RAISE(ABORT, 'audit log is append-only: decisions cannot be deleted'); END;

CREATE TRIGGER IF NOT EXISTS overrides_no_update BEFORE UPDATE ON overrides
BEGIN SELECT RAISE(ABORT, 'audit log is append-only: overrides cannot be updated'); END;
CREATE TRIGGER IF NOT EXISTS overrides_no_delete BEFORE DELETE ON overrides
BEGIN SELECT RAISE(ABORT, 'audit log is append-only: overrides cannot be deleted'); END;

CREATE TRIGGER IF NOT EXISTS outcomes_no_update BEFORE UPDATE ON outcomes
BEGIN SELECT RAISE(ABORT, 'audit log is append-only: outcomes cannot be updated'); END;
CREATE TRIGGER IF NOT EXISTS outcomes_no_delete BEFORE DELETE ON outcomes
BEGIN SELECT RAISE(ABORT, 'audit log is append-only: outcomes cannot be deleted'); END;
"""


class UnknownOrderError(ValueError):
    """An override or outcome arrived for an order that was never scored.

    A subclass of ValueError so existing callers that catch ValueError keep
    working, but distinguishable so the API can answer 404 rather than 400 -
    "I have never heard of this order" is a different problem from "your
    request was malformed".
    """


@dataclass(frozen=True)
class TimelineEvent:
    """One thing that happened to an order, for the audit view."""

    at: str
    kind: str          # "decision" | "override" | "outcome"
    summary: str
    detail: dict


class AuditLog:
    """A SQLite-backed append-only decision log.

    Use as a context manager, or call close() yourself. Pass ":memory:" for a
    throwaway log in tests.
    """

    def __init__(self, path: str = DEFAULT_DB_PATH):
        self.path = path
        self._in_batch = False
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._create_schema()

    # -- lifecycle --------------------------------------------------------
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def batch(self):
        """Group many appends into a single transaction.

        Every record_* call normally commits on its own, which is right for an
        API handling one order at a time - a decision is durable the moment it
        is returned to the caller. It is badly wrong for a bulk load: committing
        20,000 times means 20,000 fsyncs, and the 10,000-order backfill took 107
        seconds. Inside this block it takes about two.

        Nothing about the append-only guarantee changes - the triggers are
        unaffected, and a failure rolls the whole batch back rather than leaving
        the log half-written, which for an audit log is the behaviour you want.
        """
        if self._in_batch:
            raise RuntimeError("already batching - nested batches are not supported")
        self._in_batch = True
        try:
            yield self
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            self._in_batch = False

    def _write(self):
        """Commit per call, unless a batch is holding the transaction open."""
        return nullcontext() if self._in_batch else self._conn

    def _create_schema(self) -> None:
        with self._conn:
            self._conn.executescript(SCHEMA)
            self._conn.executescript(APPEND_ONLY_TRIGGERS)
            self._conn.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
                (AUDIT_SCHEMA_VERSION,),
            )

    @property
    def schema_version(self) -> str:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        return row["value"] if row else None

    # -- writing ----------------------------------------------------------
    def record_decision(self, decision: PolicyDecision,
                        decided_at: datetime = None) -> int:
        """Append one decision. Returns its decision_id.

        Recording the same order twice is allowed and appends a second row -
        an order genuinely can be re-assessed after its address is corrected,
        and the trail should show both assessments.

        If the decision already carries an override, it is appended as its own
        override row so the timeline shows the recommendation and the human
        action as two separate events, in that order.
        """
        record = decision.to_dict()
        at = (decided_at or datetime.now()).isoformat(timespec="seconds")

        with self._write():
            cursor = self._conn.execute(
                """INSERT INTO decisions (
                       order_id, merchant_id, decided_at, rule_version,
                       policy_version, raw_score, score, evidence_score,
                       tier_before_safeguards, engine_tier, policy_tier,
                       recommended_action, fired_rules, reasons,
                       safeguards_applied, policy_adjustments)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (record["order_id"], record["merchant_id"], at,
                 record["rule_version"], record["policy_version"],
                 record["raw_score"], record["score"], record["evidence_score"],
                 record["tier_before_safeguards"], record["engine_tier"],
                 record["policy_tier"], record["recommended_action"],
                 json.dumps(record["fired_rules"]), json.dumps(record["reasons"]),
                 json.dumps(record["safeguards_applied"]),
                 json.dumps(record["policy_adjustments"])),
            )
            decision_id = cursor.lastrowid

        if decision.override is not None:
            self.record_override(
                decision_id=decision_id,
                action=decision.override.action,
                reason=decision.override.reason,
                actor=decision.override.actor,
                at=decision.override.at,
            )
        return decision_id

    def record_override(self, decision_id: int, action: Action, reason: str,
                        actor: str, at: datetime = None) -> int:
        """Append a human overruling a decision. A reason is mandatory."""
        cleaned = (reason or "").strip()
        if len(cleaned) < MIN_OVERRIDE_REASON_CHARS:
            raise ValueError(
                "an override needs a reason of at least %d characters - got %r"
                % (MIN_OVERRIDE_REASON_CHARS, cleaned))
        if not (actor or "").strip():
            raise ValueError("an override must record who made it")
        if not isinstance(action, Action):
            raise ValueError("override action must be an Action, got %r" % (action,))

        row = self._conn.execute(
            "SELECT order_id FROM decisions WHERE decision_id = ?",
            (decision_id,)).fetchone()
        if row is None:
            raise UnknownOrderError("no decision %r to override" % (decision_id,))

        with self._write():
            cursor = self._conn.execute(
                """INSERT INTO overrides
                       (decision_id, order_id, overridden_at, action, actor, reason)
                   VALUES (?,?,?,?,?,?)""",
                (decision_id, row["order_id"],
                 (at or datetime.now()).isoformat(timespec="seconds"),
                 action.label, actor.strip(), cleaned),
            )
            return cursor.lastrowid

    def has_order(self, order_id: str) -> bool:
        """Has this order ever been scored by ShipGate?"""
        return self._conn.execute(
            "SELECT 1 FROM decisions WHERE order_id = ? LIMIT 1",
            (order_id,)).fetchone() is not None

    def record_outcome(self, order_id: str, is_rto: bool, source: str = "merchant",
                       note: str = None, at: datetime = None,
                       require_known_order: bool = True) -> int:
        """Append what actually happened to the parcel.

        A correction is a new row, never an edit. get_audit reports the latest
        as current and keeps the earlier ones visible.

        By default an outcome for an order ShipGate never scored is refused.
        There is no foreign key doing this, deliberately - the check is a
        deliberate policy rather than a schema accident, and it can be waived by
        a caller that genuinely wants to log an outcome for an unscored order
        (a merchant back-loading history, say). But the default is strict,
        because the far more likely cause of an unrecognised order id is a typo,
        and a silent orphan row in an audit log is worse than an error.
        """
        if not (source or "").strip():
            raise ValueError("an outcome must record where it came from")
        if require_known_order and not self.has_order(order_id):
            raise UnknownOrderError(
                "no decision has ever been recorded for order %r, so there is "
                "nothing for this outcome to attach to - check the order id"
                % order_id)
        with self._write():
            cursor = self._conn.execute(
                """INSERT INTO outcomes (order_id, recorded_at, is_rto, source, note)
                   VALUES (?,?,?,?,?)""",
                (order_id, (at or datetime.now()).isoformat(timespec="seconds"),
                 1 if is_rto else 0, source.strip(), note),
            )
            return cursor.lastrowid

    def latest_decision_id(self, order_id: str) -> int:
        """The decision an override should attach to: the most recent one.

        Overrides are addressed by order in the API, because that is what a
        merchant knows. Internally they attach to a specific decision, so that
        overriding an order that is later re-scored does not silently carry the
        override onto the new assessment.
        """
        row = self._conn.execute(
            """SELECT decision_id FROM decisions WHERE order_id = ?
               ORDER BY decision_id DESC LIMIT 1""", (order_id,)).fetchone()
        if row is None:
            raise UnknownOrderError(
                "no decision has ever been recorded for order %r, so there is "
                "nothing to override - check the order id" % order_id)
        return row["decision_id"]

    # -- reading ----------------------------------------------------------
    def get_audit(self, order_id: str) -> dict:
        """The full trail for one order: every decision, override and outcome.

        This is what GET /orders/{id}/audit returns and what the dashboard's
        audit timeline renders.
        """
        decisions = [self._decision_row(r) for r in self._conn.execute(
            "SELECT * FROM decisions WHERE order_id = ? ORDER BY decision_id",
            (order_id,))]
        overrides = [dict(r) for r in self._conn.execute(
            "SELECT * FROM overrides WHERE order_id = ? ORDER BY override_id",
            (order_id,))]
        outcomes = [self._outcome_row(r) for r in self._conn.execute(
            "SELECT * FROM outcomes WHERE order_id = ? ORDER BY outcome_id",
            (order_id,))]

        current_decision = decisions[-1] if decisions else None
        current_override = None
        if current_decision:
            for o in overrides:
                if o["decision_id"] == current_decision["decision_id"]:
                    current_override = o

        final_action = None
        if current_override:
            final_action = current_override["action"]
        elif current_decision:
            final_action = current_decision["recommended_action"]

        return {
            "order_id": order_id,
            "found": bool(decisions),
            "decisions": decisions,
            "overrides": overrides,
            "outcomes": outcomes,
            "current_decision": current_decision,
            "final_action": final_action,
            "current_outcome": outcomes[-1] if outcomes else None,
            "timeline": [vars(e) for e in self._timeline(decisions, overrides, outcomes)],
        }

    def _timeline(self, decisions, overrides, outcomes) -> list:
        events = []
        for d in decisions:
            events.append(TimelineEvent(
                at=d["decided_at"], kind="decision",
                summary="Scored %d (%s) - recommended %s"
                        % (d["score"], d["policy_tier"], d["recommended_action"]),
                detail=d))
        for o in overrides:
            events.append(TimelineEvent(
                at=o["overridden_at"], kind="override",
                summary="%s overrode to %s: %s" % (o["actor"], o["action"], o["reason"]),
                detail=o))
        for o in outcomes:
            events.append(TimelineEvent(
                at=o["recorded_at"], kind="outcome",
                summary="Recorded as %s (source: %s)"
                        % ("RTO" if o["is_rto"] else "delivered", o["source"]),
                detail=o))
        # Stable ordering: by time, then by the order things can happen in.
        rank = {"decision": 0, "override": 1, "outcome": 2}
        return sorted(events, key=lambda e: (e.at, rank[e.kind]))

    @staticmethod
    def _decision_row(row) -> dict:
        record = dict(row)
        for column in ("fired_rules", "reasons", "safeguards_applied",
                       "policy_adjustments"):
            record[column] = json.loads(record[column])
        return record

    @staticmethod
    def _outcome_row(row) -> dict:
        record = dict(row)
        record["is_rto"] = bool(record["is_rto"])
        return record

    # -- summaries --------------------------------------------------------
    def counts(self) -> dict:
        def one(sql):
            return self._conn.execute(sql).fetchone()[0]
        return {
            "decisions": one("SELECT COUNT(*) FROM decisions"),
            "orders": one("SELECT COUNT(DISTINCT order_id) FROM decisions"),
            "overrides": one("SELECT COUNT(*) FROM overrides"),
            "outcomes": one("SELECT COUNT(*) FROM outcomes"),
        }

    def action_mix(self) -> dict:
        rows = self._conn.execute(
            """SELECT recommended_action, COUNT(*) AS n FROM decisions
               GROUP BY recommended_action ORDER BY n DESC""")
        return {r["recommended_action"]: r["n"] for r in rows}

    def list_orders(self, action: str = None, tier: str = None,
                    outcome: str = None, overridden: bool = None,
                    sort: str = "score", limit: int = 50, offset: int = 0) -> dict:
        """The order queue: one row per order, showing its latest state.

        An order can be scored more than once, so this reports only the most
        recent decision for each - the queue answers "where does this order
        stand now", and the full history is one click away in get_audit.

        Returns {"total": n, "items": [...]} so the UI can paginate honestly
        rather than guessing whether there is more.
        """
        if sort not in ("score", "recent"):
            raise ValueError("sort must be 'score' or 'recent', got %r" % (sort,))
        if outcome not in (None, "rto", "delivered", "pending"):
            raise ValueError("outcome must be rto, delivered or pending, got %r"
                             % (outcome,))

        where, params = [], []
        if action:
            where.append("d.recommended_action = ?")
            params.append(action)
        if tier:
            where.append("d.policy_tier = ?")
            params.append(tier)
        if overridden is True:
            where.append("ov.override_id IS NOT NULL")
        elif overridden is False:
            where.append("ov.override_id IS NULL")
        if outcome == "pending":
            where.append("oc.outcome_id IS NULL")
        elif outcome == "rto":
            where.append("oc.is_rto = 1")
        elif outcome == "delivered":
            where.append("oc.is_rto = 0")

        # Latest decision per order, with its override (if any) and the latest
        # outcome for that order.
        base = """
            FROM decisions d
            JOIN (SELECT order_id, MAX(decision_id) AS decision_id
                    FROM decisions GROUP BY order_id) latest
              ON latest.decision_id = d.decision_id
            LEFT JOIN overrides ov ON ov.decision_id = d.decision_id
            LEFT JOIN (SELECT order_id, MAX(outcome_id) AS outcome_id
                         FROM outcomes GROUP BY order_id) lo
              ON lo.order_id = d.order_id
            LEFT JOIN outcomes oc ON oc.outcome_id = lo.outcome_id
        """
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        total = self._conn.execute("SELECT COUNT(*) " + base + clause, params).fetchone()[0]

        order_by = ("d.score DESC, d.decision_id"
                    if sort == "score" else "d.decided_at DESC, d.decision_id DESC")
        rows = self._conn.execute(
            """SELECT d.decision_id, d.order_id, d.merchant_id, d.decided_at,
                      d.score, d.evidence_score, d.policy_tier,
                      d.recommended_action, d.safeguards_applied,
                      ov.action AS override_action, ov.actor AS override_actor,
                      ov.reason AS override_reason,
                      oc.is_rto AS outcome_is_rto, oc.recorded_at AS outcome_at
               """ + base + clause +
            " ORDER BY " + order_by + " LIMIT ? OFFSET ?",
            params + [limit, offset])

        items = []
        for row in rows:
            record = dict(row)
            record["safeguards_applied"] = json.loads(record["safeguards_applied"])
            record["was_overridden"] = record["override_action"] is not None
            record["final_action"] = (record["override_action"]
                                      or record["recommended_action"])
            record["outcome"] = (None if record["outcome_is_rto"] is None
                                 else bool(record["outcome_is_rto"]))
            items.append(record)

        return {"total": total, "limit": limit, "offset": offset, "items": items}

    def review_queue(self, limit: int = 50) -> list:
        """Orders recommended for manual review that nobody has overridden yet."""
        rows = self._conn.execute(
            """SELECT d.* FROM decisions d
               LEFT JOIN overrides o ON o.decision_id = d.decision_id
               WHERE d.recommended_action = 'review' AND o.override_id IS NULL
               ORDER BY d.score DESC, d.decision_id
               LIMIT ?""", (limit,))
        return [self._decision_row(r) for r in rows]


if __name__ == "__main__":
    import os
    from datetime import timedelta

    from app.feature_normalizer import DEFAULT_RESOLUTION_LAG_DAYS, load_dataset
    from app.policy_engine import DEFAULT_POLICY, decide
    from app.rule_engine import assess

    if os.path.exists(DEFAULT_DB_PATH):
        os.remove(DEFAULT_DB_PATH)

    records = load_dataset()
    with AuditLog(DEFAULT_DB_PATH) as log, log.batch():
        for record in records:
            decision = decide(assess(record.features), DEFAULT_POLICY, record.order_id)
            log.record_decision(decision, decided_at=record.timestamp)
            # An outcome is not known at decision time - the parcel has to go
            # out and come back. Stamping it with the decision's own timestamp
            # would make every audit timeline show a decision and its result in
            # the same instant, which is both wrong and would look wrong on the
            # dashboard. Use the same resolution lag the feature replay uses.
            log.record_outcome(
                record.order_id, record.is_rto, source="simulation",
                at=record.timestamp + timedelta(days=DEFAULT_RESOLUTION_LAG_DAYS))

        print("audit schema v%s -> %s" % (log.schema_version, DEFAULT_DB_PATH))
        print("  counts:", log.counts())
        print("  action mix:", log.action_mix())
        queue = log.review_queue(limit=3)
        print("  review queue holds %d orders; top 3 by score:"
              % len(log.review_queue(limit=10_000)))
        for row in queue:
            print("    %s score=%d evidence=%d tier=%s"
                  % (row["order_id"], row["score"], row["evidence_score"],
                     row["policy_tier"]))
