"""FastAPI wrapper - the five endpoints, and nothing else.

    POST /risk/assess          score an order, return tier + action + reasons
    GET  /orders                the order queue, filtered and paginated
    POST /orders/{id}/outcome  record whether it actually became an RTO
    POST /orders/{id}/override merchant override, requires a reason
    GET  /orders/{id}/audit    full audit trail for one order

GET /orders was added in Step 8. The original design froze the API at four
endpoints, but the dashboard's order queue cannot exist without a way to list
orders - every other endpoint needs an order id you already know. It is a
read-only listing over data the other endpoints already produce, adds no new
behaviour, and the design's endpoint list was updated to match rather than
left to drift.

WHAT THIS SERVICE DOES NOT OWN
------------------------------
ShipGate has no customer database and does not want one. /risk/assess takes the
customer-history counts as INPUTS rather than looking them up, because those
counts belong to the merchant's own order system. That is not a shortcut - it is
the same stance the audit log takes by storing no names, phone numbers or
addresses. ShipGate is a decision layer over signals it is handed; it is not a
second copy of the customer database.

Offline, feature_normalizer.py is what produces those counts from order history,
with the strict rule that only outcomes resolved before the order's timestamp
may be used. A real integration would do the equivalent lookup merchant-side.

ERROR SEMANTICS
---------------
    404  this order has never been scored, so there is nothing to attach to
    422  the request was understood but is not acceptable (reason too short,
         unknown action, malformed field)

An outcome or override for an unrecognised order id is refused rather than
stored. The overwhelmingly likely cause is a typo, and a silent orphan row in an
audit log is worse than an error - the whole value of the log is that what is in
it can be trusted.
"""

from contextlib import asynccontextmanager
from datetime import datetime
import pathlib

from fastapi import Depends, FastAPI, HTTPException, Path, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.audit_service import DEFAULT_DB_PATH, AuditLog, UnknownOrderError
from app.policy_engine import (
    DEFAULT_POLICY,
    GENTLE_POLICY,
    STRICT_POLICY,
    Action,
    MerchantPolicy,
    decide,
)
from app.rule_engine import OrderFeatures, assess

API_VERSION = "1.0.0"

# Named policies a caller may select. A merchant in a real deployment would have
# their own stored configuration; this keeps the demo to the three presets
# already defined and tested in policy_engine.
POLICIES = {p.merchant_id: p for p in (DEFAULT_POLICY, GENTLE_POLICY, STRICT_POLICY)}

_db_path = DEFAULT_DB_PATH


def get_log() -> AuditLog:
    """One short-lived connection per request.

    SQLite connections are not safe to share across threads, and FastAPI runs
    sync endpoints in a threadpool. Opening per request is the simple correct
    answer at this scale; tests override this dependency to point at a temp file.
    """
    log = AuditLog(_db_path)
    try:
        yield log
    finally:
        log.close()


# --------------------------------------------------------------------------
# Request models
# --------------------------------------------------------------------------
class AssessRequest(BaseModel):
    """A checkout payload, already normalized.

    The history and pincode counts are supplied by the caller because they are
    facts about the merchant's own order book. See the module docstring.
    """

    order_id: str = Field(..., min_length=1)
    merchant_id: str = Field("default", description="which stored policy to apply")

    payment_method: str = Field("prepaid", pattern="^(cod|prepaid)$")
    order_value: float = Field(0.0, ge=0)
    category: str = "other"
    variant_count: int = Field(1, ge=1)

    completed_orders: int = Field(0, ge=0, description="resolved past orders")
    returned_orders: int = Field(0, ge=0, description="of those, ended in RTO")
    refusals_in_last_3: int = Field(0, ge=0, le=3)

    address_quality: str = Field(
        "complete", pattern="^(complete|minor_gap|major_gap|severe)$")
    pincode_total_orders: int = Field(0, ge=0)
    pincode_rto_count: int = Field(0, ge=0)
    baseline_rto_rate: float = Field(0.25, gt=0, lt=1)

    def to_features(self) -> OrderFeatures:
        return OrderFeatures(
            order_id=self.order_id,
            payment_method=self.payment_method,
            order_value=self.order_value,
            category=self.category,
            variant_count=self.variant_count,
            completed_orders=self.completed_orders,
            returned_orders=self.returned_orders,
            refusals_in_last_3=self.refusals_in_last_3,
            address_quality=self.address_quality,
            pincode_total_orders=self.pincode_total_orders,
            pincode_rto_count=self.pincode_rto_count,
            baseline_rto_rate=self.baseline_rto_rate,
        )


class OutcomeRequest(BaseModel):
    is_rto: bool = Field(..., description="did the parcel come back")
    source: str = Field("merchant", min_length=1,
                        description="who is reporting this - courier, merchant, ...")
    note: str = None


class OverrideRequest(BaseModel):
    action: str = Field(..., pattern="^(ship|confirm|nudge|review)$")
    reason: str = Field(..., description="why - this is recorded permanently")
    actor: str = Field(..., min_length=1, description="who made the call")


# --------------------------------------------------------------------------
# App
# --------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Touch the database once at startup so the schema and its append-only
    # triggers exist before the first request rather than racing to create them.
    AuditLog(_db_path).close()
    yield


app = FastAPI(
    title="ShipGate",
    version=API_VERSION,
    description=(
        "A merchant-configurable decision-policy layer for COD RTO risk. "
        "Converts risk signals into the least-disruptive action that is "
        "economically justified, with every score, rule, threshold, override "
        "and final delivery outcome recorded in an append-only audit trail. "
        "All bundled data is synthetic: it validates policy logic, not "
        "production accuracy."
    ),
    lifespan=lifespan,
)


def _resolve_policy(merchant_id: str) -> MerchantPolicy:
    policy = POLICIES.get(merchant_id)
    if policy is None:
        raise HTTPException(
            status_code=422,
            detail="unknown merchant policy %r - known policies are %s"
                   % (merchant_id, sorted(POLICIES)),
        )
    return policy


@app.post("/risk/assess", tags=["decisions"])
def assess_order(request: AssessRequest, log: AuditLog = Depends(get_log)) -> dict:
    """Score an order and return the action, the tier, and why.

    The decision is written to the audit log as a side effect - an assessment
    nobody can later account for is not much use to a merchant being asked why
    a customer was treated a certain way.
    """
    policy = _resolve_policy(request.merchant_id)
    decision = decide(assess(request.to_features()), policy, request.order_id)
    decision_id = log.record_decision(decision)

    record = decision.to_dict()
    record["decision_id"] = decision_id
    record["api_version"] = API_VERSION
    record["disclaimer"] = ("Synthetic simulation result - validates policy "
                            "logic, not production accuracy.")
    return record


@app.get("/orders", tags=["decisions"])
def list_orders(
    action: str = Query(None, pattern="^(ship|confirm|nudge|review)$"),
    tier: str = Query(None, pattern="^(low|medium|high|very_high)$"),
    outcome: str = Query(None, pattern="^(rto|delivered|pending)$"),
    overridden: bool = Query(None),
    sort: str = Query("score", pattern="^(score|recent)$"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    log: AuditLog = Depends(get_log),
) -> dict:
    """The order queue: latest state of each order, filtered and paginated.

    One row per order, showing its most recent decision - the queue answers
    "where does this order stand now". The full history is one call away at
    /orders/{id}/audit.
    """
    try:
        page = log.list_orders(action=action, tier=tier, outcome=outcome,
                               overridden=overridden, sort=sort,
                               limit=limit, offset=offset)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    page["api_version"] = API_VERSION
    return page


@app.post("/orders/{order_id}/outcome", tags=["outcomes"])
def record_outcome(request: OutcomeRequest,
                   order_id: str = Path(..., min_length=1),
                   log: AuditLog = Depends(get_log)) -> dict:
    """Record whether the order actually became an RTO.

    Refuses an order id that has never been scored. Recording an outcome against
    an unknown order would create a row that can never be joined to a decision -
    an orphan that quietly degrades the audit log and, worse, would be counted
    in outcome statistics as though it meant something.

    A correction is a new record, not an edit: send a second outcome and both
    stay on the trail.
    """
    try:
        outcome_id = log.record_outcome(
            order_id=order_id, is_rto=request.is_rto,
            source=request.source, note=request.note)
    except UnknownOrderError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    trail = log.get_audit(order_id)
    return {
        "order_id": order_id,
        "outcome_id": outcome_id,
        "recorded": {"is_rto": request.is_rto, "source": request.source,
                     "note": request.note},
        "outcomes_on_record": len(trail["outcomes"]),
        "superseded_earlier_outcome": len(trail["outcomes"]) > 1,
    }


@app.post("/orders/{order_id}/override", tags=["decisions"])
def override_decision(request: OverrideRequest,
                      order_id: str = Path(..., min_length=1),
                      log: AuditLog = Depends(get_log)) -> dict:
    """Overrule the recommendation for an order. A reason is mandatory.

    The override attaches to the order's most recent decision and never erases
    it - the trail keeps showing what ShipGate recommended alongside what the
    human did instead.
    """
    try:
        decision_id = log.latest_decision_id(order_id)
        override_id = log.record_override(
            decision_id=decision_id, action=Action[request.action.upper()],
            reason=request.reason, actor=request.actor)
    except UnknownOrderError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    trail = log.get_audit(order_id)
    return {
        "order_id": order_id,
        "override_id": override_id,
        "recommended_action": trail["current_decision"]["recommended_action"],
        "final_action": trail["final_action"],
        "reason": request.reason,
        "actor": request.actor,
    }


@app.get("/orders/{order_id}/audit", tags=["audit"])
def get_audit(order_id: str = Path(..., min_length=1),
              log: AuditLog = Depends(get_log)) -> dict:
    """Everything that ever happened to one order, in order."""
    trail = log.get_audit(order_id)
    if not trail["found"]:
        raise HTTPException(
            status_code=404,
            detail="no decision has ever been recorded for order %r" % order_id)
    trail["api_version"] = API_VERSION
    trail["disclaimer"] = ("Synthetic simulation result - validates policy "
                           "logic, not production accuracy.")
    return trail


# --------------------------------------------------------------------------
# The built dashboard
# --------------------------------------------------------------------------
# Mounted last so it never shadows an API route. If frontend/dist does not
# exist yet (nobody has run the build), the API still works and only the UI is
# missing - a missing dashboard should not take the service down.
_DIST = pathlib.Path(__file__).resolve().parent.parent / "frontend" / "dist"

if _DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=_DIST / "assets"), name="assets")

    @app.get("/", include_in_schema=False)
    def dashboard():
        return FileResponse(_DIST / "index.html")

    @app.get("/evaluation.json", include_in_schema=False)
    def evaluation_data():
        return FileResponse(_DIST / "evaluation.json")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
