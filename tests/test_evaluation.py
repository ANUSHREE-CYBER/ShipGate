"""Tests for the evaluation.

Two jobs. First, the split must never let the future into training - a
chronological split that quietly overlaps is the same class of bug as the
feature-replay leak, and just as invisible. Second, the cost arithmetic must be
right, because the cost table is a hero artifact of the submission and a sign
error in it would be quoted in the README as fact.
"""

from datetime import datetime, timedelta

import pytest

from app import evaluation as ev
from app.rule_engine import Tier


def make(score, is_rto, is_cod=True, known=True, cohort="fast_fashion", oid="X"):
    return ev.Scored(order_id=oid, score=score, tier=Tier.LOW, evidence_score=0,
                     is_rto=is_rto, is_cod=is_cod, is_known_customer=known,
                     merchant_cohort=cohort)


class FakeRecord:
    """Minimal stand-in for FeatureRecord - only the split touches these."""

    def __init__(self, order_id, timestamp):
        self.order_id = order_id
        self.timestamp = timestamp


# --------------------------------------------------------------------------
# The split
# --------------------------------------------------------------------------
def test_split_is_chronological_and_does_not_overlap():
    start = datetime(2026, 6, 1)
    records = [FakeRecord("ORD-%03d" % i, start + timedelta(hours=i))
               for i in range(100)]
    train, test = ev.split_chronological(records, 0.7)

    assert len(train) == 70 and len(test) == 30
    assert max(r.timestamp for r in train) <= min(r.timestamp for r in test)
    assert not ({r.order_id for r in train} & {r.order_id for r in test})
    assert len(train) + len(test) == len(records)


def test_split_sorts_unordered_input():
    """Input order must not matter - the split sorts before cutting."""
    start = datetime(2026, 6, 1)
    records = [FakeRecord("ORD-%03d" % i, start + timedelta(hours=i))
               for i in range(50)]
    shuffled = records[::-1]
    train_a, test_a = ev.split_chronological(records, 0.7)
    train_b, test_b = ev.split_chronological(shuffled, 0.7)
    assert [r.order_id for r in train_a] == [r.order_id for r in train_b]
    assert [r.order_id for r in test_a] == [r.order_id for r in test_b]


# --------------------------------------------------------------------------
# Confusion arithmetic
# --------------------------------------------------------------------------
def test_confusion_counts_are_right():
    scored = [make(90, True), make(90, False), make(10, True), make(10, False)]
    c = ev.confusion_at(scored, 50)
    assert (c.tp, c.fp, c.fn, c.tn) == (1, 1, 1, 1)
    assert c.flagged == 2


def test_precision_recall_f1():
    c = ev.Confusion(tp=30, fp=10, fn=20, tn=40)
    assert c.precision == pytest.approx(0.75)
    assert c.recall == pytest.approx(0.60)
    assert c.f1 == pytest.approx(2 * 0.75 * 0.6 / 1.35)


def test_empty_flag_set_does_not_divide_by_zero():
    c = ev.Confusion(tp=0, fp=0, fn=5, tn=5)
    assert c.precision == 0.0
    assert c.f1 == 0.0


def test_net_value_matches_the_cost_table():
    c = ev.Confusion(tp=10, fp=20, fn=30, tn=40)
    expected = 10 * 200 + 20 * -300 + 30 * -200 + 40 * 300
    assert c.net_value == pytest.approx(expected)
    assert c.net_value == pytest.approx(2000 - 6000 - 6000 + 12000)


def test_break_even_probability_is_sixty_percent():
    """The headline consequence of the brief's costs. Worth pinning.

    Disrupting a good order costs 600 relative to leaving it; missing a bad one
    costs 400 relative to catching it. So flagging only pays above p = 0.6.
    """
    assert ev.BREAK_EVEN_P == pytest.approx(0.60)


def test_flagging_below_break_even_destroys_value():
    """A population failing at 50% is not worth flagging under these costs."""
    scored = [make(90, i < 50) for i in range(100)]
    flagged = ev.confusion_at(scored, 50)
    untouched = ev.confusion_at(scored, 500)
    assert flagged.net_value < untouched.net_value


def test_flagging_above_break_even_creates_value():
    scored = [make(90, i < 70) for i in range(100)]
    flagged = ev.confusion_at(scored, 50)
    untouched = ev.confusion_at(scored, 500)
    assert flagged.net_value > untouched.net_value


# --------------------------------------------------------------------------
# Threshold selection
# --------------------------------------------------------------------------
def test_best_threshold_beats_every_other_threshold():
    scored = ([make(90, True) for _ in range(80)]
              + [make(90, False) for _ in range(20)]
              + [make(10, False) for _ in range(100)])
    chosen, value = ev.best_threshold_by_value(scored)
    for candidate in {s.score for s in scored} | {0, 200}:
        assert ev.confusion_at(scored, candidate).net_value <= value
    assert chosen == 90


def test_best_threshold_declines_to_flag_when_nothing_is_worth_it():
    """All groups below break-even - the optimum is to flag nothing."""
    scored = [make(s, i % 4 == 0) for i, s in enumerate([10, 40, 70, 100] * 25)]
    chosen, _ = ev.best_threshold_by_value(scored)
    assert ev.confusion_at(scored, chosen).flagged == 0


# --------------------------------------------------------------------------
# Ranking metrics
# --------------------------------------------------------------------------
def test_pr_auc_is_undefined_with_a_single_class():
    assert ev.pr_auc([make(50, True), make(20, True)]) != ev.pr_auc(
        [make(50, True), make(20, True)])  # nan != nan


def test_pr_auc_rewards_correct_ranking():
    good = [make(90, True) for _ in range(20)] + [make(10, False) for _ in range(80)]
    bad = [make(10, True) for _ in range(20)] + [make(90, False) for _ in range(80)]
    assert ev.pr_auc(good) > ev.pr_auc(bad)


def test_prevalence_is_the_positive_rate():
    assert ev.prevalence([make(0, True), make(0, False)]) == pytest.approx(0.5)


# --------------------------------------------------------------------------
# End to end
# --------------------------------------------------------------------------
def test_report_runs_on_the_real_dataset(capsys):
    """Smoke test: the whole pipeline produces a report with the caveat on it."""
    ev.report()
    out = capsys.readouterr().out
    assert "TEST slice only" in out
    assert ev.CAVEAT in out
    assert "COD only" in out
