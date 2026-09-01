# ShipGate — Findings Log

Working record of what got built, what broke, and what the data actually showed.
Written at full technical detail with real numbers, because this is the source
material for the README and the demo script — anything claimed there has to be
traceable to a measurement here.

Every number below was measured against committed code. Where a number was later
invalidated, the correction is recorded rather than the original quietly edited.

> This file starts on Sep 1. Step 1 (`rule_engine.py`, Aug 31) predates it and is
> documented in CLAUDE.md's rule tables, which were verified against the code on
> Sep 1 by asserting every documented point value.

---

## 2026-09-01 — Steps 2 and 3: synthetic data and latent outcomes

### Built

**`app/synthetic_generator.py`** — invents visible checkout data. 10,000 orders,
5,067 customers, 300 pincodes, 2026-06-01 → 2026-08-29, seed `20260901`.

**`app/latent_outcome.py`** — decides what actually happened to each order, using
logic kept independent of both the generator and the rule engine. Seed `771013`.

**`tests/test_latent_outcome.py`** — 16 tests. Suite total is now 44.

Also, earlier in the day: rewrote CLAUDE.md's rule tables to match
`rule_engine.py` exactly (the brief still described draft numbers — tiered P2
bands, graduated H5, split H1/H2, three D1 tiers were all wrong or missing), and
added `.claude/settings.json` with a permission allowlist.

### The separation, and how it is enforced

The whole point of two files is that the thing inventing the data must not be the
thing grading it. Enforcement is structural, not by convention:

- `latent_outcome.py` imports only `dataclasses`, `datetime`, `csv`, `hashlib`,
  `math`, `random`, `statistics`. No `rule_engine`, no `synthetic_generator`.
- The two modules share **no Python interface at all** — only CSV column names.
- Different functional form. The rule engine is an additive point score with
  per-group caps; the latent model is a logistic model over two competing
  failure mechanisms. Not the same numbers rescaled.
- Separate RNG seeds.
- `test_does_not_import_sibling_modules` asserts this from the AST, so it fails
  loudly if anyone ever wires them together.

Verified by AST inspection on request: stripping comments and docstrings, zero
lines of executing code in `synthetic_generator.py` contain `rto`, `risk`,
`probability`, or `fail`, and no identifier anywhere in its syntax tree matches
`rto|risk|outcome|deliver|refus|return_|score|fail`.

### Errors found and fixed

**1. `n_orders` was silently a lie — asked for 10,000, got 6,726.**
Customers were drawn until their *intended* order counts summed to the target,
but orders spilling past the 90-day window were then dropped. About a third
vanished. Fixed by turning `_build_customers` into `_customer_stream` and drawing
until *placed* orders hit the target. Now returns exactly 10,000.

**2. The repeat-purchase tail was being crushed.**
Inter-order gaps were drawn from a fixed lognormal (median ~12 days) regardless
of how many orders a customer intended. A customer meant to place 15 orders got
truncated to ~8 by the window, and `REPEAT_COUNTS` topped out at an observed max
of 10. This matters because H5's upper bands need customers with double-digit
clean deliveries to exist at all. Fixed by deriving cadence from intended volume:
`cadence = SPAN_DAYS / (intended_orders + 1)`, with lognormal noise around it.
A 15-order customer now buys every ~5.6 days, a 3-order customer every ~22.
That is also the more honest model — purchase frequency and total volume are the
same underlying engagement trait, not two independent draws.
Result: 19 customers now reach 11+ orders; max observed is 15.

**3. A ₹101,240 cash-on-delivery gemstone order.**
The lognormal value tail is realistic for that niche as a *prepaid* sale, but no
Indian merchant ships ₹1 lakh COD. Added `COD_MAX_VALUE = 25000` — a merchant
policy applied at checkout, not an outcome assumption. Max COD is now ₹24,450;
the value tail itself is untouched (max order ₹118,840, prepaid).

**4. D2's minimum-sample gate was never exercised.**
At 120 pincodes over 10k orders, only *one* pincode had under 20 orders, so the
`MIN_PINCODE_SAMPLE = 20` safeguard would never have fired in the entire dataset.
Raised to 300 pincodes. Now 49 of 300 fall below the gate, with orders/pincode
min 7, median 33, max 70.

**5. Rural pincodes were only 13% of orders.**
`PINCODE_TIER_WEIGHT` at 5.0/3.0/1.5 made the dataset metro-dominated (57%),
thinly sampling the rural address-quality and COD effects. Retuned to
4.0/3.0/2.2 → metro 44.1%, tier2 34.3%, rural 21.6%, which is a more credible
mix for a COD-heavy Indian D2C seller.

**6. Prepaid "refusals" were as common as prepaid delivery failures.**
First calibrated run gave prepaid 155 refused vs 156 undeliverable. Refusing a
parcel you have already paid for should be near-nonexistent. `PREPAID_REFUSAL_TERM`
was -2.60, which stopped being restrictive enough once `REFUSAL_BASE` rose during
calibration. Set to -3.60. Prepaid RTO fell 7.0% → 4.8%, and the split is now
57 refused vs 156 undeliverable — logistics-dominated, as it should be.

**7. Methodological gotcha worth remembering.**
Adding the COD ceiling introduced an early `return` *before* the RNG draw in
`_draw_payment_method`. That shifted the entire downstream random stream, so
every distribution moved slightly and all pre-ceiling measurements became stale
(e.g. customer count 5,000 → 5,067, rural 21.8% → 21.6%). Any control-flow change
that skips an RNG draw invalidates previously quoted figures even though the seed
is unchanged. Re-measure after touching generator control flow.

**8. My own plan had the wrong calibration target.**
The plan said "overall RTO ~28%". CLAUDE.md's 20–40% band describes **COD orders
specifically**, not the whole book. Overall is naturally lower once prepaid is
mixed in. No code was built against the wrong target; COD landed at 30.4%, which
is mid-band and correct.

### Calibration

Tuned only against published Indian COD marginals, never against rule-engine
performance. Trajectory across passes: COD 24.0% → 31.1% → 30.5% → 30.4%,
prepaid 9.4% → 7.4% → 7.0% → 4.8%.

| Segment | RTO rate | n | refused | undeliverable |
|---|---|---|---|---|
| **COD** | **30.4%** | 5,564 | 1,494 | 196 |
| Prepaid | 4.8% | 4,436 | 57 | 156 |
| Overall | 19.0% | 10,000 | 1,551 | 352 |

Deterministic: two runs at the same seed produce byte-identical `outcomes.csv`.

Generator marginals: COD share 55.6% overall (gemstone 31.8%, fast-fashion
72.8%); median order value ₹4,500 gemstone / ₹1,090 fast-fashion; address quality
complete 76.3% / minor 14.1% / major 7.4% / severe 2.2%; variant count 1 in 83.5%
of orders.

### Finding 1 — bracketing looks predictive but the association is confounded

This is the most useful result of the day, and it directly vindicates a design
decision already baked into the rule engine.

Marginally, RTO rises steadily with the number of size variants ordered:

| Variants | RTO | n |
|---|---|---|
| 1 | 18.5% | 8,351 |
| 2 | 20.7% | 1,147 |
| 3 | 23.0% | 356 |
| 4 | 28.1% | 146 |

A 9.6-point spread looks like a real signal. It is not. Holding payment method
and category fixed — COD orders in fit-uncertain categories only — it flattens:

| Variants | RTO (COD + apparel/footwear/ethnic_wear) | n |
|---|---|---|
| 1 | 31.3% | 2,549 |
| 2 | 29.8% | 620 |
| 3 | 33.0% | 227 |
| 4 | 38.3% | 107 |

The 4-variant cell sits ~1.5 standard errors above the 1-variant cell
(SE ≈ 4.7% at n=107), i.e. noise. The marginal gradient is composition:
4-variant orders are **100% fit-uncertain category and 73% COD**, versus 43% and
54% for single-variant orders.

The latent model's `REFUSAL_VARIANT_COEF` is 0.05 per extra variant — deliberately
near-zero, because bracketing drives *post-delivery returns*, not *pre-shipment
RTO*. The confounding is emergent, not planted.

**Why this matters for the pitch:** the rule engine caps C2 at 12 points and
refuses to count it as evidence. This is direct evidence that the caution was
right — a naive model would happily learn "bracketing → risk" and start
penalising customers for a behaviour that carries no independent signal.
`test_variant_count_has_no_direct_effect_within_a_stratum` pins the result.

### Finding 2 — new customers are not riskier here; H4 is unsupported

Restricted to COD orders:

| Segment | RTO | n |
|---|---|---|
| First order | 29.4% | 2,823 |
| Returning | 31.3% | 2,741 |

Returning customers fail **more**, by 1.9 points. Two real effects cancel: new
buyers carry a genuine unknown-address/unknown-person penalty
(`REFUSAL_FIRST_ORDER_TERM = 0.18`), while the returning population is
contaminated by exactly the momentum-driven repeat refusers the rules exist to
catch.

H4 awards +5 for `completed_orders == 0`. Against this data that is unjustified.
Nothing breaks — H4 is small, non-evidence, and short-circuits the history group —
but it should not be defended as data-backed. Options later: drop it, or keep it
and state plainly that it encodes uncertainty rather than measured risk. The
second is defensible and matches the rule's own wording ("small uncertainty, not
suspicion").

### Finding 3 — the two merchant cohorts do not differ on COD risk

| Cohort | RTO (all orders) | RTO (COD only) | COD n |
|---|---|---|---|
| gemstone | 13.0% | 30.8% | 1,331 |
| fast_fashion | 23.4% | 30.2% | 4,233 |

The headline gap (13.0% vs 23.4%) is almost entirely **COD mix**: 31.8% of
gemstone orders are COD against 72.8% of fast-fashion. Within COD the two are
statistically indistinguishable — 0.6 points apart.

The mechanism is a genuine cancellation. Fast-fashion has higher category refusal
coefficients (apparel 0.34, footwear 0.28, ethnic_wear 0.20 vs gemstone 0.05,
jewellery 0.12), but gemstone's much larger baskets (median ₹4,500 vs ₹1,090)
feed `REFUSAL_VALUE_COEF = 0.22` per log unit, and the two effects roughly cancel.

**Implication for the cohort threshold demo:** it still works, because the
cohorts differ sharply in base rate (13.0% vs 23.4%) and in order value, which is
what drives different optimal thresholds through the cost table. But the story is
**"different payment mix and basket size"**, not "different customer behaviour."
Do not claim the latter in the demo.

### Finding 4 — severe address defects are the strongest single visible signal

COD orders only:

| Address quality | RTO | n |
|---|---|---|
| complete | 29.9% | 4,225 |
| minor_gap | 29.7% | 790 |
| major_gap | 31.2% | 426 |
| severe | 48.8% | 123 |

Note the shape: minor and major gaps barely move the needle (−0.2 and +1.3
points), while severe jumps +18.9. This supports the rule engine's tiering —
D1-severe (+20) is the only address tier flagged as evidence, and D1-minor (+6)
is correctly treated as near-worthless. It also supports the "needs verification"
framing: this is a fixable data problem concentrated in a small slice of orders.

Pincode tier, all orders: metro 15.4%, tier2 20.3%, rural 24.4% — a real
geographic gradient, which is why D2 exists at all and why it is smoothed and
sample-gated rather than raw.

### Finding 5 — every rule now has real subjects to act on

Computed by chronological replay (each order sees only prior resolved outcomes):

| Rule | Subjects |
|---|---|
| H1 (2–3 of last 3 refused) | 312 orders, 160 distinct customers |
| H2 (exactly 1 of last 3) | 1,144 orders |
| H3 (≥5 orders, >30% / >50% return rate) | 366 eligible customers; 107 over 30%, 29 over 50% |
| H5 (≥3 clean deliveries, no refusals) | 546 customers |

Demo case: **CUST-01853** — five COD orders (Jun-26 to Aug-21), all refused, all
with `complete` addresses. Clean illustration that the driver is the hidden
reliability trait plus refusal momentum, not a data-quality problem. Good
candidate for the manual-review queue screenshot.

### Known limitation — H5's top band is dormant

H5's −30 tier requires 21+ clean deliveries. The 90-day window caps the busiest
customer at 15 orders, and the highest clean-delivery count anywhere in the data
is **13**. So the −30 band cannot fire, and −22 (11–20) fires for very few.
Nothing is broken; the band is untestable in this dataset. Decide later between
widening the window, lowering the threshold, or documenting it as untested. Being
explicit about this is better than a reviewer noticing a rule tier that never
appears in any output.

### Carried into Step 4

- Chronological replay attaching `completed_orders` / `returned_orders` /
  `refusals_in_last_3` and pincode statistics per order, computed strictly from
  events before each timestamp. This is where leakage would enter if anywhere.
- Expect C2 to show marginal predictive power that vanishes under stratification —
  report it that way, do not let it inflate a headline metric.
- Suspiciously strong metrics are a signal to hunt for leakage, not to celebrate.
  With `RELIABILITY_COEF = 0.85` of unobservable per-customer variance plus
  courier and weather noise, a high PR-AUC would be evidence of a bug.
