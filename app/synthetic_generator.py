"""ShipGate synthetic order generator - visible checkout data only.

Invents a plausible population of customers, pincodes and orders for a small
Indian D2C seller over a ~90 day window. Every field produced here is something
a checkout would genuinely know at the moment the order is placed.

WHAT THIS FILE MUST NEVER CONTAIN
---------------------------------
No RTO probability. No risk score. No rule weights. Nothing that decides, hints
at, or correlates-by-construction with whether an order eventually fails. That
decision belongs solely to latent_outcome.py, which owns its own coefficients
and its own hidden factors.

The reason is the whole point of the two-file split: if one body of logic both
invents the data and grades it, the evaluation is circular and the metrics mean
nothing. So this module does not import rule_engine, does not import
latent_outcome, and knows only that orders exist - not that they can fail.

Two counts a checkout legitimately knows are included, and are NOT leakage:

  prior_orders_placed - how many orders this customer has placed before. Placing
      an order requires no outcome, so this is visible immediately.
  pincode_tier - metro / tier2 / rural. A property of the address, not of any
      delivery result.

The rule engine's history features (completed_orders, returned_orders,
refusals_in_last_3) and its pincode statistics are deliberately absent: every
one of them is derived from earlier orders' OUTCOMES, which do not exist yet at
generation time. They get attached later, by a chronological replay that walks
forward in time and uses only orders already resolved before each timestamp.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
import csv
import math
import random

GENERATOR_VERSION = "1.0.0"

# Fixed window so runs are reproducible and the data reads as "the last ~90 days".
START_DATE = datetime(2026, 6, 1)
SPAN_DAYS = 90

DEFAULT_SEED = 20260901
DEFAULT_N_ORDERS = 10000
# A seller shipping nationally touches hundreds of pincodes, most of them
# rarely. 300 over ~10k orders averages ~33 orders each, which keeps a real
# population of thin pincodes for the minimum-sample gate to reject.
DEFAULT_N_PINCODES = 300

# --- Merchant cohorts -----------------------------------------------------
# Two profiles for the cohort threshold demo. They differ ONLY in their input
# distributions here - category mix, value scale, COD appetite. Any difference
# in how often their orders actually fail is latent_outcome.py's business.
COHORTS = ("gemstone", "fast_fashion")
COHORT_SHARE = (0.42, 0.58)

# Categories per cohort, with the share of that cohort's orders.
COHORT_CATEGORIES = {
    "gemstone": (
        ("jewellery", 0.42),
        ("gemstone", 0.28),
        ("home_decor", 0.18),
        ("other", 0.12),
    ),
    "fast_fashion": (
        ("apparel", 0.44),
        ("footwear", 0.22),
        ("ethnic_wear", 0.19),
        ("accessories", 0.15),
    ),
}

# Order value as a lognormal in log-INR: (mu, sigma). Medians run from roughly
# INR 550 for accessories to INR 8,000 for loose gemstones, which is the spread
# a small seller in either niche would actually see.
CATEGORY_VALUE = {
    "jewellery": (8.60, 0.75),
    "gemstone": (9.00, 0.80),
    "home_decor": (7.60, 0.70),
    "other": (7.30, 0.70),
    "apparel": (6.90, 0.60),
    "footwear": (7.20, 0.55),
    "ethnic_wear": (7.50, 0.65),
    "accessories": (6.30, 0.60),
}

MIN_ORDER_VALUE = 199

# Categories where a shopper may order two sizes intending to keep one. Kept in
# sync with rule_engine's SIZE_DEPENDENT_CATEGORIES by meaning, not by import -
# this file stays free of any rule-engine dependency.
FIT_UNCERTAIN_CATEGORIES = frozenset({"apparel", "footwear", "ethnic_wear"})

# --- Geography ------------------------------------------------------------
PINCODE_TIERS = ("metro", "tier2", "rural")
PINCODE_TIER_SHARE = (0.30, 0.40, 0.30)

# How many customers a pincode attracts, by tier. Metro pincodes are busier per
# pincode, but COD-heavy D2C selling in India leans tier2 and beyond, so the
# weights stop well short of making this a metro-only dataset. The spread also
# leaves a genuine tail of low-traffic pincodes - exactly the case the rule
# engine's minimum-sample gate exists to handle.
PINCODE_TIER_WEIGHT = {"metro": 4.0, "tier2": 3.0, "rural": 2.2}

# --- Address quality ------------------------------------------------------
ADDRESS_QUALITIES = ("complete", "minor_gap", "major_gap", "severe")

# Base distribution by pincode tier. Rural addresses are likelier to be missing
# a house number or a usable landmark - a data-completeness fact about the
# address, not a judgement about the customer.
ADDRESS_BY_TIER = {
    "metro": (0.78, 0.14, 0.06, 0.02),
    "tier2": (0.68, 0.19, 0.10, 0.03),
    "rural": (0.55, 0.24, 0.16, 0.05),
}

# A returning customer usually reuses a saved, already-corrected address.
ADDRESS_SAVED = (0.95, 0.040, 0.009, 0.001)
ADDRESS_SAVED_MAX_BLEND = 0.65

# --- Repeat-purchase shape ------------------------------------------------
# Most customers order once or twice; a small tail orders many times. That tail
# is what makes customer-history features non-empty later - if every customer
# were a one-timer, no refusal pattern could ever exist to be detected.
REPEAT_COUNTS = (1, 2, 3, 4, 5, 6, 8, 10, 15)
REPEAT_WEIGHTS = (0.42, 0.20, 0.12, 0.08, 0.06, 0.04, 0.035, 0.030, 0.015)

# Spacing between a customer's orders. Cadence is derived from how many orders
# they intend to place rather than drawn independently: a shopper who buys 12
# times in a quarter and one who buys twice are not the same shopper on a
# different schedule, they have different engagement. Treating the two as one
# trait also stops the 90-day window from truncating every frequent buyer down
# to the same few orders. Noise is lognormal around that cadence, median 1.0x.
GAP_SIGMA = 0.55

# Hour-of-day shape: a midday peak and a larger late-evening peak. Used only to
# make timestamps look real. No rule reads the hour - "late-night order" was
# dropped from the rule set as too weak to justify.
HOUR_WEIGHTS = (
    0.008, 0.005, 0.003, 0.002, 0.002, 0.004,  # 00-05
    0.010, 0.018, 0.030, 0.042, 0.055, 0.068,  # 06-11
    0.072, 0.065, 0.052, 0.048, 0.050, 0.058,  # 12-17
    0.070, 0.082, 0.090, 0.085, 0.060, 0.021,  # 18-23
)

# --- COD propensity -------------------------------------------------------
# A logistic model over cohort, order value and pincode tier. This decides how
# the customer chooses to PAY, which is a checkout-time fact. It says nothing
# about whether the order later succeeds.
COD_BASE = {"gemstone": -0.40, "fast_fashion": 0.90}
COD_VALUE_COEF = -0.35          # pricier baskets skew prepaid
COD_VALUE_PIVOT = math.log(1500.0)
COD_TIER_TERM = {"metro": -0.35, "tier2": 0.15, "rural": 0.60}

# Merchants cap the order value they will ship COD at all - the cash exposure on
# a refused high-value parcel is simply not worth it. Real caps run anywhere from
# INR 5,000 to INR 25,000; this sits at the generous end so the rule engine's
# top value band (above INR 10,000) still sees traffic. Without this ceiling the
# lognormal tail happily produces a INR 1 lakh cash-on-delivery gemstone order,
# which no seller would ever have accepted at checkout.
COD_MAX_VALUE = 25000.0


@dataclass(frozen=True)
class Pincode:
    code: str
    tier: str


@dataclass(frozen=True)
class Customer:
    customer_id: str
    merchant_cohort: str
    pincode: Pincode
    intended_orders: int


@dataclass(frozen=True)
class Order:
    """One row of visible checkout data. Deliberately has no outcome field."""

    order_id: str
    customer_id: str
    merchant_cohort: str
    timestamp: datetime
    payment_method: str
    order_value: float
    category: str
    variant_count: int
    address_quality: str
    pincode: str
    pincode_tier: str
    prior_orders_placed: int

    def to_row(self) -> dict:
        return {
            "order_id": self.order_id,
            "customer_id": self.customer_id,
            "merchant_cohort": self.merchant_cohort,
            "timestamp": self.timestamp.isoformat(timespec="seconds"),
            "payment_method": self.payment_method,
            "order_value": "%.2f" % self.order_value,
            "category": self.category,
            "variant_count": self.variant_count,
            "address_quality": self.address_quality,
            "pincode": self.pincode,
            "pincode_tier": self.pincode_tier,
            "prior_orders_placed": self.prior_orders_placed,
        }


CSV_FIELDS = (
    "order_id", "customer_id", "merchant_cohort", "timestamp", "payment_method",
    "order_value", "category", "variant_count", "address_quality", "pincode",
    "pincode_tier", "prior_orders_placed",
)


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def _pick(rng: random.Random, options, weights):
    return rng.choices(list(options), weights=list(weights), k=1)[0]


def _blend(a, b, t: float) -> tuple:
    """Linear blend of two probability vectors; t=0 gives a, t=1 gives b."""
    return tuple((1.0 - t) * x + t * y for x, y in zip(a, b))


# --------------------------------------------------------------------------
# World building
# --------------------------------------------------------------------------
def _build_pincodes(rng: random.Random, n: int) -> list:
    """Distinct 6-digit codes in the real Indian range (first digit 1-8)."""
    seen, out = set(), []
    while len(out) < n:
        code = "%06d" % rng.randint(110000, 855999)
        if code in seen:
            continue
        seen.add(code)
        out.append(Pincode(code=code, tier=_pick(rng, PINCODE_TIERS, PINCODE_TIER_SHARE)))
    return out


def _customer_stream(rng: random.Random, pincodes: list):
    """Yield customers indefinitely.

    The customer count is emergent rather than fixed: the caller draws until it
    has the order volume it asked for. What matters is that volume and the shape
    of the repeat-purchase tail, not a round headcount.
    """
    weights = [PINCODE_TIER_WEIGHT[p.tier] for p in pincodes]
    i = 0
    while True:
        i += 1
        yield Customer(
            customer_id="CUST-%05d" % i,
            merchant_cohort=_pick(rng, COHORTS, COHORT_SHARE),
            pincode=rng.choices(pincodes, weights=weights, k=1)[0],
            intended_orders=_pick(rng, REPEAT_COUNTS, REPEAT_WEIGHTS),
        )


# --------------------------------------------------------------------------
# Per-order fields
# --------------------------------------------------------------------------
def _draw_timestamp(rng: random.Random, day: float) -> datetime:
    hour = _pick(rng, range(24), HOUR_WEIGHTS)
    return (START_DATE + timedelta(days=int(day))).replace(
        hour=hour, minute=rng.randrange(60), second=rng.randrange(60))


def _draw_category(rng: random.Random, cohort: str) -> str:
    pairs = COHORT_CATEGORIES[cohort]
    return _pick(rng, [c for c, _ in pairs], [w for _, w in pairs])


def _draw_order_value(rng: random.Random, category: str) -> float:
    mu, sigma = CATEGORY_VALUE[category]
    value = math.exp(rng.gauss(mu, sigma))
    return float(max(MIN_ORDER_VALUE, round(value / 10.0) * 10))


def _draw_variant_count(rng: random.Random, category: str) -> int:
    if category in FIT_UNCERTAIN_CATEGORIES:
        return _pick(rng, (1, 2, 3, 4), (0.72, 0.18, 0.07, 0.03))
    return _pick(rng, (1, 2, 3), (0.94, 0.05, 0.01))


def _draw_address_quality(rng: random.Random, tier: str, prior_orders: int) -> str:
    dist = ADDRESS_BY_TIER[tier]
    if prior_orders > 0:
        # Confidence in a saved address grows with each repeat order, then flattens.
        blend = min(ADDRESS_SAVED_MAX_BLEND, 0.35 + 0.10 * prior_orders)
        dist = _blend(dist, ADDRESS_SAVED, blend)
    return _pick(rng, ADDRESS_QUALITIES, dist)


def _draw_payment_method(rng: random.Random, cohort: str, value: float, tier: str) -> str:
    if value > COD_MAX_VALUE:
        return "prepaid"
    logit = (COD_BASE[cohort]
             + COD_VALUE_COEF * (math.log(value) - COD_VALUE_PIVOT)
             + COD_TIER_TERM[tier])
    return "cod" if rng.random() < 1.0 / (1.0 + math.exp(-logit)) else "prepaid"


# --------------------------------------------------------------------------
# Generation
# --------------------------------------------------------------------------
def generate(seed: int = DEFAULT_SEED,
             n_orders: int = DEFAULT_N_ORDERS,
             n_pincodes: int = DEFAULT_N_PINCODES) -> list:
    """Return roughly n_orders orders sorted by timestamp, with time-ordered ids.

    A customer's later orders are spaced by lognormal gaps; any that fall past
    the 90-day window are simply not placed, which is why a customer who first
    appears on day 85 shows up as a one-time buyer. Customers are therefore
    drawn until the *placed* order count reaches the target rather than the
    intended count - otherwise the window silently swallows a third of them.
    The final customer may overshoot the target by a few orders.
    """
    rng = random.Random(seed)
    pincodes = _build_pincodes(rng, n_pincodes)

    drafts = []
    for cust in _customer_stream(rng, pincodes):
        if len(drafts) >= n_orders:
            break
        day = rng.uniform(0.0, SPAN_DAYS)
        cadence = SPAN_DAYS / (cust.intended_orders + 1)
        for placed in range(cust.intended_orders):
            if day >= SPAN_DAYS:
                break
            category = _draw_category(rng, cust.merchant_cohort)
            value = _draw_order_value(rng, category)
            drafts.append((
                _draw_timestamp(rng, day),
                cust,
                category,
                value,
                _draw_variant_count(rng, category),
                _draw_address_quality(rng, cust.pincode.tier, placed),
                _draw_payment_method(rng, cust.merchant_cohort, value, cust.pincode.tier),
                placed,
            ))
            day += cadence * math.exp(rng.gauss(0.0, GAP_SIGMA))

    drafts.sort(key=lambda d: d[0])
    return [
        Order(
            order_id="ORD-%06d" % (i + 1),
            customer_id=cust.customer_id,
            merchant_cohort=cust.merchant_cohort,
            timestamp=ts,
            payment_method=payment,
            order_value=value,
            category=category,
            variant_count=variants,
            address_quality=address,
            pincode=cust.pincode.code,
            pincode_tier=cust.pincode.tier,
            prior_orders_placed=placed,
        )
        for i, (ts, cust, category, value, variants, address, payment, placed)
        in enumerate(drafts)
    ]


def write_orders_csv(orders: list, path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(CSV_FIELDS))
        writer.writeheader()
        for order in orders:
            writer.writerow(order.to_row())


if __name__ == "__main__":
    orders = generate()
    write_orders_csv(orders, "data/orders.csv")
    print("generator v%s - wrote %d orders to data/orders.csv"
          % (GENERATOR_VERSION, len(orders)))
