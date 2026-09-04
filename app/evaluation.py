"""Chronological evaluation of the rule engine against independent outcomes.

Answers one question honestly: given orders scored using only what was knowable
at checkout, how well do the rules separate the ones that came back from the
ones that did not?

WHAT MAKES THIS EVALUATION WORTH ANYTHING
-----------------------------------------
Three properties, each enforced elsewhere and relied on here:

  1. The outcomes come from latent_outcome.py, which has different coefficients,
     hidden factors and its own seed, and imports neither the rule engine nor the
     generator. We are not grading our own homework.
  2. Features come from feature_normalizer.py, which attaches only history that
     had already resolved before each order's timestamp.
  3. The split is chronological. Train on the earlier 70%, report on the later
     30%. Never random - a random split lets the model see the future.

EVERY NUMBER PRODUCED HERE IS A SYNTHETIC SIMULATION RESULT. It validates policy
logic, not production accuracy.

WHY THE HEADLINE PR-AUC IS COD-ONLY
-----------------------------------
Prepaid orders fire nothing in the payment group and almost all land in Low.
Including them lets a ranking metric take credit for separating prepaid from COD,
which is not a prediction - payment method is known at checkout. The COD-only
figure is the one worth quoting; the overall figure is reported alongside it so
the gap is visible rather than hidden.

WHAT THE COST TABLE IMPLIES
---------------------------
With the brief's numbers, flagging one order is worth it only when

    p*TP_VALUE + (1-p)*FP_COST  >  p*FN_COST + (1-p)*TN_VALUE

which for +200 / -300 / -200 / +300 solves to p > 0.60. Missing a bad order
costs 400 relative to catching it; disrupting a good one costs 600 relative to
leaving it alone. That is a demanding bar, and it is a real consequence of the
stated costs rather than a modelling accident.

It is worth understanding where it comes from: the -300 false-positive cost
assumes a disrupted customer is lost entirely at full margin. That is the right
price for a hard block. It is the wrong price for a confirmation SMS, which is
what the Medium tier actually does.

So this module reports TWO cost tables. The brief's, unchanged, because it was
specified and it is the honest blunt-instrument baseline. And a graduated one
that prices each action for what it actually does - see ActionCost. The brief's
table turns out to be the limiting case of the graduated model with
prevent_rate and abandon_rate both at 1.0, which is precisely the arithmetic of
a hard block. That equivalence is the argument for graduated actions, and it is
a derivation rather than an assertion.

The graduated model's prevent/abandon rates are ASSUMPTIONS about real human
behaviour that no amount of synthetic data can supply. They are stated as such,
and the report prints the abandon rate at which each action stops paying for
itself so the fragility of the conclusion is visible rather than buried.
"""

from dataclasses import dataclass
import pathlib

from sklearn.metrics import average_precision_score

from app.feature_normalizer import load_dataset
from app.rule_engine import Tier, assess

EVALUATION_VERSION = "1.0.0"

TRAIN_FRACTION = 0.70

CAVEAT = ("Synthetic simulation result - validates policy logic, "
          "not production accuracy.")

# --- False-positive cost table (from the design brief) --------------------
TP_VALUE = 200.0    # flagged a risky order, prevented a real RTO
FP_COST = -300.0    # flagged a genuine customer, caused friction/drop-off
FN_COST = -200.0    # missed a real RTO, shipped normally, paid two-way freight
TN_VALUE = 300.0    # correctly shipped a safe order, full sale

# Probability above which flagging beats shipping, given the costs above.
BREAK_EVEN_P = (TN_VALUE - FP_COST) / ((TN_VALUE - FP_COST) + (TP_VALUE - FN_COST))

# Tier boundaries, as score thresholds. Named for the action they trigger.
TIER_THRESHOLDS = (
    ("medium+ (confirm and above)", 31),
    ("high+ (prepaid nudge and above)", 61),
    ("very_high (manual review only)", 86),
)

# --- Per-action economics -------------------------------------------------
# The table above prices every intervention identically, which is only correct
# for a hard block. ShipGate's whole argument is that a confirmation SMS is not
# a lost sale. Modelling that needs three numbers per action rather than one.
#
# Absolute accounting, per order:
#     delivered and kept   +300   (margin)
#     came back as RTO     -200   (two-way freight)
#     abandoned/cancelled     0   (no sale, but no freight either)
#     minus the action's operating cost
#
# A blunt block is the limiting case of this model: prevent_rate 1.0 (nothing
# ships, so nothing can come back) and abandon_rate 1.0 (the customer is gone).
# Substituting those reproduces the brief's +200 / -300 swings exactly, which is
# why that table is so hostile to intervening - it charges block prices for
# every action ShipGate offers.
ORDER_MARGIN = 300.0
RTO_FREIGHT_COST = -200.0


@dataclass(frozen=True)
class ActionCost:
    """What one action does to an order's economics.

    prevent_rate  - share of orders that WOULD have come back that this action
                    stops (address corrected, order cancelled, switched to
                    prepaid). The order does not ship, so no freight is burned.
    abandon_rate  - share of orders that WOULD have delivered fine where the
                    friction loses the sale.
    op_cost       - direct cost of performing the action, per order.
    """

    label: str
    prevent_rate: float
    abandon_rate: float
    op_cost: float

    def value(self, is_rto: bool) -> float:
        """Expected rupee value of this order, given what it would have done."""
        if is_rto:
            return (1.0 - self.prevent_rate) * RTO_FREIGHT_COST - self.op_cost
        return (1.0 - self.abandon_rate) * ORDER_MARGIN - self.op_cost

    @property
    def break_even_p(self) -> float:
        """RTO probability above which this action beats shipping normally.

        Acting gains prevent_rate * 200 on a bad order and loses
        abandon_rate * 300 on a good one, minus the operating cost throughout.
        """
        gain = self.prevent_rate * -RTO_FREIGHT_COST
        loss = self.abandon_rate * ORDER_MARGIN
        if gain + loss == 0:
            return float("inf")
        return (loss + self.op_cost) / (gain + loss)


# These rates are ASSUMPTIONS, not measurements. Nothing in the synthetic data
# says how many customers abandon after an OTP prompt - that is a fact about
# real human behaviour which this project has no data on. They are set to
# defensible mid-range values and the sensitivity of the conclusion to them is
# reported below, because the per-action result depends on them entirely.
ACTIONS = {
    "ship": ActionCost("ship normally", 0.00, 0.00, 0.0),
    "confirm": ActionCost("confirmation / OTP", 0.35, 0.03, 5.0),
    "nudge": ActionCost("prepaid incentive nudge", 0.55, 0.12, 15.0),
    "review": ActionCost("manual review queue", 0.75, 0.20, 40.0),
    "block": ActionCost("hard block (brief's price)", 1.00, 1.00, 0.0),
}

TIER_ACTIONS = {
    Tier.LOW: "ship",
    Tier.MEDIUM: "confirm",
    Tier.HIGH: "nudge",
    Tier.VERY_HIGH: "review",
}


@dataclass(frozen=True)
class Scored:
    """One evaluated order: what we saw, what we said, what happened."""

    order_id: str
    score: int
    tier: Tier
    evidence_score: int
    is_rto: bool
    is_cod: bool
    is_known_customer: bool
    merchant_cohort: str


@dataclass(frozen=True)
class Confusion:
    tp: int
    fp: int
    fn: int
    tn: int

    @property
    def flagged(self) -> int:
        return self.tp + self.fp

    @property
    def precision(self) -> float:
        return self.tp / self.flagged if self.flagged else 0.0

    @property
    def recall(self) -> float:
        positives = self.tp + self.fn
        return self.tp / positives if positives else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    @property
    def net_value(self) -> float:
        return (self.tp * TP_VALUE + self.fp * FP_COST
                + self.fn * FN_COST + self.tn * TN_VALUE)


# --------------------------------------------------------------------------
# Split and score
# --------------------------------------------------------------------------
def split_chronological(records: list, train_fraction: float = TRAIN_FRACTION):
    """Earlier orders train, later orders test. Never shuffle.

    Exposed because the calibration layer must use exactly this split - a model
    tuned on a different one is not comparable to these numbers.
    """
    ordered = sorted(records, key=lambda r: (r.timestamp, r.order_id))
    cut = int(len(ordered) * train_fraction)
    return ordered[:cut], ordered[cut:]


def score_records(records: list) -> list:
    out = []
    for record in records:
        assessment = assess(record.features)
        out.append(Scored(
            order_id=record.order_id,
            score=assessment.score,
            tier=assessment.tier,
            evidence_score=assessment.evidence_score,
            is_rto=record.is_rto,
            is_cod=record.payment_method == "cod",
            is_known_customer=record.is_known_customer,
            merchant_cohort=record.merchant_cohort,
        ))
    return out


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------
def confusion_at(scored: list, threshold: int) -> Confusion:
    tp = fp = fn = tn = 0
    for s in scored:
        flagged = s.score >= threshold
        if flagged and s.is_rto:
            tp += 1
        elif flagged:
            fp += 1
        elif s.is_rto:
            fn += 1
        else:
            tn += 1
    return Confusion(tp, fp, fn, tn)


def confusion_for_predicate(scored: list, predicate) -> Confusion:
    """Confusion matrix for any flagging rule, used for the baseline rows."""
    tp = fp = fn = tn = 0
    for s in scored:
        flagged = predicate(s)
        if flagged and s.is_rto:
            tp += 1
        elif flagged:
            fp += 1
        elif s.is_rto:
            fn += 1
        else:
            tn += 1
    return Confusion(tp, fp, fn, tn)


def pr_auc(scored: list) -> float:
    """Average precision over the raw score. Undefined without both classes."""
    labels = [s.is_rto for s in scored]
    if not scored or len(set(labels)) < 2:
        return float("nan")
    return float(average_precision_score(labels, [s.score for s in scored]))


def prevalence(scored: list) -> float:
    return sum(s.is_rto for s in scored) / len(scored) if scored else float("nan")


def policy_value(scored: list, action_for) -> tuple:
    """Total rupee value of applying `action_for` to every order.

    Returns (total, counts) where counts is how many orders got each action, so
    the operational load of a policy is visible next to its value.
    """
    total = 0.0
    counts = {}
    for s in scored:
        name = action_for(s)
        total += ACTIONS[name].value(s.is_rto)
        counts[name] = counts.get(name, 0) + 1
    return total, counts


def shipgate_action_for(s: Scored) -> str:
    """The actual product: tier decides the action, after safeguards."""
    return TIER_ACTIONS[s.tier]


def best_threshold_by_value(scored: list) -> tuple:
    """Sweep every threshold and return the one maximising net value.

    Only ever called on the TRAIN slice. Applying it to test is what makes this
    a genuine fit-then-evaluate rather than a threshold chosen with hindsight.
    """
    candidates = sorted({s.score for s in scored} | {0, 200})
    best = max(candidates, key=lambda t: confusion_at(scored, t).net_value)
    return best, confusion_at(scored, best).net_value


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------
def _rule(width: int = 78) -> str:
    return "-" * width


def _money(value: float) -> str:
    """Signed rupee amount with thousands separators."""
    return "INR {:+,.0f}".format(value)


def _print_confusion_table(title: str, rows: list) -> None:
    print(title)
    print("  %-34s %6s %6s %6s %6s %7s %7s %7s"
          % ("policy", "TP", "FP", "FN", "TN", "prec", "recall", "F1"))
    for label, c in rows:
        print("  %-34s %6d %6d %6d %6d %6.3f %6.3f %6.3f"
              % (label, c.tp, c.fp, c.fn, c.tn, c.precision, c.recall, c.f1))


def _print_breakdown(title: str, groups: list) -> None:
    print(title)
    print("  %-24s %6s %8s %9s %9s" % ("segment", "n", "RTO", "PR-AUC", "vs base"))
    for label, subset in groups:
        if not subset:
            continue
        base = prevalence(subset)
        auc = pr_auc(subset)
        lift = auc / base if base and base == base else float("nan")
        print("  %-24s %6d %7.1f%% %9.3f %8.2fx"
              % (label, len(subset), 100 * base, auc, lift))


def report(orders_path: str = "data/orders.csv",
           outcomes_path: str = "data/outcomes.csv") -> None:
    records = load_dataset(orders_path, outcomes_path)
    train_records, test_records = split_chronological(records)
    train = score_records(train_records)
    test = score_records(test_records)

    print("=" * 78)
    print("ShipGate evaluation v%s" % EVALUATION_VERSION)
    print(CAVEAT.upper())
    print("=" * 78)
    print("Chronological split, never random.")
    print("  train %d orders  %s -> %s"
          % (len(train), train_records[0].timestamp.date(),
             train_records[-1].timestamp.date()))
    print("  test  %d orders  %s -> %s"
          % (len(test), test_records[0].timestamp.date(),
             test_records[-1].timestamp.date()))
    print("  All metrics below are computed on the TEST slice only.")
    print()

    # --- Ranking quality --------------------------------------------------
    cod_test = [s for s in test if s.is_cod]
    print(_rule())
    print("RANKING QUALITY (PR-AUC on the raw score)")
    print(_rule())
    print("  COD only (the number worth quoting)   %.3f   baseline %.3f  (%.2fx)"
          % (pr_auc(cod_test), prevalence(cod_test),
             pr_auc(cod_test) / prevalence(cod_test)))
    print("  All orders                            %.3f   baseline %.3f  (%.2fx)"
          % (pr_auc(test), prevalence(test), pr_auc(test) / prevalence(test)))
    print()
    print("  Baseline is the positive rate - what random ranking scores.")
    print("  The all-orders figure is inflated: separating prepaid from COD is")
    print("  not a prediction, payment method is known at checkout.")
    print()

    # --- Operating points -------------------------------------------------
    print(_rule())
    print("OPERATING POINTS AT THE TIER BOUNDARIES (all test orders)")
    print(_rule())
    _print_confusion_table("", [(label, confusion_at(test, t))
                                for label, t in TIER_THRESHOLDS])
    print()
    _print_confusion_table("Same, COD orders only:",
                           [(label, confusion_at(cod_test, t))
                            for label, t in TIER_THRESHOLDS])
    print()

    # --- Required breakdowns ---------------------------------------------
    print(_rule())
    print("BREAKDOWNS (COD test orders)")
    print(_rule())
    _print_breakdown("By customer state:", [
        ("new (no resolved history)", [s for s in cod_test if not s.is_known_customer]),
        ("known", [s for s in cod_test if s.is_known_customer]),
    ])
    print()
    _print_breakdown("By merchant cohort:", [
        (cohort, [s for s in cod_test if s.merchant_cohort == cohort])
        for cohort in ("gemstone", "fast_fashion")
    ])
    print()

    # --- Cost table -------------------------------------------------------
    chosen, train_value = best_threshold_by_value(train)
    print(_rule())
    print("COST TABLE  (TP +%.0f / FP %.0f / FN %.0f / TN +%.0f per order)"
          % (TP_VALUE, FP_COST, FN_COST, TN_VALUE))
    print(_rule())
    print("  Flagging beats shipping only above p(RTO) = %.2f under these costs." % BREAK_EVEN_P)
    print("  Missing a bad order costs %.0f relative to catching it;" % (TP_VALUE - FN_COST))
    print("  disrupting a good one costs %.0f relative to leaving it alone."
          % (TN_VALUE - FP_COST))
    print()

    policies = [
        ("conservative: score >= 61", confusion_at(test, 61)),
        ("balanced: score >= 31", confusion_at(test, 31)),
        ("aggressive: score >= 21", confusion_at(test, 21)),
        ("value-optimal on train: >= %-3d" % chosen, confusion_at(test, chosen)),
        ("reference: flag nothing", confusion_at(test, 10_000)),
        ("reference: flag everything", confusion_at(test, 0)),
        ("reference: flag every COD order",
         confusion_for_predicate(test, lambda s: s.is_cod)),
    ]

    print("  %-34s %7s %7s %8s %12s" % ("policy", "flagged", "prec", "recall", "net value"))
    for label, c in policies:
        print("  %-34s %7d %7.3f %8.3f %12s"
              % (label, c.flagged, c.precision, c.recall, _money(c.net_value)))
    print()
    print("  Threshold %d was selected on the train slice (net value %s there)"
          % (chosen, _money(train_value)))
    print("  and applied unchanged to test. No threshold was chosen with hindsight.")
    print()
    print("  NOTE: this table prices every intervention as a lost customer at full")
    print("  margin, which is the cost of a hard BLOCK. ShipGate does not block.")
    print("  The graduated table below prices each action for what it actually is.")
    print()

    # --- Per-action economics --------------------------------------------
    print(_rule())
    print("GRADUATED ACTION ECONOMICS (absolute rupees, test slice)")
    print(_rule())
    print("  Each action has its own break-even, because each does a different")
    print("  amount of good and a different amount of harm.")
    print()
    print("  %-26s %8s %8s %7s %11s %12s"
          % ("action", "prevents", "abandons", "cost", "break-even", "tier RTO"))
    tier_rate = {}
    for tier, name in TIER_ACTIONS.items():
        subset = [s for s in test if s.tier is tier]
        tier_rate[name] = prevalence(subset) if subset else float("nan")
    for name in ("ship", "confirm", "nudge", "review", "block"):
        a = ACTIONS[name]
        rate = tier_rate.get(name)
        observed = "%11.1f%%" % (100 * rate) if rate is not None and rate == rate else " " * 12
        breakeven = ("%10s " % "-" if a.break_even_p == float("inf")
                     else "%10.1f%%" % (100 * a.break_even_p))
        print("  %-26s %7.0f%% %8.0f%% %7.0f %s %s"
              % (a.label, 100 * a.prevent_rate, 100 * a.abandon_rate, a.op_cost,
                 breakeven, observed))
    print()
    print("  'tier RTO' is the measured failure rate of the tier that triggers")
    print("  that action. An action is justified where its tier's rate exceeds")
    print("  its own break-even.")
    print()

    ship_all, _ = policy_value(test, lambda s: "ship")
    shipgate, load = policy_value(test, shipgate_action_for)
    confirm_all, _ = policy_value(test, lambda s: "confirm")
    block_tiers, _ = policy_value(
        test, lambda s: "block" if s.tier is not Tier.LOW else "ship")
    block_vh, _ = policy_value(
        test, lambda s: "block" if s.tier is Tier.VERY_HIGH else "ship")

    print("  %-44s %13s %12s" % ("policy", "net value", "vs ship-all"))
    for label, value in (
        ("ship everything (do nothing)", ship_all),
        ("ShipGate graduated tiers", shipgate),
        ("confirm every order", confirm_all),
        ("hard block everything above Low", block_tiers),
        ("hard block Very High only", block_vh),
    ):
        print("  %-44s %13s %12s"
              % (label, _money(value), _money(value - ship_all)))
    print()
    print("  ShipGate's operational load: %s"
          % ", ".join("%d %s" % (n, k) for k, n in sorted(load.items())))
    print()
    print("  THE RATES ABOVE ARE ASSUMPTIONS, NOT MEASUREMENTS. Nothing in")
    print("  synthetic data can tell us how many real customers abandon after an")
    print("  OTP prompt. Sensitivity - the abandon rate at which each action stops")
    print("  paying for itself, holding its tier's measured RTO rate fixed:")
    for name in ("confirm", "nudge", "review"):
        a = ACTIONS[name]
        p = tier_rate[name]
        # Solve p*prevent*200 = abandon*300*(1-p)... + op_cost for abandon.
        breakeven_abandon = ((p * a.prevent_rate * -RTO_FREIGHT_COST - a.op_cost)
                             / (ORDER_MARGIN * (1 - p))) if p == p and p < 1 else float("nan")
        print("    %-26s assumed %.0f%%, stops paying above %.0f%%"
              % (a.label, 100 * a.abandon_rate, 100 * breakeven_abandon))
    print()
    print(_rule())
    print(CAVEAT)
    print(_rule())


def report_data(orders_path: str = "data/orders.csv",
                outcomes_path: str = "data/outcomes.csv") -> dict:
    """The same numbers report() prints, as JSON for the dashboard.

    Emitted as a static file rather than an endpoint on purpose. These come from
    a batch evaluation over a held-out time slice; recomputing them per request
    would be both slow and wrong, because the split is a property of the whole
    dataset rather than of any one order.
    """
    records = load_dataset(orders_path, outcomes_path)
    train_records, test_records = split_chronological(records)
    train, test = score_records(train_records), score_records(test_records)
    cod_test = [s for s in test if s.is_cod]
    chosen, _ = best_threshold_by_value(train)

    def confusion_dict(label, c):
        return {"policy": label, "tp": c.tp, "fp": c.fp, "fn": c.fn, "tn": c.tn,
                "flagged": c.flagged, "precision": round(c.precision, 4),
                "recall": round(c.recall, 4), "f1": round(c.f1, 4),
                "net_value": round(c.net_value, 2)}

    ship_all, _ = policy_value(test, lambda s: "ship")
    shipgate, load = policy_value(test, shipgate_action_for)
    confirm_all, _ = policy_value(test, lambda s: "confirm")
    block_tiers, _ = policy_value(
        test, lambda s: "block" if s.tier is not Tier.LOW else "ship")
    block_vh, _ = policy_value(
        test, lambda s: "block" if s.tier is Tier.VERY_HIGH else "ship")

    tier_rate = {}
    for tier, name in TIER_ACTIONS.items():
        subset = [s for s in test if s.tier is tier]
        tier_rate[name] = round(prevalence(subset), 4) if subset else None

    return {
        "evaluation_version": EVALUATION_VERSION,
        "disclaimer": CAVEAT,
        "split": {
            "train_orders": len(train),
            "test_orders": len(test),
            "train_from": str(train_records[0].timestamp.date()),
            "train_to": str(train_records[-1].timestamp.date()),
            "test_from": str(test_records[0].timestamp.date()),
            "test_to": str(test_records[-1].timestamp.date()),
        },
        "ranking": {
            "cod_only": {"pr_auc": round(pr_auc(cod_test), 4),
                         "baseline": round(prevalence(cod_test), 4),
                         "n": len(cod_test)},
            "all_orders": {"pr_auc": round(pr_auc(test), 4),
                           "baseline": round(prevalence(test), 4),
                           "n": len(test)},
        },
        "operating_points": [confusion_dict(label, confusion_at(cod_test, t))
                             for label, t in TIER_THRESHOLDS],
        "segments": [
            {"segment": label, "n": len(subset),
             "rto_rate": round(prevalence(subset), 4),
             "pr_auc": round(pr_auc(subset), 4)}
            for label, subset in (
                ("new customer", [s for s in cod_test if not s.is_known_customer]),
                ("known customer", [s for s in cod_test if s.is_known_customer]),
                ("gemstone", [s for s in cod_test if s.merchant_cohort == "gemstone"]),
                ("fast_fashion", [s for s in cod_test
                                  if s.merchant_cohort == "fast_fashion"]),
            ) if subset
        ],
        "blunt_cost_table": {
            "break_even_p": round(BREAK_EVEN_P, 4),
            "note": ("Prices every intervention as a customer lost at full "
                     "margin, which is the cost of a hard block."),
            "policies": [
                confusion_dict("conservative: score >= 61", confusion_at(test, 61)),
                confusion_dict("balanced: score >= 31", confusion_at(test, 31)),
                confusion_dict("aggressive: score >= 21", confusion_at(test, 21)),
                confusion_dict("value-optimal on train: >= %d" % chosen,
                               confusion_at(test, chosen)),
                confusion_dict("reference: flag nothing", confusion_at(test, 10_000)),
                confusion_dict("reference: flag everything", confusion_at(test, 0)),
                confusion_dict("reference: flag every COD order",
                               confusion_for_predicate(test, lambda s: s.is_cod)),
            ],
        },
        "graduated_actions": [
            {"action": name, "label": ACTIONS[name].label,
             "prevent_rate": ACTIONS[name].prevent_rate,
             "abandon_rate": ACTIONS[name].abandon_rate,
             "op_cost": ACTIONS[name].op_cost,
             "break_even_p": (None if ACTIONS[name].break_even_p == float("inf")
                              else round(ACTIONS[name].break_even_p, 4)),
             "tier_rto_rate": tier_rate.get(name)}
            for name in ("ship", "confirm", "nudge", "review", "block")
        ],
        "graduated_policies": [
            {"policy": "ship everything (do nothing)", "net_value": round(ship_all, 2),
             "vs_ship_all": 0.0},
            {"policy": "ShipGate graduated tiers", "net_value": round(shipgate, 2),
             "vs_ship_all": round(shipgate - ship_all, 2)},
            {"policy": "confirm every order", "net_value": round(confirm_all, 2),
             "vs_ship_all": round(confirm_all - ship_all, 2)},
            {"policy": "hard block everything above Low",
             "net_value": round(block_tiers, 2),
             "vs_ship_all": round(block_tiers - ship_all, 2)},
            {"policy": "hard block Very High only", "net_value": round(block_vh, 2),
             "vs_ship_all": round(block_vh - ship_all, 2)},
        ],
        "operational_load": load,
        "assumption_sensitivity": [
            {"action": ACTIONS[name].label,
             "assumed_abandon_rate": ACTIONS[name].abandon_rate,
             "stops_paying_above": round(
                 (tier_rate[name] * ACTIONS[name].prevent_rate * -RTO_FREIGHT_COST
                  - ACTIONS[name].op_cost) / (ORDER_MARGIN * (1 - tier_rate[name])), 4)}
            for name in ("confirm", "nudge", "review") if tier_rate.get(name)
        ],
    }


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="ShipGate evaluation")
    parser.add_argument("--json", metavar="PATH", nargs="?",
                        const="frontend/public/evaluation.json",
                        help="also write the numbers as JSON for the dashboard")
    args = parser.parse_args()

    report()
    if args.json:
        path = pathlib.Path(args.json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report_data(), indent=2), encoding="utf-8")
        print()
        print("wrote %s for the dashboard cost view" % path)
