"""Turns a risk assessment into the least-disruptive action that fits.

The rule engine stops at a tier on purpose. Deciding what to DO with that tier
is a merchant's business decision, not a property of the score, and the two are
kept in separate files so that a merchant can change their policy without
anybody touching the scoring logic.

THE FOUR ACTIONS, AND THE ONE THAT DOES NOT EXIST
-------------------------------------------------
    ship    - nothing happens, the order goes out as placed
    confirm - customer confirms or corrects the address before shipping
    nudge   - offer a small discount to switch to prepaid
    review  - hold for a human, who approves, overrides or holds with a reason

There is deliberately no BLOCK action, and none can be configured. A merchant
using ShipGate cannot refuse a customer outright through it, because the entire
argument of the project is that blunt blocking is both disproportionate and (as
Step 4 measured) economically worse than graduated handling.

WHAT A MERCHANT MAY AND MAY NOT CONFIGURE
-----------------------------------------
May:  move the score boundaries, map any tier to a gentler action, and cap the
      harshest action they are willing to take at all.

May not: reach a harsh action the evidence does not support. The policy layer
      derives a tier from the merchant's own thresholds and then hands it to
      rule_engine.apply_safeguards - the very same function assess() uses.
      Lowering a threshold lets a merchant act sooner on orders that have real
      evidence behind them; it can never manufacture evidence that is not there,
      and it cannot undo the pincode safeguard either.

That asymmetry is the whole safeguard. Configuration is for proportionality, not
for escaping the constraints - otherwise "merchant-configurable" would just mean
"the safeguards are optional", and every guarantee in the README would be void
for any merchant who edited a config file.

This module calls the engine's safeguard function rather than repeating it. The
first version did repeat it, covering only the evidence gate, and silently
escalated four clean customers in high-RTO pincodes from "ship normally" back to
"confirm" - undoing safeguard 3 for exactly the case it was written for. Two
copies of a safeguard means one of them is out of date.

Economic justification lives in evaluation.py, not here. This module decides what
a policy SAYS to do; whether that policy pays for itself is measured separately
against outcomes, and deliberately so - a policy engine that graded itself would
have the same circularity problem the synthetic data was built to avoid.
"""

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import IntEnum

from app.rule_engine import RiskAssessment, Tier, apply_safeguards

POLICY_VERSION = "1.0.0"

# A merchant override must say why. This is the minimum that counts as a reason -
# short enough not to be a nuisance, long enough that "ok" does not pass.
MIN_OVERRIDE_REASON_CHARS = 10


class Action(IntEnum):
    """Ordered by how much they disrupt the customer. Order matters: it is what
    'least-disruptive' and 'cap the harshest action' are measured against."""

    SHIP = 0
    CONFIRM = 1
    NUDGE = 2
    REVIEW = 3

    @property
    def label(self) -> str:
        return {0: "ship", 1: "confirm", 2: "nudge", 3: "review"}[self.value]


DEFAULT_TIER_ACTIONS = {
    Tier.LOW: Action.SHIP,
    Tier.MEDIUM: Action.CONFIRM,
    Tier.HIGH: Action.NUDGE,
    Tier.VERY_HIGH: Action.REVIEW,
}

# Score at which each tier begins. Matches rule_engine.tier_from_score by
# default; a merchant may move them.
DEFAULT_THRESHOLDS = (31, 61, 86)


@dataclass(frozen=True)
class MerchantPolicy:
    """One merchant's configuration. Validated on construction, not on use."""

    merchant_id: str = "default"
    thresholds: tuple = DEFAULT_THRESHOLDS       # (medium_at, high_at, very_high_at)
    tier_actions: dict = field(default_factory=lambda: dict(DEFAULT_TIER_ACTIONS))
    max_action: Action = Action.REVIEW            # harshest action allowed at all

    def __post_init__(self):
        if len(self.thresholds) != 3:
            raise ValueError("thresholds must be (medium_at, high_at, very_high_at)")
        if not all(isinstance(t, int) for t in self.thresholds):
            raise ValueError("thresholds must be integers")
        if not (0 < self.thresholds[0] < self.thresholds[1] < self.thresholds[2]):
            raise ValueError("thresholds must be strictly ascending and above zero, got %r"
                             % (self.thresholds,))
        missing = set(Tier) - set(self.tier_actions)
        if missing:
            raise ValueError("tier_actions missing %s"
                             % sorted(t.label for t in missing))
        # A higher tier must never receive a gentler action than a lower one.
        # Without this a merchant could configure "review at Medium, ship at
        # High", which is incoherent and would make the audit trail nonsense.
        ordered = [self.tier_actions[t] for t in sorted(Tier)]
        if ordered != sorted(ordered):
            raise ValueError("tier_actions must not get gentler as risk rises: %s"
                             % [a.label for a in ordered])
        if self.tier_actions[Tier.LOW] is not Action.SHIP:
            raise ValueError("the Low tier must ship - it is the no-action baseline")

    def action_for(self, tier: Tier) -> Action:
        return min(self.tier_actions[tier], self.max_action)


DEFAULT_POLICY = MerchantPolicy()

# Two illustrative alternatives. Neither can escape the evidence gate.
GENTLE_POLICY = MerchantPolicy(
    merchant_id="gentle",
    thresholds=(41, 71, 91),
    tier_actions={Tier.LOW: Action.SHIP, Tier.MEDIUM: Action.CONFIRM,
                  Tier.HIGH: Action.CONFIRM, Tier.VERY_HIGH: Action.NUDGE},
    max_action=Action.NUDGE,
)

STRICT_POLICY = MerchantPolicy(
    merchant_id="strict",
    thresholds=(21, 51, 76),
    tier_actions=dict(DEFAULT_TIER_ACTIONS),
)


@dataclass(frozen=True)
class Override:
    """A human overruling the policy. The reason is mandatory, by design."""

    action: Action
    reason: str
    actor: str
    at: datetime

    def to_dict(self) -> dict:
        return {"action": self.action.label, "reason": self.reason,
                "actor": self.actor, "at": self.at.isoformat(timespec="seconds")}


@dataclass(frozen=True)
class PolicyDecision:
    """Everything the audit trail needs about one decision.

    CLAUDE.md requires each record to carry rule_version, the fired rules, the
    score before and after caps, the recommended action, any override plus its
    reason, and eventually the outcome. Everything but the outcome is here; the
    outcome is attached later by audit_service, because it does not exist yet at
    decision time.
    """

    order_id: str
    rule_version: str
    policy_version: str
    merchant_id: str

    raw_score: int                # before per-group caps
    score: int                    # after per-group caps
    evidence_score: int

    tier_before_safeguards: Tier  # what the raw score alone would have said
    engine_tier: Tier             # after the rule engine's own safeguards
    policy_tier: Tier             # after this merchant's thresholds and gate

    recommended_action: Action
    reasons: tuple
    fired_rules: tuple
    safeguards_applied: tuple
    policy_adjustments: tuple

    override: Override = None

    @property
    def final_action(self) -> Action:
        """What actually happens - the override wins if there is one."""
        return self.override.action if self.override else self.recommended_action

    @property
    def was_overridden(self) -> bool:
        return self.override is not None

    def to_dict(self) -> dict:
        return {
            "order_id": self.order_id,
            "rule_version": self.rule_version,
            "policy_version": self.policy_version,
            "merchant_id": self.merchant_id,
            "raw_score": self.raw_score,
            "score": self.score,
            "evidence_score": self.evidence_score,
            "tier_before_safeguards": self.tier_before_safeguards.label,
            "engine_tier": self.engine_tier.label,
            "policy_tier": self.policy_tier.label,
            "recommended_action": self.recommended_action.label,
            "final_action": self.final_action.label,
            "was_overridden": self.was_overridden,
            "override": self.override.to_dict() if self.override else None,
            "reasons": list(self.reasons),
            "fired_rules": [dict(vars(r)) for r in self.fired_rules],
            "safeguards_applied": list(self.safeguards_applied),
            "policy_adjustments": list(self.policy_adjustments),
        }


# --------------------------------------------------------------------------
# Deciding
# --------------------------------------------------------------------------
def tier_for_policy(score: int, thresholds: tuple) -> Tier:
    """Same shape as rule_engine.tier_from_score, but with merchant boundaries."""
    medium_at, high_at, very_high_at = thresholds
    if score < medium_at:
        return Tier.LOW
    if score < high_at:
        return Tier.MEDIUM
    if score < very_high_at:
        return Tier.HIGH
    return Tier.VERY_HIGH


def decide(assessment: RiskAssessment, policy: MerchantPolicy = DEFAULT_POLICY,
           order_id: str = None) -> PolicyDecision:
    """Choose the least-disruptive action this policy allows for this score.

    The merchant's thresholds decide which band the score falls in; the rule
    engine's safeguards then get the final word. Calling the engine's own
    apply_safeguards - rather than reimplementing part of it here - is what
    stops configuration from becoming a way around the guarantees.
    """
    policy_tier, adjustments = apply_safeguards(
        tier_for_policy(assessment.score, policy.thresholds),
        assessment.score,
        assessment.evidence_score,
        assessment.fired_rules,
        tier_fn=lambda s: tier_for_policy(s, policy.thresholds),
    )

    mapped = policy.tier_actions[policy_tier]
    action = policy.action_for(policy_tier)
    if action is not mapped:
        adjustments.append("capped_at_%s" % policy.max_action.label)

    reasons = _explain(assessment, policy, policy_tier, action, adjustments)

    return PolicyDecision(
        order_id=order_id or assessment.order_id,
        rule_version=assessment.rule_version,
        policy_version=POLICY_VERSION,
        merchant_id=policy.merchant_id,
        raw_score=assessment.raw_score,
        score=assessment.score,
        evidence_score=assessment.evidence_score,
        tier_before_safeguards=assessment.tier_before_safeguards,
        engine_tier=assessment.tier,
        policy_tier=policy_tier,
        recommended_action=action,
        reasons=tuple(reasons),
        fired_rules=tuple(assessment.fired_rules),
        safeguards_applied=tuple(assessment.safeguards_applied),
        policy_adjustments=tuple(adjustments),
    )


def _explain(assessment, policy, policy_tier, action, adjustments) -> list:
    """Plain-language reasons, for the drawer in the dashboard and the audit log."""
    reasons = ["Score %d of a possible 145 puts this order in the %s band for "
               "merchant '%s'." % (assessment.score, policy_tier.label,
                                   policy.merchant_id)]

    if assessment.fired_rules:
        top = sorted(assessment.fired_rules, key=lambda h: -abs(h.points))[:3]
        reasons.append("Largest contributors: %s."
                       % "; ".join("%s %+d (%s)" % (h.label, h.points, h.id)
                                   for h in top))
    else:
        reasons.append("No rules fired at all - nothing about this order raised a flag.")

    if assessment.evidence_score:
        reasons.append("Evidence score %d - there is a real signal about this "
                       "customer or address, not just context."
                       % assessment.evidence_score)
    else:
        reasons.append("Evidence score 0 - only contextual signals fired, so a "
                       "restrictive action is not available for this order.")

    for name in assessment.safeguards_applied:
        reasons.append("Rule-engine safeguard applied: %s." % name)
    for name in adjustments:
        reasons.append("Policy adjustment applied: %s." % name)

    reasons.append("Action chosen: %s (the least disruptive step this policy "
                   "allows at this tier)." % action.label)
    return reasons


# --------------------------------------------------------------------------
# Overrides
# --------------------------------------------------------------------------
def apply_override(decision: PolicyDecision, action: Action, reason: str,
                   actor: str, at: datetime = None) -> PolicyDecision:
    """Record a human overruling the recommendation. A reason is mandatory.

    The original recommendation is never erased - recommended_action stays put
    and the override sits alongside it, so the audit trail always shows both
    what the system said and what the human did instead.
    """
    cleaned = (reason or "").strip()
    if len(cleaned) < MIN_OVERRIDE_REASON_CHARS:
        raise ValueError(
            "an override needs a reason of at least %d characters - got %r"
            % (MIN_OVERRIDE_REASON_CHARS, cleaned)
        )
    if not (actor or "").strip():
        raise ValueError("an override must record who made it")
    if not isinstance(action, Action):
        raise ValueError("override action must be an Action, got %r" % (action,))

    return replace(decision, override=Override(
        action=action, reason=cleaned, actor=actor.strip(),
        at=at or datetime.now(),
    ))


if __name__ == "__main__":
    from app.feature_normalizer import load_dataset
    from app.rule_engine import assess

    records = load_dataset()
    counts = {}
    for policy in (DEFAULT_POLICY, GENTLE_POLICY, STRICT_POLICY):
        tally = {}
        gated = 0
        for record in records:
            decision = decide(assess(record.features), policy, record.order_id)
            tally[decision.recommended_action.label] = (
                tally.get(decision.recommended_action.label, 0) + 1)
            if decision.policy_adjustments:
                gated += 1
        counts[policy.merchant_id] = (tally, gated)

    print("policy engine v%s - action mix over %d orders" % (POLICY_VERSION, len(records)))
    for merchant, (tally, gated) in counts.items():
        total = sum(tally.values())
        mix = "  ".join("%s %d (%.1f%%)" % (k, v, 100.0 * v / total)
                        for k, v in sorted(tally.items()))
        print("  %-8s %s" % (merchant, mix))
        print("           safeguards demoted %d orders away from a harsher action"
              % gated)
