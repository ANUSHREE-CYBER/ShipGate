"""ShipGate latent outcome simulator - decides what ACTUALLY happened.

Reads the visible checkout data produced by the generator and decides, for each
order, whether it was delivered or came back as an RTO. This is the "ground
truth" the rule engine is later graded against.

WHY THIS IS A SEPARATE FILE
---------------------------
If one body of logic both invented the orders and decided their fate, grading
the rule engine against it would be circular - we would be marking our own
homework. So this module is independent by construction:

  * It does not import rule_engine, and never sees a risk score.
  * It does not import synthetic_generator. The two communicate only through a
    flat CSV, read here by column name.
  * It uses its own coefficients and its own functional form - a logistic model
    over two competing failure mechanisms, not an additive point score with
    group caps. The numbers here are NOT the rule engine's numbers rescaled.
  * It has its own random seed, distinct from the generator's.

It also knows things the rule engine can never observe: a hidden per-customer
reliability trait, the true difficulty of each pincode, courier strain, weather
disruption, and irreducible randomness. Those hidden factors are the reason a
perfect score is impossible - which is exactly right. A rule engine that scored
perfectly against this data would mean the simulation was leaking, not that the
rules were good.

TUNING DISCIPLINE
-----------------
The constants below are calibrated so the marginal rates land where published
figures for Indian COD put them (roughly 20-40% RTO on COD). They are NEVER
tuned to make the rule engine look better. If a rule turns out weak against
this data, that is a finding to report and a reason to reconsider the rule -
not a reason to edit this file.

TWO FAILURE MECHANISMS
----------------------
An order fails for one of two quite different reasons, evaluated in the order
they would happen in real life:

  undeliverable - the parcel never reaches the door. Bad address, hard pincode,
      no courier capacity, floods. Applies to prepaid orders too.
  refused - the parcel reaches the door and the customer declines it. Requires
      cash to be handed over, so this is overwhelmingly a COD phenomenon.

Keeping them separate is what produces the COD/prepaid gap honestly, instead of
asserting it with a single coefficient.
"""

from dataclasses import dataclass
from datetime import datetime
import csv
import hashlib
import math
import random
import statistics

LATENT_VERSION = "1.0.0"

# Deliberately unrelated to the generator's seed (20260901): the two modules
# must not share a random stream any more than they share logic.
LATENT_SEED = 771013

# Salts for the per-entity hidden traits. Hashing the id means a customer's
# reliability is stable no matter what order the data is processed in, without
# the generator ever having assigned it.
CUSTOMER_SALT = "shipgate/latent/customer/v1"
PINCODE_SALT = "shipgate/latent/pincode/v1"

# --- Mechanism 1: refusal at the door -------------------------------------
REFUSAL_BASE = -1.56

# Prepaid is already paid for, so there is nothing to refuse at the door. Not
# quite zero - people do occasionally turn a courier away - but close.
PREPAID_REFUSAL_TERM = -3.60

# The hidden reliability trait, standard-normal per customer. Nothing visible at
# checkout correlates with it; it is the single biggest reason RTO is not fully
# predictable from an order payload.
RELIABILITY_COEF = 0.85

# A customer who has already refused is likelier to refuse again. This is the
# real process behind the rule engine's H1/H2 rules - the rules are an attempt
# to detect this, and they only get to see the outcome, never the momentum.
MOMENTUM_COEF = 0.75
MOMENTUM_CAP = 3.0
MOMENTUM_DECAY = 0.55

# More cash to produce at the door, more chance of a change of heart. A smooth
# function of log value - deliberately NOT the rule engine's banded steps.
REFUSAL_VALUE_COEF = 0.22
REFUSAL_VALUE_PIVOT = math.log(1200.0)

# Per-category refusal tendency, on its own scale. Note the three fit-driven
# categories do NOT share one value here, unlike the rule engine's flat bonus
# for the whole set - the rule is an approximation of this, not a copy of it.
CATEGORY_REFUSAL = {
    "apparel": 0.34,
    "footwear": 0.28,
    "ethnic_wear": 0.20,
    "accessories": 0.06,
    "jewellery": 0.12,
    "gemstone": 0.05,
    "home_decor": 0.10,
    "other": 0.00,
}

# Ordering several sizes barely moves pre-shipment refusal at all. Bracketing
# shows up as a POST-delivery return, which is a different problem from RTO.
# The rule engine caps its bracketing points low for this reason; this near-zero
# coefficient is what that caution is guarding against.
REFUSAL_VARIANT_COEF = 0.05

# A first-time buyer is a mild unknown, not a suspect.
REFUSAL_FIRST_ORDER_TERM = 0.18

# --- Mechanism 2: undeliverable -------------------------------------------
UNDELIVERABLE_BASE = -4.05

# A missing house number is a real logistics failure, not a character judgement.
ADDRESS_UNDELIVERABLE = {
    "complete": 0.00,
    "minor_gap": 0.42,
    "major_gap": 1.15,
    "severe": 2.35,
}

# True difficulty of a pincode, standard-normal, hashed from the code itself.
# The rule engine never sees this - it only ever sees a noisy estimate built
# from past outcomes, which is why its D2 rule is smoothed and sample-gated.
PINCODE_DIFFICULTY_COEF = 0.62

# Pincode tier shifts the mean difficulty, on this module's own scale.
PINCODE_TIER_SHIFT = {"metro": -0.30, "tier2": 0.05, "rural": 0.45}

# Courier strain: how stretched delivery capacity was on a given day.
COURIER_COEF = 0.75
COURIER_DAY_SIGMA = 0.55
COURIER_SUNDAY_TERM = 0.65
COURIER_SATURDAY_TERM = 0.25

# Regional disruption windows - floods, strikes, festivals clogging the network.
N_DISRUPTIONS = 5
DISRUPTION_MIN_DAYS = 2
DISRUPTION_MAX_DAYS = 6
DISRUPTION_TERM = 1.30
N_REGIONS = 10

# A COD parcel needs someone present with cash, so it is marginally harder to
# hand over even when the address is fine.
COD_UNDELIVERABLE_TERM = 0.22

CSV_FIELDS = ("order_id", "is_rto", "failure_mode")

ORDER_COLUMNS = (
    "order_id", "customer_id", "merchant_cohort", "timestamp", "payment_method",
    "order_value", "category", "variant_count", "address_quality", "pincode",
    "pincode_tier", "prior_orders_placed",
)


@dataclass(frozen=True)
class Outcome:
    """What actually happened to one order.

    failure_mode is recorded so the evaluation can report which KIND of failure
    the rules catch - detecting a refuser and detecting a bad address are
    different achievements. It is a diagnostic label only.

    WARNING: failure_mode must NEVER be used as a model feature or fed into any
    scoring path. It is knowledge from after the fact, available to nobody at
    checkout time. Only is_rto belongs in the evaluation as a label.
    """

    order_id: str
    is_rto: bool
    failure_mode: str          # "delivered" | "refused" | "undeliverable"

    def to_row(self) -> dict:
        return {
            "order_id": self.order_id,
            "is_rto": 1 if self.is_rto else 0,
            "failure_mode": self.failure_mode,
        }


# --------------------------------------------------------------------------
# Hidden traits
# --------------------------------------------------------------------------
def _hash_unit(key: str, salt: str) -> float:
    """Stable uniform value in [0, 1) for an entity id, from a salted hash."""
    digest = hashlib.blake2b(("%s|%s" % (salt, key)).encode("utf-8"),
                             digest_size=8).digest()
    return int.from_bytes(digest, "big") / float(1 << 64)


_NORMAL = statistics.NormalDist()


def _hash_normal(key: str, salt: str) -> float:
    """Stable standard-normal trait for an entity id."""
    u = min(max(_hash_unit(key, salt), 1e-9), 1.0 - 1e-9)
    return _NORMAL.inv_cdf(u)


def _logistic(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


# --------------------------------------------------------------------------
# The hidden world: courier capacity and disruption
# --------------------------------------------------------------------------
class _World:
    """Precomputed conditions the rule engine can never observe.

    Built once per simulation from this module's own RNG, so the same seed
    always reproduces the same weather and the same bad courier days.
    """

    def __init__(self, rng: random.Random, span_days: int):
        # Day-level courier strain, plus a weekend penalty applied at lookup.
        self.day_strain = [rng.gauss(0.0, COURIER_DAY_SIGMA)
                           for _ in range(span_days + 1)]

        # Disruption windows, each hitting one region for a few days.
        self.disruptions = []
        for _ in range(N_DISRUPTIONS):
            start = rng.randrange(max(1, span_days))
            length = rng.randint(DISRUPTION_MIN_DAYS, DISRUPTION_MAX_DAYS)
            self.disruptions.append((start, start + length,
                                     rng.randrange(N_REGIONS)))

    def courier_strain(self, day: int, weekday: int) -> float:
        strain = self.day_strain[min(day, len(self.day_strain) - 1)]
        if weekday == 6:
            strain += COURIER_SUNDAY_TERM
        elif weekday == 5:
            strain += COURIER_SATURDAY_TERM
        return strain

    def is_disrupted(self, day: int, region: int) -> bool:
        return any(start <= day < end and region == reg
                   for start, end, reg in self.disruptions)


# --------------------------------------------------------------------------
# Simulation
# --------------------------------------------------------------------------
def simulate_outcomes(orders: list, seed: int = LATENT_SEED) -> list:
    """Decide the fate of every order, walking forward in time.

    Processing is strictly chronological and stateful: a customer's refusal
    momentum builds as the simulation passes their earlier orders. That memory
    is private to this function and is never written out - the evaluation only
    ever gets to see the resulting outcome, exactly as a real merchant would.
    """
    if not orders:
        return []

    ordered = sorted(orders, key=lambda o: (o["timestamp"], o["order_id"]))
    first_day = ordered[0]["timestamp"].date()
    span = (ordered[-1]["timestamp"].date() - first_day).days

    rng = random.Random(seed)
    world = _World(rng, span)

    momentum = {}
    results = []

    for order in ordered:
        cid = order["customer_id"]
        pincode = order["pincode"]
        is_cod = order["payment_method"] == "cod"
        day = (order["timestamp"].date() - first_day).days
        weekday = order["timestamp"].weekday()

        # --- Mechanism 2 first: the parcel has to arrive before it can be
        # refused, so an undeliverable order never reaches a refusal decision.
        difficulty = (_hash_normal(pincode, PINCODE_SALT)
                      + PINCODE_TIER_SHIFT.get(order["pincode_tier"], 0.0))
        region = int(_hash_unit(pincode, PINCODE_SALT) * N_REGIONS)

        undeliverable_logit = (
            UNDELIVERABLE_BASE
            + ADDRESS_UNDELIVERABLE.get(order["address_quality"], 0.0)
            + PINCODE_DIFFICULTY_COEF * difficulty
            + COURIER_COEF * world.courier_strain(day, weekday)
            + (DISRUPTION_TERM if world.is_disrupted(day, region) else 0.0)
            + (COD_UNDELIVERABLE_TERM if is_cod else 0.0)
        )

        if rng.random() < _logistic(undeliverable_logit):
            results.append(Outcome(order["order_id"], True, "undeliverable"))
            # Logistics failure is not the customer's doing, so it does not feed
            # refusal momentum. Their address and pincode already carry it.
            continue

        # --- Mechanism 1: the customer is standing at the door.
        refusal_logit = (
            REFUSAL_BASE
            + (0.0 if is_cod else PREPAID_REFUSAL_TERM)
            - RELIABILITY_COEF * _hash_normal(cid, CUSTOMER_SALT)
            + MOMENTUM_COEF * momentum.get(cid, 0.0)
            + REFUSAL_VALUE_COEF * (math.log(order["order_value"])
                                    - REFUSAL_VALUE_PIVOT)
            + CATEGORY_REFUSAL.get(order["category"], 0.0)
            + REFUSAL_VARIANT_COEF * (order["variant_count"] - 1)
            + (REFUSAL_FIRST_ORDER_TERM if order["prior_orders_placed"] == 0
               else 0.0)
        )

        if rng.random() < _logistic(refusal_logit):
            momentum[cid] = min(MOMENTUM_CAP, momentum.get(cid, 0.0) + 1.0)
            results.append(Outcome(order["order_id"], True, "refused"))
        else:
            momentum[cid] = momentum.get(cid, 0.0) * MOMENTUM_DECAY
            results.append(Outcome(order["order_id"], False, "delivered"))

    return results


# --------------------------------------------------------------------------
# CSV in / out
# --------------------------------------------------------------------------
def load_orders_csv(path: str) -> list:
    """Read visible order data by column name.

    Deliberately does not import synthetic_generator: the only contract between
    the two modules is this file's column names.
    """
    with open(path, newline="", encoding="utf-8") as fh:
        rows = []
        for raw in csv.DictReader(fh):
            rows.append({
                "order_id": raw["order_id"],
                "customer_id": raw["customer_id"],
                "merchant_cohort": raw["merchant_cohort"],
                "timestamp": datetime.fromisoformat(raw["timestamp"]),
                "payment_method": raw["payment_method"],
                "order_value": float(raw["order_value"]),
                "category": raw["category"],
                "variant_count": int(raw["variant_count"]),
                "address_quality": raw["address_quality"],
                "pincode": raw["pincode"],
                "pincode_tier": raw["pincode_tier"],
                "prior_orders_placed": int(raw["prior_orders_placed"]),
            })
    return rows


def write_outcomes_csv(outcomes: list, path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(CSV_FIELDS))
        writer.writeheader()
        for outcome in outcomes:
            writer.writerow(outcome.to_row())


if __name__ == "__main__":
    orders = load_orders_csv("data/orders.csv")
    outcomes = simulate_outcomes(orders)
    write_outcomes_csv(outcomes, "data/outcomes.csv")

    by_id = {o["order_id"]: o for o in orders}
    rto = [o for o in outcomes if o.is_rto]
    cod = [o for o in outcomes if by_id[o.order_id]["payment_method"] == "cod"]
    prepaid = [o for o in outcomes if by_id[o.order_id]["payment_method"] != "cod"]

    print("latent v%s - wrote %d outcomes to data/outcomes.csv"
          % (LATENT_VERSION, len(outcomes)))
    print("  overall RTO   %.1f%%" % (100.0 * len(rto) / len(outcomes)))
    print("  COD           %.1f%%  (n=%d)"
          % (100.0 * sum(o.is_rto for o in cod) / len(cod), len(cod)))
    print("  prepaid       %.1f%%  (n=%d)"
          % (100.0 * sum(o.is_rto for o in prepaid) / len(prepaid), len(prepaid)))
    print("  refused       %d" % sum(o.failure_mode == "refused" for o in rto))
    print("  undeliverable %d" % sum(o.failure_mode == "undeliverable" for o in rto))
