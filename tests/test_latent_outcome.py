"""Tests for the latent outcome simulator.

The independence test is the important one here. Everything else checks that the
simulation behaves plausibly; that one checks the simulation is still entitled
to be used as ground truth at all. If latent_outcome ever imports the rule
engine, every metric computed downstream becomes circular and worthless, and no
amount of plausible-looking rates would reveal it.
"""

import ast
import collections
import pathlib

import pytest

from app import synthetic_generator as gen
from app import latent_outcome as lat


LATENT_SOURCE = pathlib.Path(lat.__file__).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def dataset(tmp_path_factory):
    """A smaller run of the real pipeline: visible orders, then outcomes.

    Goes through the CSV deliberately, because the flat file IS the contract
    between the two modules - they share no Python interface at all. Round
    tripping it here means load_orders_csv is covered by every test below.

    The test is allowed to touch both modules; it is the integration point. The
    modules themselves are forbidden from touching each other, which is what
    test_does_not_import_sibling_modules actually enforces.
    """
    path = tmp_path_factory.mktemp("data") / "orders.csv"
    gen.write_orders_csv(gen.generate(n_orders=3000), str(path))
    orders = lat.load_orders_csv(str(path))
    return orders, {o.order_id: o for o in lat.simulate_outcomes(orders)}


def rate(orders, res, predicate) -> float:
    subset = [res[o["order_id"]] for o in orders if predicate(o)]
    assert subset, "predicate matched no orders - the test itself is broken"
    return sum(o.is_rto for o in subset) / len(subset)


# --------------------------------------------------------------------------
# Independence - the anti-circularity guarantee
# --------------------------------------------------------------------------
def test_does_not_import_sibling_modules():
    """Ground truth must not be derived from the thing it is used to grade."""
    tree = ast.parse(LATENT_SOURCE)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    forbidden = {name for name in imported
                 if "rule_engine" in name or "synthetic_generator" in name}
    assert not forbidden, (
        "latent_outcome imported %s - the simulation must not depend on the "
        "logic it provides ground truth for" % sorted(forbidden)
    )


def test_carries_no_rule_engine_vocabulary_in_code():
    """Comments may discuss the rule engine; executing code may not name it."""
    tree = ast.parse(LATENT_SOURCE)
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.name for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    banned = {"assess", "OrderFeatures", "RiskAssessment", "tier_from_score",
              "RULE_VERSION", "evidence_score"}
    assert not (names & banned)


# --------------------------------------------------------------------------
# Determinism
# --------------------------------------------------------------------------
def test_same_seed_reproduces_outcomes(dataset):
    orders, _ = dataset
    first = lat.simulate_outcomes(orders, seed=4242)
    second = lat.simulate_outcomes(orders, seed=4242)
    assert first == second


def test_different_seed_changes_outcomes(dataset):
    orders, _ = dataset
    a = lat.simulate_outcomes(orders, seed=1)
    b = lat.simulate_outcomes(orders, seed=2)
    assert a != b


def test_hidden_traits_are_stable_and_salt_specific():
    a = lat._hash_normal("CUST-00042", lat.CUSTOMER_SALT)
    assert a == lat._hash_normal("CUST-00042", lat.CUSTOMER_SALT)
    assert a != lat._hash_normal("CUST-00042", lat.PINCODE_SALT)
    assert a != lat._hash_normal("CUST-00043", lat.CUSTOMER_SALT)


# --------------------------------------------------------------------------
# Structural correctness
# --------------------------------------------------------------------------
def test_one_outcome_per_order_and_ids_match(dataset):
    orders, res = dataset
    assert len(res) == len(orders)
    assert set(res) == {o["order_id"] for o in orders}


def test_failure_mode_agrees_with_is_rto(dataset):
    _, res = dataset
    for outcome in res.values():
        assert outcome.is_rto == (outcome.failure_mode != "delivered")
        assert outcome.failure_mode in ("delivered", "refused", "undeliverable")


def test_empty_input_gives_empty_output():
    assert lat.simulate_outcomes([]) == []


# --------------------------------------------------------------------------
# Calibration - marginal rates against published COD figures
# --------------------------------------------------------------------------
def test_cod_rto_rate_sits_in_the_published_band(dataset):
    orders, res = dataset
    cod = rate(orders, res, lambda o: o["payment_method"] == "cod")
    assert 0.20 <= cod <= 0.40, "COD RTO %.3f outside the 20-40%% band" % cod


def test_prepaid_fails_far_less_than_cod(dataset):
    orders, res = dataset
    cod = rate(orders, res, lambda o: o["payment_method"] == "cod")
    prepaid = rate(orders, res, lambda o: o["payment_method"] != "cod")
    assert prepaid < cod / 2.0
    assert 0.0 < prepaid < 0.10, "prepaid RTO %.3f is not credible" % prepaid


def test_prepaid_failures_are_mostly_logistics_not_refusal(dataset):
    """Nobody refuses a parcel they have already paid for - almost nobody."""
    orders, res = dataset
    prepaid = [res[o["order_id"]] for o in orders if o["payment_method"] != "cod"]
    modes = collections.Counter(o.failure_mode for o in prepaid)
    assert modes["undeliverable"] > modes["refused"]


def test_severe_address_fails_materially_more(dataset):
    orders, res = dataset
    severe = rate(orders, res, lambda o: o["address_quality"] == "severe")
    complete = rate(orders, res, lambda o: o["address_quality"] == "complete")
    assert severe > complete * 1.5


def test_harder_pincode_tiers_fail_more(dataset):
    orders, res = dataset
    metro = rate(orders, res, lambda o: o["pincode_tier"] == "metro")
    rural = rate(orders, res, lambda o: o["pincode_tier"] == "rural")
    assert rural > metro


# --------------------------------------------------------------------------
# The signals the rule engine is meant to detect must actually exist
# --------------------------------------------------------------------------
def test_momentum_produces_repeat_refusers(dataset):
    """Without a repeat-refusal pattern in the data, H1 has nothing to find."""
    orders, res = dataset
    history = collections.defaultdict(list)
    repeat_refusers = set()
    for order in sorted(orders, key=lambda o: (o["timestamp"], o["order_id"])):
        cid = order["customer_id"]
        if sum(history[cid][-3:]) >= 2:
            repeat_refusers.add(cid)
        history[cid].append(1 if res[order["order_id"]].is_rto else 0)
    assert len(repeat_refusers) >= 10


def test_clean_repeat_customers_exist(dataset):
    """Without them, the trusted-customer discount has nothing to reward."""
    orders, res = dataset
    total = collections.Counter()
    failed = collections.Counter()
    for order in orders:
        total[order["customer_id"]] += 1
        failed[order["customer_id"]] += res[order["order_id"]].is_rto
    spotless = [c for c in total if total[c] >= 3 and failed[c] == 0]
    assert len(spotless) >= 20


# --------------------------------------------------------------------------
# Bracketing: the association must be confounded, not causal
# --------------------------------------------------------------------------
def test_variant_count_has_no_direct_effect_within_a_stratum(dataset):
    """Bracketing drives post-delivery returns, not pre-shipment RTO.

    Marginally, multi-variant orders DO fail more - but only because they are
    overwhelmingly COD fashion orders. Holding payment method and category
    fixed, the effect should all but vanish. This is precisely why the rule
    engine caps C2 low and never counts it as evidence.
    """
    orders, res = dataset
    fit = {"apparel", "footwear", "ethnic_wear"}

    def cod_fashion(v):
        return lambda o: (o["variant_count"] == v
                          and o["payment_method"] == "cod"
                          and o["category"] in fit)

    single = rate(orders, res, cod_fashion(1))
    multi = rate(orders, res, lambda o: (o["variant_count"] >= 2
                                         and o["payment_method"] == "cod"
                                         and o["category"] in fit))
    assert abs(multi - single) < 0.06, (
        "bracketing moved RTO by %.3f within one stratum - it is behaving like "
        "a real driver, which is not the intended design" % (multi - single)
    )
