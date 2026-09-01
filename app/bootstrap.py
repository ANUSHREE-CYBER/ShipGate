"""One command to build everything and run the demo.

    python -m app.bootstrap --serve

Runs the pipeline in dependency order and then starts the API with the
dashboard attached:

    1. synthetic_generator  invents 10,000 orders of visible checkout data
    2. latent_outcome       decides, independently, which ones came back
    3. feature_normalizer   replays them in time order, attaching only history
       + rule_engine        that had already resolved, and scores each one
       + policy_engine
       + audit_service      writing every decision into the append-only log
    4. evaluation           measures the rules on the held-out later 30% and
                            emits the JSON the dashboard's cost view reads

Everything it writes is reproducible from fixed seeds, so a rebuild produces
byte-identical data. Nothing here is checked into git except the built
dashboard - the CSVs and the database are outputs, not source.

This is a demo bootstrapper, not a migration tool. It rebuilds from scratch
every time and will happily delete an existing audit database, which is exactly
what you want for a demo and exactly what you do not want in production.
"""

import argparse
import os
import time

DEFAULT_ORDERS = "data/orders.csv"
DEFAULT_OUTCOMES = "data/outcomes.csv"


def _step(number: int, total: int, title: str):
    print("[%d/%d] %s" % (number, total, title), flush=True)


def build(n_orders: int = 10000, force: bool = False) -> None:
    from app import synthetic_generator as generator
    from app import latent_outcome as latent
    from app.audit_service import DEFAULT_DB_PATH, AuditLog
    from app.evaluation import report_data
    from app.feature_normalizer import DEFAULT_RESOLUTION_LAG_DAYS, load_dataset
    from app.policy_engine import DEFAULT_POLICY, decide
    from app.rule_engine import assess
    from datetime import timedelta
    import json
    import pathlib

    os.makedirs("data", exist_ok=True)
    started = time.time()

    have_data = os.path.exists(DEFAULT_ORDERS) and os.path.exists(DEFAULT_OUTCOMES)
    if have_data and not force:
        print("orders.csv and outcomes.csv already exist - reusing them "
              "(pass --force to regenerate)")
    else:
        _step(1, 4, "generating %d synthetic orders" % n_orders)
        orders = generator.generate(n_orders=n_orders)
        generator.write_orders_csv(orders, DEFAULT_ORDERS)

        _step(2, 4, "deciding outcomes with independent latent logic")
        rows = latent.load_orders_csv(DEFAULT_ORDERS)
        latent.write_outcomes_csv(latent.simulate_outcomes(rows), DEFAULT_OUTCOMES)

    _step(3, 4, "scoring every order and writing the audit log")
    if os.path.exists(DEFAULT_DB_PATH):
        os.remove(DEFAULT_DB_PATH)
    records = load_dataset(DEFAULT_ORDERS, DEFAULT_OUTCOMES)
    with AuditLog(DEFAULT_DB_PATH) as log, log.batch():
        for record in records:
            log.record_decision(
                decide(assess(record.features), DEFAULT_POLICY, record.order_id),
                decided_at=record.timestamp)
            # An outcome is not known at decision time - the parcel has to go
            # out and come back. Same lag the feature replay uses.
            log.record_outcome(
                record.order_id, record.is_rto, source="simulation",
                at=record.timestamp + timedelta(days=DEFAULT_RESOLUTION_LAG_DAYS))
        counts, mix = log.counts(), log.action_mix()

    _step(4, 4, "evaluating on the held-out later 30% and emitting cost data")
    target = pathlib.Path("frontend/dist/evaluation.json")
    payload = json.dumps(report_data(DEFAULT_ORDERS, DEFAULT_OUTCOMES), indent=2)
    for path in (pathlib.Path("frontend/public/evaluation.json"), target):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")

    print()
    print("built in %.1fs" % (time.time() - started))
    print("  orders        %s" % format(counts["orders"], ","))
    print("  decisions     %s" % format(counts["decisions"], ","))
    print("  outcomes      %s" % format(counts["outcomes"], ","))
    print("  action mix    %s" % "  ".join(
        "%s %s" % (k, format(v, ",")) for k, v in sorted(mix.items())))
    if not target.exists():
        print("  NOTE: frontend/dist is missing - the API will run but the "
              "dashboard will not. Build it with: cd frontend && npm install "
              "&& npm run build")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build the ShipGate demo data and optionally serve it.")
    parser.add_argument("--serve", action="store_true",
                        help="start the API and dashboard after building")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--orders", type=int, default=10000,
                        help="how many synthetic orders to generate")
    parser.add_argument("--force", action="store_true",
                        help="regenerate the CSVs even if they already exist")
    args = parser.parse_args()

    build(n_orders=args.orders, force=args.force)

    if args.serve:
        import uvicorn
        print()
        print("dashboard: http://%s:%d/" % (args.host, args.port))
        print("api docs:  http://%s:%d/docs" % (args.host, args.port))
        uvicorn.run("app.main:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
