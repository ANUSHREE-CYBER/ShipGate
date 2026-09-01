"""Tests for the policy engine.

The load-bearing tests here are the ones proving a merchant cannot configure
their way out of a safeguard. "Merchant-configurable" is in the locked pitch, so
if configuration could switch off the evidence gate or the pincode rule, every
guarantee in the README would be void for anyone who edited a config file.

test_pincode_safeguard_survives_the_policy_layer is a regression test for a real
bug: the first version of decide() re-applied only the evidence gate, and
escalated four clean customers in high-RTO pincodes from "ship normally" back to
"confirm" - undoing safeguard 3 for precisely the case it exists to handle.
"""

from datetime import datetime

import pytest

from app.policy_engine import (
    DEFAULT_POLICY,
    DEFAULT_TIER_ACTIONS,
    GENTLE_POLICY,
    MIN_OVERRIDE_REASON_CHARS,
    POLICY_VERSION,
    STRICT_POLICY,
    Action,
    MerchantPolicy,
    apply_override,
    decide,
    tier_for_policy,
)
from app.rule_engine import OrderFeatures, Tier, assess


def assessment(**kwargs):
    return assess(OrderFeatures(**kwargs))


# An order whose ONLY deliverability signal is an elevated pincode rate, sitting
# exactly one band above where it would be without that rate. P1 (+25) plus D2
# (+6) is 31, which is Medium; strip D2 and 25 is Low.
PINCODE_PIVOTAL = dict(
    order_id="ORD-PIN", payment_method="cod", order_value=1500,
    category="books", variant_count=1,
    completed_orders=1, returned_orders=0, refusals_in_last_3=0,
    address_quality="complete",
    pincode_total_orders=200, pincode_rto_count=80, baseline_rto_rate=0.25,
)

REPEAT_REFUSER = dict(
    order_id="ORD-REF", payment_method="cod", order_value=3500,
    category="apparel", completed_orders=3, returned_orders=2,
    refusals_in_last_3=2, address_quality="complete",
)


# --------------------------------------------------------------------------
# Configuration cannot escape a safeguard
# --------------------------------------------------------------------------
def test_pincode_safeguard_survives_the_policy_layer():
    """A bad pincode alone must not buy a clean customer an extra hoop.

    Regression test. This exact order was one of four the first policy engine
    silently escalated back to "confirm".
    """
    a = assessment(**PINCODE_PIVOTAL)
    assert "pincode_not_pivotal" in a.safeguards_applied, "fixture stopped exercising the rule"
    assert a.tier is Tier.LOW

    d = decide(a, DEFAULT_POLICY)
    assert d.policy_tier is Tier.LOW
    assert d.recommended_action is Action.SHIP
    assert "pincode_not_pivotal" in d.policy_adjustments


def test_default_policy_reproduces_the_engine_tier_exactly():
    """With default thresholds the two must never disagree on any real order."""
    from app.feature_normalizer import load_dataset

    for record in load_dataset():
        a = assess(record.features)
        d = decide(a, DEFAULT_POLICY, record.order_id)
        assert d.policy_tier is a.tier, (
            "policy and engine disagreed on %s: %s vs %s"
            % (record.order_id, d.policy_tier.label, a.tier.label)
        )


def test_lowering_thresholds_cannot_manufacture_evidence():
    """A merchant who wants to be strict still cannot reach a harsh tier
    on an order with no evidence behind it."""
    reckless = MerchantPolicy(merchant_id="reckless", thresholds=(5, 10, 15))
    a = assessment(payment_method="cod", order_value=3000, category="apparel",
                   variant_count=3, completed_orders=2, returned_orders=0)
    assert a.evidence_score == 0

    d = decide(a, reckless)
    assert d.policy_tier <= Tier.MEDIUM
    assert d.recommended_action <= Action.CONFIRM
    assert any("insufficient_evidence" in adj for adj in d.policy_adjustments)


def test_lowering_thresholds_does_let_a_merchant_act_sooner_with_evidence():
    """The gate blocks unevidenced escalation, not configuration itself."""
    a = assessment(**REPEAT_REFUSER)
    assert a.evidence_score >= 35

    default = decide(a, DEFAULT_POLICY)
    strict = decide(a, STRICT_POLICY)
    assert strict.policy_tier >= default.policy_tier
    assert strict.recommended_action >= default.recommended_action


def test_max_action_caps_the_harshest_response():
    a = assessment(**REPEAT_REFUSER)
    capped = MerchantPolicy(merchant_id="capped", max_action=Action.CONFIRM)
    d = decide(a, capped)
    assert d.recommended_action is Action.CONFIRM
    assert any(adj.startswith("capped_at_") for adj in d.policy_adjustments)


def test_there_is_no_block_action_to_configure():
    """The product refuses to offer outright blocking, structurally."""
    assert [a.label for a in Action] == ["ship", "confirm", "nudge", "review"]
    assert not hasattr(Action, "BLOCK")
    assert max(Action) is Action.REVIEW


# --------------------------------------------------------------------------
# Configuration validation
# --------------------------------------------------------------------------
@pytest.mark.parametrize("thresholds", [(61, 31, 86), (31, 31, 86), (0, 10, 20),
                                        (31, 61), (31, 61, 86, 99)])
def test_bad_thresholds_are_refused(thresholds):
    with pytest.raises(ValueError):
        MerchantPolicy(thresholds=thresholds)


def test_actions_may_not_get_gentler_as_risk_rises():
    with pytest.raises(ValueError, match="gentler"):
        MerchantPolicy(tier_actions={
            Tier.LOW: Action.SHIP, Tier.MEDIUM: Action.REVIEW,
            Tier.HIGH: Action.CONFIRM, Tier.VERY_HIGH: Action.REVIEW})


def test_low_tier_must_ship():
    with pytest.raises(ValueError, match="Low tier must ship"):
        MerchantPolicy(tier_actions={
            Tier.LOW: Action.CONFIRM, Tier.MEDIUM: Action.CONFIRM,
            Tier.HIGH: Action.NUDGE, Tier.VERY_HIGH: Action.REVIEW})


def test_missing_tier_mapping_is_refused():
    with pytest.raises(ValueError, match="missing"):
        MerchantPolicy(tier_actions={Tier.LOW: Action.SHIP})


def test_tier_for_policy_respects_custom_boundaries():
    assert tier_for_policy(30, (31, 61, 86)) is Tier.LOW
    assert tier_for_policy(31, (31, 61, 86)) is Tier.MEDIUM
    assert tier_for_policy(30, (21, 51, 76)) is Tier.MEDIUM
    assert tier_for_policy(100, (21, 51, 76)) is Tier.VERY_HIGH


def test_gentle_policy_never_reaches_review():
    from app.feature_normalizer import load_dataset

    for record in load_dataset():
        d = decide(assess(record.features), GENTLE_POLICY, record.order_id)
        assert d.recommended_action <= Action.NUDGE


# --------------------------------------------------------------------------
# Overrides
# --------------------------------------------------------------------------
def test_override_requires_a_real_reason():
    d = decide(assessment(**REPEAT_REFUSER))
    for bad in ("", "   ", "ok", "fine", "x" * (MIN_OVERRIDE_REASON_CHARS - 1)):
        with pytest.raises(ValueError, match="reason"):
            apply_override(d, Action.SHIP, bad, actor="anushree")


def test_override_requires_an_actor():
    d = decide(assessment(**REPEAT_REFUSER))
    with pytest.raises(ValueError, match="who made it"):
        apply_override(d, Action.SHIP, "known good wholesale buyer", actor="  ")


def test_override_rejects_a_non_action():
    d = decide(assessment(**REPEAT_REFUSER))
    with pytest.raises(ValueError, match="must be an Action"):
        apply_override(d, "ship", "known good wholesale buyer", actor="anushree")


def test_override_never_erases_the_recommendation():
    """The audit trail must show both what we said and what the human did."""
    d = decide(assessment(**REPEAT_REFUSER))
    original = d.recommended_action
    overridden = apply_override(d, Action.SHIP, "regular wholesale buyer, verified by phone",
                                actor="anushree", at=datetime(2026, 9, 1, 12, 0))

    assert overridden.recommended_action is original
    assert overridden.final_action is Action.SHIP
    assert overridden.was_overridden
    assert overridden.override.reason == "regular wholesale buyer, verified by phone"
    assert overridden.override.actor == "anushree"
    assert not d.was_overridden, "the original decision must not be mutated"


def test_final_action_is_the_recommendation_without_an_override():
    d = decide(assessment(**REPEAT_REFUSER))
    assert d.final_action is d.recommended_action
    assert not d.was_overridden


# --------------------------------------------------------------------------
# The audit record
# --------------------------------------------------------------------------
def test_decision_dict_carries_everything_the_audit_trail_needs():
    d = apply_override(decide(assessment(**REPEAT_REFUSER)), Action.SHIP,
                       "verified by phone, shipping as normal", actor="anushree")
    record = d.to_dict()

    for required in ("order_id", "rule_version", "policy_version", "merchant_id",
                     "raw_score", "score", "evidence_score",
                     "tier_before_safeguards", "engine_tier", "policy_tier",
                     "recommended_action", "final_action", "was_overridden",
                     "override", "reasons", "fired_rules", "safeguards_applied",
                     "policy_adjustments"):
        assert required in record, "audit record is missing %s" % required

    assert record["policy_version"] == POLICY_VERSION
    assert record["override"]["reason"] == "verified by phone, shipping as normal"
    assert record["fired_rules"], "fired rules must be carried through for the drawer"


def test_reasons_are_written_for_a_human():
    d = decide(assessment(**REPEAT_REFUSER))
    blob = " ".join(d.reasons).lower()
    assert "repeat refusal" in blob
    assert "evidence score" in blob
    assert d.recommended_action.label in blob


def test_deciding_twice_gives_the_same_answer():
    a = assessment(**REPEAT_REFUSER)
    first, second = decide(a, DEFAULT_POLICY), decide(a, DEFAULT_POLICY)
    assert first == second
