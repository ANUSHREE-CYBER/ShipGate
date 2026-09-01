"""Tests for the chronological feature replay.

These are the leakage tests. Everything downstream - precision, recall, PR-AUC,
the cost table, the whole claim that our numbers mean anything - rests on the
guarantee that no order was scored using information from its own future. A bug
here does not crash anything; it just quietly makes the results better than they
should be, which is far worse.

The first version of build_features had exactly that bug: it used "<=" when
folding in resolved orders, so at a lag of 0 every order resolved at its own
timestamp and landed in its own feature row. The tell was 100% of orders having
resolved history, which is impossible. test_no_order_sees_its_own_outcome pins
it permanently.
"""

import collections
from datetime import timedelta

import pytest

from app import feature_normalizer as fn
from app import latent_outcome as lat
from app import synthetic_generator as gen


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """Run the real pipeline end to end: orders -> outcomes -> features."""
    tmp = tmp_path_factory.mktemp("data")
    orders_path = tmp / "orders.csv"
    outcomes_path = tmp / "outcomes.csv"

    gen.write_orders_csv(gen.generate(n_orders=3000), str(orders_path))
    orders = lat.load_orders_csv(str(orders_path))
    lat.write_outcomes_csv(lat.simulate_outcomes(orders), str(outcomes_path))

    customer_of = {o["order_id"]: o["customer_id"] for o in orders}

    def build(lag=fn.DEFAULT_RESOLUTION_LAG_DAYS):
        return fn.load_dataset(str(orders_path), str(outcomes_path), lag)

    return build, customer_of


# --------------------------------------------------------------------------
# Leakage - the tests that matter
# --------------------------------------------------------------------------
@pytest.mark.parametrize("lag", [0, 1, 5, 30])
def test_no_order_sees_its_own_outcome(built, lag):
    """A customer's first-ever order must show no resolved history at all.

    If an order can fold its own outcome into its own features, this is where it
    surfaces - and it must hold at every lag, including 0.
    """
    build, customer_of = built
    seen = set()
    for record in build(lag):
        customer = customer_of[record.order_id]
        if customer in seen:
            continue
        seen.add(customer)
        assert record.features.completed_orders == 0, (
            "first-ever order %s already had %d completed orders - it is seeing "
            "its own outcome or the future"
            % (record.order_id, record.features.completed_orders)
        )
        assert record.features.returned_orders == 0
        assert record.features.refusals_in_last_3 == 0


def test_history_matches_an_independent_replay(built):
    """Recompute history the slow, obvious way and demand the same answer."""
    build, customer_of = built
    lag = fn.DEFAULT_RESOLUTION_LAG_DAYS
    records = build(lag)
    by_id = {r.order_id: r for r in records}

    resolved_at = [(r.timestamp + timedelta(days=lag), r.order_id)
                   for r in records]

    # Spot-check the busiest customers, where an off-by-one would show up.
    counts = collections.Counter(customer_of[r.order_id] for r in records)
    for customer, _ in counts.most_common(5):
        theirs = sorted((r for r in records if customer_of[r.order_id] == customer),
                        key=lambda r: (r.timestamp, r.order_id))
        for record in theirs:
            known = [oid for when, oid in resolved_at
                     if when < record.timestamp and customer_of[oid] == customer]
            expected_completed = len(known)
            expected_returned = sum(by_id[oid].is_rto for oid in known)
            assert record.features.completed_orders == expected_completed
            assert record.features.returned_orders == expected_returned


def test_a_long_lag_withholds_all_history(built):
    """Nothing resolves inside the window, so everyone stays a new customer."""
    build, _ = built
    records = build(3650)
    assert all(r.features.completed_orders == 0 for r in records)
    assert all(r.features.pincode_total_orders == 0 for r in records)


def test_shorter_lag_reveals_more_history(built):
    """Sanity: the lag is actually doing something."""
    build, _ = built
    eager = sum(r.features.completed_orders for r in build(0))
    patient = sum(r.features.completed_orders for r in build(5))
    assert eager > patient


def test_failure_mode_never_escapes_the_loader(built, tmp_path_factory):
    """load_outcomes_csv must hand back labels only, never the failure reason."""
    tmp = tmp_path_factory.mktemp("modes")
    orders_path = tmp / "orders.csv"
    outcomes_path = tmp / "outcomes.csv"
    gen.write_orders_csv(gen.generate(n_orders=400), str(orders_path))
    orders = lat.load_orders_csv(str(orders_path))
    outcomes = lat.simulate_outcomes(orders)
    lat.write_outcomes_csv(outcomes, str(outcomes_path))

    assert any(o.failure_mode == "refused" for o in outcomes), "weak fixture"
    loaded = fn.load_outcomes_csv(str(outcomes_path))
    assert all(isinstance(v, bool) for v in loaded.values())


# --------------------------------------------------------------------------
# Internal consistency
# --------------------------------------------------------------------------
def test_counts_are_self_consistent(built):
    build, _ = built
    for record in build():
        f = record.features
        assert 0 <= f.returned_orders <= f.completed_orders
        assert 0 <= f.refusals_in_last_3 <= 3
        assert f.refusals_in_last_3 <= f.returned_orders
        assert 0 <= f.pincode_rto_count <= f.pincode_total_orders
        assert 0.0 < f.baseline_rto_rate < 1.0


def test_history_never_goes_backwards(built):
    build, customer_of = built
    latest = {}
    for record in sorted(build(), key=lambda r: (r.timestamp, r.order_id)):
        customer = customer_of[record.order_id]
        previous = latest.get(customer)
        if previous is not None:
            assert record.features.completed_orders >= previous
        latest[customer] = record.features.completed_orders


def test_records_come_back_in_time_order(built):
    build, _ = built
    records = build()
    assert all(records[i].timestamp <= records[i + 1].timestamp
               for i in range(len(records) - 1))


def test_known_customer_flag_follows_resolved_history(built):
    build, _ = built
    for record in build():
        assert record.is_known_customer == (record.features.completed_orders > 0)


def test_missing_outcome_is_refused_not_guessed(built, tmp_path_factory):
    """A silently dropped order would quietly shrink the evaluation set."""
    tmp = tmp_path_factory.mktemp("partial")
    orders_path = tmp / "orders.csv"
    outcomes_path = tmp / "outcomes.csv"
    gen.write_orders_csv(gen.generate(n_orders=300), str(orders_path))
    orders = lat.load_orders_csv(str(orders_path))
    outcomes = lat.simulate_outcomes(orders)
    lat.write_outcomes_csv(outcomes[:-5], str(outcomes_path))

    with pytest.raises(ValueError, match="no recorded outcome"):
        fn.load_dataset(str(orders_path), str(outcomes_path))


def test_features_carry_no_label(built):
    """The scorer must not be able to reach the answer through its input."""
    build, _ = built
    fields = set(vars(build()[0].features))
    assert not (fields & {"is_rto", "failure_mode", "outcome", "label"})
