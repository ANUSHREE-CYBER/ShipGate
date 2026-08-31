"""ShipGate rule engine - grouped, capped COD RTO risk scoring.

Scores a single order across four independent groups, each clamped to its own
ceiling, and returns a full trace of which rules fired and why. The score is
explainable by construction: no learned weights, no opaque terms, nothing that
needs SHAP to read.

Three hard safeguards are enforced structurally, not by convention:

  1. Weak contextual signals alone can never reach High or Very High tier.
  2. A trusted-customer discount can never cancel a deliverability problem.
  3. Pincode risk alone can never force a restrictive action.

Mapping a tier to an action (ship / confirm / nudge / review) is NOT done here.
That belongs to policy_engine.py, so scoring and policy stay separable.
"""

from dataclasses import dataclass, field
from enum import IntEnum

RULE_VERSION = "1.0.0"

# --- Group ceilings -------------------------------------------------------
# No single group can dominate the total. A pile-up inside one group saturates
# rather than escalating without limit.
CAP_PAYMENT = 45
CAP_HISTORY = 45
CAP_CONTEXT = 25
CAP_DELIVERABILITY = 30

GROUP_CAPS = {
    "payment": CAP_PAYMENT,
    "history": CAP_HISTORY,
    "context": CAP_CONTEXT,
    "deliverability": CAP_DELIVERABILITY,
}

# --- Safeguard thresholds -------------------------------------------------
# "Evidence" means a signal about this customer's own observed behaviour, or a
# severe defect in this order's address. Never category, cart shape, or area
# statistics - and never order value, which is loss magnitude, not evidence.
MIN_EVIDENCE_FOR_HIGH = 18       # one real refusal, or a severe address defect
MIN_EVIDENCE_FOR_VERY_HIGH = 35  # a genuine repeat-refusal pattern

# --- Pincode smoothing ----------------------------------------------------
MIN_PINCODE_SAMPLE = 20   # below this the area statistic is not trustworthy
PINCODE_ALPHA = 20.0      # prior strength; pulls thin pincodes toward baseline

# Categories where fit uncertainty raises refusal-at-door.
SIZE_DEPENDENT_CATEGORIES = frozenset({"apparel", "footwear", "ethnic_wear"})


class Tier(IntEnum):
    LOW = 0
    MEDIUM = 1
    HIGH = 2
    VERY_HIGH = 3

    @property
    def label(self) -> str:
        return {0: "low", 1: "medium", 2: "high", 3: "very_high"}[self.value]


def tier_from_score(score: int) -> Tier:
    if score <= 30:
        return Tier.LOW
    if score <= 60:
        return Tier.MEDIUM
    if score <= 85:
        return Tier.HIGH
    return Tier.VERY_HIGH


@dataclass(frozen=True)
class OrderFeatures:
    """Normalized order input. feature_normalizer.py will produce these later.

    Every history count must be computed from events strictly BEFORE this
    order's timestamp - the chronological evaluation in Step 4 depends on it.
    """

    order_id: str = "unknown"

    # Payment exposure
    payment_method: str = "prepaid"      # "cod" | "prepaid"
    order_value: float = 0.0             # INR

    # Order context
    category: str = "other"
    variant_count: int = 1               # distinct sizes/variants of same product

    # Customer history (all counts strictly prior to this order)
    completed_orders: int = 0            # resolved past orders
    returned_orders: int = 0             # of those, ended in RTO/return
    refusals_in_last_3: int = 0          # 0..3

    # Deliverability
    address_quality: str = "complete"    # complete | minor_gap | major_gap | severe
    pincode_total_orders: int = 0
    pincode_rto_count: int = 0
    baseline_rto_rate: float = 0.25      # merchant-wide prior

    @property
    def is_cod(self) -> bool:
        return self.payment_method.lower() == "cod"

    @property
    def clean_deliveries(self) -> int:
        return max(0, self.completed_orders - self.returned_orders)

    @property
    def return_rate(self) -> float:
        if self.completed_orders == 0:
            return 0.0
        return self.returned_orders / self.completed_orders


@dataclass(frozen=True)
class RuleHit:
    id: str
    label: str
    group: str
    points: int
    evidence: bool
    detail: str


@dataclass
class RiskAssessment:
    rule_version: str
    raw_score: int                  # sum of group subtotals BEFORE caps
    score: int                      # sum AFTER per-group caps
    group_breakdown: dict
    fired_rules: list
    evidence_score: int
    tier: Tier
    tier_before_safeguards: Tier
    safeguards_applied: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "rule_version": self.rule_version,
            "raw_score": self.raw_score,
            "score": self.score,
            "group_breakdown": self.group_breakdown,
            "fired_rules": [vars(r) for r in self.fired_rules],
            "evidence_score": self.evidence_score,
            "tier": self.tier.label,
            "tier_before_safeguards": self.tier_before_safeguards.label,
            "safeguards_applied": list(self.safeguards_applied),
        }


# --------------------------------------------------------------------------
# Group 1: payment exposure (cap 45)
# --------------------------------------------------------------------------
def _payment_rules(f: OrderFeatures) -> list:
    """COD is the precondition for refusal-RTO. Prepaid contributes nothing."""
    if not f.is_cod:
        return []

    hits = [RuleHit(
        id="P1", label="COD order", group="payment", points=25, evidence=False,
        detail="Cash on delivery - refusal at the door is possible.",
    )]

    # Tiered, not additive: INR 2,100 and INR 15,000 are not the same exposure.
    v = f.order_value
    if v > 10000:
        pts, band = 20, "above INR 10,000"
    elif v > 5000:
        pts, band = 15, "INR 5,001-10,000"
    elif v > 2000:
        pts, band = 10, "INR 2,001-5,000"
    else:
        pts, band = 0, ""

    if pts:
        hits.append(RuleHit(
            id="P2", label="High-value COD", group="payment", points=pts,
            evidence=False,
            detail="Order value %.0f (%s) - larger loss if it returns." % (v, band),
        ))
    return hits


# --------------------------------------------------------------------------
# Group 2: customer history (cap 45, floor 0)
# --------------------------------------------------------------------------
def _history_rules(f: OrderFeatures) -> list:
    """The only group carrying real behavioural evidence, and the only one that
    can go negative, via the trusted-customer discount."""
    hits = []

    # H4: new customer. Mutually exclusive with every other history rule.
    if f.completed_orders == 0:
        return [RuleHit(
            id="H4", label="New customer", group="history", points=5,
            evidence=False,
            detail="No delivery history yet - small uncertainty, not suspicion.",
        )]

    # H1 / H2: recent refusals. Mutually exclusive tiers.
    if f.refusals_in_last_3 >= 2:
        hits.append(RuleHit(
            id="H1", label="Repeat refusal", group="history", points=40,
            evidence=True,
            detail="%d of last 3 orders refused or returned." % f.refusals_in_last_3,
        ))
    elif f.refusals_in_last_3 == 1:
        hits.append(RuleHit(
            id="H2", label="One recent refusal", group="history", points=18,
            evidence=True,
            detail="1 of last 3 orders refused - an incident, not yet a pattern.",
        ))

    # H3: overall return rate, gated on a usable denominator so that
    # "1 of 2 returned = 50%" can never fire.
    if f.completed_orders >= 5 and f.return_rate > 0.30:
        pts = 20 if f.return_rate > 0.50 else 12
        hits.append(RuleHit(
            id="H3", label="High return rate", group="history", points=pts,
            evidence=True,
            detail="%.0f%% of %d completed orders returned." % (
                f.return_rate * 100, f.completed_orders),
        ))

    # H5: trusted-customer discount - graduated, capped at -30, and disqualified
    # by any recent refusal. You cannot be trusted and a recent refuser at once.
    if f.clean_deliveries >= 3 and f.refusals_in_last_3 == 0:
        n = f.clean_deliveries
        if n >= 21:
            pts = -30
        elif n >= 11:
            pts = -22
        elif n >= 6:
            pts = -15
        else:
            pts = -8
        hits.append(RuleHit(
            id="H5", label="Trusted customer", group="history", points=pts,
            evidence=False,
            detail="%d clean completed deliveries, no recent refusal." % n,
        ))

    return hits


# --------------------------------------------------------------------------
# Group 3: order context (cap 25) - weak signals only, by design
# --------------------------------------------------------------------------
def _context_rules(f: OrderFeatures) -> list:
    hits = []

    if f.category.lower() in SIZE_DEPENDENT_CATEGORIES:
        hits.append(RuleHit(
            id="C1", label="Size-dependent category", group="context", points=10,
            evidence=False,
            detail="Category '%s' depends on fit - mild refusal uncertainty." % f.category,
        ))

    # Bracketing is hard-capped at 12 no matter how many variants are ordered.
    # It relates more to post-delivery returns than to pre-shipment RTO, so it
    # is never allowed to look like strong evidence.
    if f.variant_count >= 3:
        hits.append(RuleHit(
            id="C2", label="Variant bracketing", group="context", points=12,
            evidence=False,
            detail="%d sizes/variants ordered (capped at 12 pts)." % f.variant_count,
        ))
    elif f.variant_count == 2:
        hits.append(RuleHit(
            id="C2", label="Variant bracketing", group="context", points=8,
            evidence=False,
            detail="2 sizes/variants ordered.",
        ))

    return hits


# --------------------------------------------------------------------------
# Group 4: deliverability (cap 30)
# --------------------------------------------------------------------------
def _smoothed_pincode_rate(f: OrderFeatures) -> float:
    """Empirical-Bayes smoothing: thin pincodes are pulled toward the baseline
    instead of being judged on a handful of orders."""
    return ((f.pincode_rto_count + PINCODE_ALPHA * f.baseline_rto_rate)
            / (f.pincode_total_orders + PINCODE_ALPHA))


def _deliverability_rules(f: OrderFeatures) -> list:
    hits = []

    # D1: address completeness. This is a fixable data problem - the language
    # stays "needs verification", never "risky customer". Worst tier only.
    quality = f.address_quality.lower()
    if quality == "severe":
        hits.append(RuleHit(
            id="D1", label="Address needs verification (severe)",
            group="deliverability", points=20, evidence=True,
            detail="Address unparseable, or pincode missing/invalid.",
        ))
    elif quality == "major_gap":
        hits.append(RuleHit(
            id="D1", label="Address needs verification (major)",
            group="deliverability", points=14, evidence=False,
            detail="House or street number missing.",
        ))
    elif quality == "minor_gap":
        hits.append(RuleHit(
            id="D1", label="Address needs verification (minor)",
            group="deliverability", points=6, evidence=False,
            detail="Landmark or floor detail missing.",
        ))

    # D2: area-level pincode risk - smoothed, min-sample gated, capped at 10,
    # and blocked from ever being the pivotal rule (Safeguard 3, in assess()).
    if f.pincode_total_orders >= MIN_PINCODE_SAMPLE and f.baseline_rto_rate > 0:
        ratio = _smoothed_pincode_rate(f) / f.baseline_rto_rate
        pts = 10 if ratio > 2.0 else (6 if ratio > 1.5 else 0)
        if pts:
            hits.append(RuleHit(
                id="D2", label="Elevated pincode RTO rate",
                group="deliverability", points=pts, evidence=False,
                detail="Smoothed area RTO rate is %.1fx baseline over %d orders." % (
                    ratio, f.pincode_total_orders),
            ))

    return hits


# --------------------------------------------------------------------------
# Assessment
# --------------------------------------------------------------------------
def _clamp(value: int, cap: int) -> int:
    """Floor at 0, ceiling at the group cap.

    The floor is what makes Safeguard 2 structural: a negative history subtotal
    can reach 0 and go no further, so it is arithmetically unable to subtract
    from any other group.
    """
    return max(0, min(value, cap))


def assess(f: OrderFeatures) -> RiskAssessment:
    groups = {
        "payment": _payment_rules(f),
        "history": _history_rules(f),
        "context": _context_rules(f),
        "deliverability": _deliverability_rules(f),
    }

    breakdown, score, raw_score = {}, 0, 0
    for name, hits in groups.items():
        subtotal = sum(h.points for h in hits)
        capped = _clamp(subtotal, GROUP_CAPS[name])
        breakdown[name] = {"raw": subtotal, "capped": capped,
                           "cap": GROUP_CAPS[name]}
        raw_score += subtotal
        score += capped

    fired = [h for hits in groups.values() for h in hits]
    evidence_score = sum(h.points for h in fired if h.evidence)

    # Safeguard 2 (assertion): the trust discount lives in the history group and
    # groups are summed independently, so no negative points can reach the
    # deliverability subtotal. Verify rather than assume.
    deliverability_subtotal = sum(h.points for h in groups["deliverability"])
    assert breakdown["deliverability"]["raw"] == deliverability_subtotal, (
        "deliverability subtotal was modified outside its own group"
    )

    tier = tier_from_score(score)
    tier_before = tier
    safeguards = []

    # Safeguard 3: pincode risk alone can never force a restrictive action. It
    # may contribute alongside a real address defect or refusal history, but it
    # can never be the single rule that pushes an order into a harsher tier.
    pincode_points = sum(h.points for h in groups["deliverability"] if h.id == "D2")
    if pincode_points:
        tier_without = tier_from_score(score - pincode_points)
        if (tier > tier_without
                and pincode_points == deliverability_subtotal
                and evidence_score == 0):
            tier = tier_without
            safeguards.append("pincode_not_pivotal")

    # Safeguard 1: weak contextual signals alone can never reach High or Very
    # High. The score is reported honestly and left untouched - only the tier is
    # demoted, and the demotion is recorded for the audit trail.
    if tier >= Tier.VERY_HIGH and evidence_score < MIN_EVIDENCE_FOR_VERY_HIGH:
        tier = Tier.HIGH
        safeguards.append("insufficient_evidence_for_very_high")
    if tier >= Tier.HIGH and evidence_score < MIN_EVIDENCE_FOR_HIGH:
        tier = Tier.MEDIUM
        safeguards.append("insufficient_evidence_for_high")

    return RiskAssessment(
        rule_version=RULE_VERSION,
        raw_score=raw_score,
        score=score,
        group_breakdown=breakdown,
        fired_rules=fired,
        evidence_score=evidence_score,
        tier=tier,
        tier_before_safeguards=tier_before,
        safeguards_applied=safeguards,
    )
