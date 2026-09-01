"""Turns raw order data into scoreable OrderFeatures, without time travel.

The rule engine needs five things that are not in any checkout payload:
completed_orders, returned_orders, refusals_in_last_3, and the two pincode
counts. Every one of them is derived from the OUTCOMES of earlier orders. In
production these come from a database lookup. Offline, they come from replaying
the order history forward in time - which is where leakage would enter if
anywhere in this project, so the rules are strict and stated explicitly.

RULE 1: only earlier orders count.
    Features for an order are built from orders that came before it. Never from
    the order itself, never from anything after it.

RULE 2: only RESOLVED earlier orders count.
    This is the subtle one. A customer orders on Jun-10 and again on Jun-14. At
    checkout on Jun-14 the Jun-10 parcel is still in transit - nobody yet knows
    whether it will be refused. Counting it would be using information that did
    not exist at decision time.

    It matters here specifically: frequent buyers in this dataset order roughly
    every 5.6 days, and Indian COD delivery takes 3-7 days, so the overlap is
    routine rather than rare.

    An order therefore enters history only after RESOLUTION_LAG_DAYS have
    passed. Set the lag to 0 to get the naive timestamp-only behaviour and see
    how much it flatters the results.

RULE 3: failure_mode never enters a feature.
    load_outcomes_csv deliberately discards it and returns only the is_rto
    label. Whether a failure was a refusal or a delivery failure is knowledge
    from after the fact, available to nobody at checkout. Dropping the column at
    the boundary means it cannot be used by accident.

A consequence of Rule 2 worth understanding: a customer with three unresolved
orders in flight looks like a brand new customer to the rule engine, because
completed_orders is 0. That is correct. The merchant genuinely knows nothing
about them yet.
"""

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
import csv

from app.rule_engine import OrderFeatures
# Reused purely as a CSV reader for the visible order columns. Nothing from the
# outcome simulation itself is imported, and none of its coefficients are used.
from app.latent_outcome import load_orders_csv

NORMALIZER_VERSION = "1.0.0"

# How long before an order's fate is known and can inform the next decision.
# A real merchant would take this from their courier's actual delivery SLA.
DEFAULT_RESOLUTION_LAG_DAYS = 5

# The merchant-wide RTO rate the pincode rules are compared against. Early on
# there is not enough resolved history to estimate it, so it is smoothed toward
# an industry prior - the same empirical-Bayes idea the rule engine already uses
# for thin pincodes, applied to the baseline itself.
BASELINE_PRIOR = 0.25
BASELINE_ALPHA = 200.0


@dataclass(frozen=True)
class FeatureRecord:
    """One order, ready to score, with the label and the slicing metadata.

    features is what the rule engine is allowed to see. is_rto is the answer it
    is graded against and must never reach the scorer.
    """

    order_id: str
    timestamp: datetime
    merchant_cohort: str
    payment_method: str
    features: OrderFeatures
    is_rto: bool

    @property
    def is_known_customer(self) -> bool:
        """Known means the merchant had resolved history at decision time."""
        return self.features.completed_orders > 0


class _CustomerState:
    """Resolved history for one customer. Counts only, no order details."""

    __slots__ = ("completed", "returned", "last3")

    def __init__(self):
        self.completed = 0
        self.returned = 0
        self.last3 = deque(maxlen=3)

    def record(self, is_rto: bool) -> None:
        self.completed += 1
        self.returned += int(is_rto)
        self.last3.append(int(is_rto))


def load_outcomes_csv(path: str) -> dict:
    """Read outcomes as order_id -> is_rto.

    failure_mode is read and thrown away on purpose (Rule 3). If it is never
    returned, it can never be turned into a feature by mistake.
    """
    with open(path, newline="", encoding="utf-8") as fh:
        return {row["order_id"]: row["is_rto"] == "1" for row in csv.DictReader(fh)}


def build_features(orders: list, outcomes: dict,
                   resolution_lag_days: float = DEFAULT_RESOLUTION_LAG_DAYS) -> list:
    """Replay orders forward in time, attaching only knowable history.

    Two cursors move through the same order stream: one placing orders, one
    resolving them `resolution_lag_days` later. Before scoring an order, every
    resolution that has already happened is folded into the running state. The
    state therefore only ever contains outcomes that were genuinely known.
    """
    placed = sorted(orders, key=lambda o: (o["timestamp"], o["order_id"]))
    lag = timedelta(days=resolution_lag_days)

    # Same orders, ordered by when their fate became known.
    resolving = sorted(
        ((o["timestamp"] + lag, o) for o in placed),
        key=lambda pair: (pair[0], pair[1]["order_id"]),
    )

    customers = {}
    pincode_total = {}
    pincode_rto = {}
    resolved_total = 0
    resolved_rto = 0
    cursor = 0

    records = []
    for order in placed:
        now = order["timestamp"]

        # Fold in everything that resolved strictly BEFORE this order was placed.
        # The comparison must be strict. With a lag of 0 an order resolves at its
        # own timestamp, and "<=" would fold it into its own feature row - every
        # order would see its own outcome. That showed up as 100% of orders
        # having resolved history, which is impossible: a customer's first order
        # has none by definition.
        while cursor < len(resolving) and resolving[cursor][0] < now:
            _, done = resolving[cursor]
            cursor += 1
            is_rto = outcomes[done["order_id"]]

            state = customers.get(done["customer_id"])
            if state is None:
                state = customers[done["customer_id"]] = _CustomerState()
            state.record(is_rto)

            code = done["pincode"]
            pincode_total[code] = pincode_total.get(code, 0) + 1
            pincode_rto[code] = pincode_rto.get(code, 0) + int(is_rto)

            resolved_total += 1
            resolved_rto += int(is_rto)

        state = customers.get(order["customer_id"])
        code = order["pincode"]
        baseline = ((resolved_rto + BASELINE_ALPHA * BASELINE_PRIOR)
                    / (resolved_total + BASELINE_ALPHA))

        records.append(FeatureRecord(
            order_id=order["order_id"],
            timestamp=now,
            merchant_cohort=order["merchant_cohort"],
            payment_method=order["payment_method"],
            is_rto=outcomes[order["order_id"]],
            features=OrderFeatures(
                order_id=order["order_id"],
                payment_method=order["payment_method"],
                order_value=order["order_value"],
                category=order["category"],
                variant_count=order["variant_count"],
                completed_orders=state.completed if state else 0,
                returned_orders=state.returned if state else 0,
                refusals_in_last_3=sum(state.last3) if state else 0,
                address_quality=order["address_quality"],
                pincode_total_orders=pincode_total.get(code, 0),
                pincode_rto_count=pincode_rto.get(code, 0),
                baseline_rto_rate=baseline,
            ),
        ))

    return records


def load_dataset(orders_path: str = "data/orders.csv",
                 outcomes_path: str = "data/outcomes.csv",
                 resolution_lag_days: float = DEFAULT_RESOLUTION_LAG_DAYS) -> list:
    """Convenience: read both CSVs and return scoreable records in time order."""
    orders = load_orders_csv(orders_path)
    outcomes = load_outcomes_csv(outcomes_path)
    missing = {o["order_id"] for o in orders} - set(outcomes)
    if missing:
        raise ValueError("%d orders have no recorded outcome (e.g. %s)"
                         % (len(missing), sorted(missing)[:3]))
    return build_features(orders, outcomes, resolution_lag_days)


if __name__ == "__main__":
    for lag in (DEFAULT_RESOLUTION_LAG_DAYS, 0):
        records = load_dataset(resolution_lag_days=lag)
        known = [r for r in records if r.is_known_customer]
        with_refusal = [r for r in known if r.features.refusals_in_last_3 > 0]
        print("lag=%-2s  %d records | known customers %d (%.1f%%) | "
              "with a recent refusal %d | mean baseline %.3f"
              % (lag, len(records), len(known), 100.0 * len(known) / len(records),
                 len(with_refusal),
                 sum(r.features.baseline_rto_rate for r in records) / len(records)))
