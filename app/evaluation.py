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
price for a hard block. It is almost certainly too harsh for a confirmation
step, which is what the Medium tier actually does. A per-action cost model would
be more faithful to the product, and is noted in the README as future work
rather than quietly assumed here.
"""

from dataclasses import dataclass

from sklearn.metrics import average_precision_score

from app.feature_normalizer import load_dataset
from app.rule_engine import Tier, assess

EVALUATION_VERSION = "1.0.0"

TRAIN_FRACTION = 0.70

CAVEAT = ("Synthetic simulation result - validates policy logic, "
          "not production accuracy.")

# --- False-positive cost table (from CLAUDE.md) ---------------------------
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
    print(_rule())
    print(CAVEAT)
    print(_rule())


if __name__ == "__main__":
    report()
