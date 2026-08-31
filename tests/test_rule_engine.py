"""Tests for the rule engine.

The safeguard tests are the important ones - they are the difference between a
safeguard that is enforced and a safeguard that is merely described in a README.
"""

import pytest

from app.rule_engine import (
    CAP_CONTEXT,
    CAP_DELIVERABILITY,
    CAP_HISTORY,
    CAP_PAYMENT,
    MIN_PINCODE_SAMPLE,
    RULE_VERSION,
    OrderFeatures,
    Tier,
    assess,
    tier_from_score,
)


def fired_ids(assessment):
    return {h.id for h in assessment.fired_rules}


# --------------------------------------------------------------------------
# Hand-check cases from the plan
# --------------------------------------------------------------------------
def test_clean_prepaid_order_scores_zero():
    a = assess(OrderFeatures(payment_method="prepaid", order_value=800,
                             category="books", completed_orders=8,
                             returned_orders=0))
    assert a.score == 0
    assert a.tier is Tier.LOW


def test_repeat_refuser_reaches_high():
    a = assess(OrderFeatures(payment_method="cod", order_value=3500,
                             category="apparel", completed_orders=3,
                             returned_orders=2, refusals_in_last_3=2))
    assert a.score == 85
    assert a.tier is Tier.HIGH
    assert a.evidence_score == 40
    assert not a.safeguards_applied


def test_context_pile_up_is_demoted_to_medium():
    a = assess(OrderFeatures(payment_method="cod", order_value=2500,
                             category="apparel", variant_count=2,
                             completed_orders=0, address_quality="minor_gap"))
    assert a.score == 64                          # score reported honestly
    assert a.tier_before_safeguards is Tier.HIGH  # what it scored as
    assert a.tier is Tier.MEDIUM                  # what it is allowed to be
    assert "insufficient_evidence_for_high" in a.safeguards_applied


# --------------------------------------------------------------------------
# Group caps
# --------------------------------------------------------------------------
def test_payment_group_cap_binds_exactly_at_worst_case():
    a = assess(OrderFeatures(payment_method="cod", order_value=50000))
    assert a.group_breakdown["payment"]["raw"] == CAP_PAYMENT


def test_history_group_clamps_at_cap():
    # Repeat refusal (40) + return rate >50% (20) = 60 raw, capped to 45.
    a = assess(OrderFeatures(payment_method="cod", order_value=500,
                             completed_orders=10, returned_orders=8,
                             refusals_in_last_3=3))
    assert a.group_breakdown["history"]["raw"] == 60
    assert a.group_breakdown["history"]["capped"] == CAP_HISTORY


def test_context_group_never_exceeds_its_cap():
    a = assess(OrderFeatures(category="apparel", variant_count=99))
    assert a.group_breakdown["context"]["capped"] <= CAP_CONTEXT


def test_deliverability_group_cap_binds_exactly_at_worst_case():
    a = assess(OrderFeatures(address_quality="severe", pincode_total_orders=200,
                             pincode_rto_count=120, baseline_rto_rate=0.25))
    assert a.group_breakdown["deliverability"]["raw"] == CAP_DELIVERABILITY


# --------------------------------------------------------------------------
# Safeguard 1 - weak context alone can never reach High or Very High
# --------------------------------------------------------------------------
def test_no_amount_of_weak_context_can_reach_high():
    """Every non-evidence signal at maximum, and it still stops at Medium."""
    a = assess(OrderFeatures(payment_method="cod", order_value=99999,
                             category="apparel", variant_count=10,
                             completed_orders=0, address_quality="major_gap",
                             pincode_total_orders=500, pincode_rto_count=350,
                             baseline_rto_rate=0.25))
    assert a.score >= 86                # scores into Very High territory
    assert a.evidence_score == 0        # but nothing is about this customer
    assert a.tier is Tier.MEDIUM


def test_high_value_cod_alone_cannot_reach_high():
    """Order value is loss magnitude, not evidence (decision of Aug 31)."""
    a = assess(OrderFeatures(payment_method="cod", order_value=12000,
                             category="apparel", variant_count=3,
                             completed_orders=0))
    assert a.tier_before_safeguards is Tier.HIGH
    assert a.tier is Tier.MEDIUM


def test_real_evidence_is_allowed_through_to_very_high():
    a = assess(OrderFeatures(payment_method="cod", order_value=12000,
                             category="apparel", completed_orders=10,
                             returned_orders=6, refusals_in_last_3=2))
    assert a.score >= 86
    assert a.tier is Tier.VERY_HIGH
    assert not a.safeguards_applied


# --------------------------------------------------------------------------
# Safeguard 2 - trust can never cancel a deliverability problem
# --------------------------------------------------------------------------
@pytest.mark.parametrize("clean_orders", [3, 8, 15, 25, 400])
def test_trust_discount_never_reduces_deliverability(clean_orders):
    trusted = OrderFeatures(payment_method="cod", order_value=1500,
                            completed_orders=clean_orders, returned_orders=0,
                            address_quality="severe")
    stranger = OrderFeatures(payment_method="cod", order_value=1500,
                             completed_orders=0, address_quality="severe")

    a, b = assess(trusted), assess(stranger)
    assert a.group_breakdown["deliverability"]["capped"] == 20
    assert (a.group_breakdown["deliverability"]
            == b.group_breakdown["deliverability"])
    assert a.group_breakdown["history"]["capped"] == 0   # floored, not negative


def test_trusted_customer_with_bad_address_still_gets_verified():
    a = assess(OrderFeatures(payment_method="cod", order_value=1500,
                             completed_orders=25, returned_orders=0,
                             address_quality="severe"))
    assert a.tier is Tier.MEDIUM   # confirmation step to fix the address


def test_trust_discount_is_graduated_and_floored_at_minus_30():
    def discount(n):
        hits = assess(OrderFeatures(completed_orders=n, returned_orders=0)).fired_rules
        return next((h.points for h in hits if h.id == "H5"), 0)

    assert discount(2) == 0      # below the 3-delivery gate
    assert discount(4) == -8
    assert discount(8) == -15
    assert discount(15) == -22
    assert discount(25) == -30
    assert discount(5000) == -30  # floor holds


def test_recent_refusal_disqualifies_trust():
    a = assess(OrderFeatures(completed_orders=30, returned_orders=1,
                             refusals_in_last_3=1))
    assert "H5" not in fired_ids(a)


# --------------------------------------------------------------------------
# Safeguard 3 - pincode risk alone can never force a restrictive action
# --------------------------------------------------------------------------
def test_pincode_alone_cannot_push_low_into_medium():
    base = dict(payment_method="cod", order_value=1500, category="books",
                completed_orders=4, returned_orders=0, baseline_rto_rate=0.25)
    quiet = assess(OrderFeatures(**base))
    risky = assess(OrderFeatures(pincode_total_orders=200,
                                 pincode_rto_count=90, **base))

    assert risky.score > quiet.score                    # it does contribute
    assert risky.tier_before_safeguards is Tier.MEDIUM  # it would have escalated
    assert risky.tier is Tier.LOW                       # but it is not allowed to
    assert "pincode_not_pivotal" in risky.safeguards_applied


def test_pincode_still_counts_alongside_a_real_address_defect():
    a = assess(OrderFeatures(payment_method="cod", order_value=1500,
                             address_quality="major_gap",
                             pincode_total_orders=200, pincode_rto_count=120,
                             baseline_rto_rate=0.25, completed_orders=4))
    assert "D2" in fired_ids(a)
    assert "pincode_not_pivotal" not in a.safeguards_applied


def test_thin_pincode_sample_is_ignored():
    a = assess(OrderFeatures(pincode_total_orders=MIN_PINCODE_SAMPLE - 1,
                             pincode_rto_count=19, baseline_rto_rate=0.25))
    assert "D2" not in fired_ids(a)


def test_pincode_contribution_is_capped_at_10():
    a = assess(OrderFeatures(pincode_total_orders=10000,
                             pincode_rto_count=10000, baseline_rto_rate=0.25))
    assert next(h.points for h in a.fired_rules if h.id == "D2") == 10
    assert a.group_breakdown["deliverability"]["capped"] == 10


# --------------------------------------------------------------------------
# Brief compliance
# --------------------------------------------------------------------------
def test_no_late_night_or_time_of_day_rule_exists():
    """The 'late-night order' rule was dropped from the brief entirely."""
    assert not any(f.startswith(("hour", "timestamp", "ordered_at", "time"))
                   for f in OrderFeatures.__dataclass_fields__)

    a = assess(OrderFeatures(payment_method="cod", order_value=5000,
                             category="apparel", variant_count=3,
                             completed_orders=0, address_quality="severe",
                             pincode_total_orders=200, pincode_rto_count=120))
    assert not any("night" in h.label.lower() or "hour" in h.label.lower()
                   for h in a.fired_rules)


def test_bracketing_is_capped_at_12_regardless_of_variant_count():
    for n in (3, 5, 20, 500):
        hits = assess(OrderFeatures(variant_count=n)).fired_rules
        assert next(h.points for h in hits if h.id == "C2") == 12


def test_return_rate_needs_a_usable_denominator():
    """1 of 2 returned is 50%, but 2 orders is not evidence of anything."""
    thin = assess(OrderFeatures(completed_orders=2, returned_orders=1))
    thick = assess(OrderFeatures(completed_orders=10, returned_orders=6))
    assert "H3" not in fired_ids(thin)
    assert "H3" in fired_ids(thick)


def test_every_assessment_carries_the_rule_version():
    a = assess(OrderFeatures())
    assert a.rule_version == RULE_VERSION
    assert a.to_dict()["rule_version"] == RULE_VERSION


def test_tier_boundaries_match_the_brief():
    assert tier_from_score(30) is Tier.LOW
    assert tier_from_score(31) is Tier.MEDIUM
    assert tier_from_score(60) is Tier.MEDIUM
    assert tier_from_score(61) is Tier.HIGH
    assert tier_from_score(85) is Tier.HIGH
    assert tier_from_score(86) is Tier.VERY_HIGH


def test_assessment_serializes_for_the_audit_log():
    a = assess(OrderFeatures(payment_method="cod", order_value=3000,
                             category="apparel", completed_orders=3,
                             returned_orders=2, refusals_in_last_3=2))
    d = a.to_dict()
    assert d["raw_score"] and d["score"] and d["tier"] == "high"
    assert d["fired_rules"][0]["id"] == "P1"
    assert set(d["group_breakdown"]) == {
        "payment", "history", "context", "deliverability"}
