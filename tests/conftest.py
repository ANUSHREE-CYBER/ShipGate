"""Shared fixtures.

A handful of tests run against the full 10,000-order demo dataset rather than
a small generated fixture, because what they check - that the default policy
never disagrees with the rule engine on a single real order, that every tier
clears its own break-even - is a property of the whole dataset.

Those CSVs are generated, not committed, so a fresh clone has none. Rather than
fail with FileNotFoundError, the fixture below builds them once per session
using the same bootstrap the README tells a reader to run, and says so in the
terminal summary so the extra few seconds are not a mystery.
"""

import os
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
ORDERS = ROOT / "data" / "orders.csv"
OUTCOMES = ROOT / "data" / "outcomes.csv"

_generated_this_session = False


@pytest.fixture(scope="session")
def demo_data():
    """Ensure data/orders.csv and data/outcomes.csv exist, building if not.

    Only builds when the CSVs are missing. If they exist - the normal case on a
    machine where the demo has been run - nothing is touched, so a developer's
    audit database and any overrides recorded in it survive a test run.
    """
    global _generated_this_session
    if ORDERS.exists() and OUTCOMES.exists():
        return

    from app import bootstrap

    previous = os.getcwd()
    os.chdir(ROOT)                      # bootstrap writes relative to the repo root
    try:
        bootstrap.build()
    finally:
        os.chdir(previous)
    _generated_this_session = True


def pytest_terminal_summary(terminalreporter):
    if _generated_this_session:
        terminalreporter.write_sep(
            "-", "Auto-generated synthetic data for testing")
        terminalreporter.write_line(
            "data/orders.csv and data/outcomes.csv were missing, so the demo "
            "dataset was built once via app.bootstrap before the tests that need it.")
