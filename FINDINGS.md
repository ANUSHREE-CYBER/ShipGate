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

---

## 2026-09-01 (later) — Step 4 groundwork: chronological feature replay

### Built

**`app/feature_normalizer.py`** — turns the two CSVs into scoreable
`OrderFeatures`, attaching the five history-derived fields the rule engine needs
(`completed_orders`, `returned_orders`, `refusals_in_last_3`,
`pincode_total_orders`, `pincode_rto_count`) plus a running merchant baseline.

**`tests/test_feature_normalizer.py`** — 14 tests, all leakage-focused. Suite
total is now 58.

Two design decisions worth recording:

**A resolution lag, not just timestamp ordering.** The brief says use only events
before an order's timestamp. That is necessary but not sufficient. A customer
ordering on Jun-10 and again on Jun-14 has the Jun-10 parcel *still in transit*
at the second checkout — nobody knows yet whether it will be refused. Counting it
uses information that did not exist at decision time. This is routine rather than
rare here: frequent buyers order every ~5.6 days and Indian COD delivery takes
3–7 days. `DEFAULT_RESOLUTION_LAG_DAYS = 5`, exposed as a parameter so the naive
behaviour stays measurable.

**`failure_mode` is dropped at the loader boundary.** `load_outcomes_csv` reads
the column and discards it, returning `order_id -> is_rto` only. Whether a
failure was a refusal or a delivery failure is after-the-fact knowledge. Making
it structurally unreachable is stronger than a comment asking people not to use it.

### Error found and fixed — an order could see its own outcome

**The tell:** first run printed *100% known customers at lag=0*. That is
impossible. A customer's first-ever order has no resolved history by definition,
so the true figure can never reach 100%.

**The cause:** the resolution loop folded in completed orders with `<=`:

```
while cursor < len(resolving) and resolving[cursor][0] <= now:
```

At `lag=0` an order's resolution time equals its own timestamp, so the loop
folded the order into the running state *before* building that same order's
feature row. Every order was scored with its own outcome already counted in its
history.

**The fix:** a strict `<`. Any strictly-earlier resolution is known; an order's
own resolution never is.

**Why this one matters more than the others.** It does not crash, raise, or
produce an obviously wrong number. It silently inflates every downstream metric —
precision, recall, PR-AUC, the entire cost table. Had it survived to the demo we
would have reported flattering numbers with no idea they were wrong. This is
exactly the failure mode CLAUDE.md's "suspiciously perfect metrics are a signal
to check for leakage" rule exists to catch, and the catch came from a sanity
check on a percentage rather than from any test.

**Pinned:** `test_no_order_sees_its_own_outcome` runs at lags 0, 1, 5 and 30 and
asserts every customer's first-ever order shows zero completed orders, zero
returns, zero recent refusals. A second test,
`test_history_matches_an_independent_replay`, recomputes history for the five
busiest customers by brute force and demands identical counts.

### Finding 6 — the resolution lag is cheap insurance, not a large correction

| Lag | Known customers | Orders with a recent refusal | Mean baseline |
|---|---|---|---|
| 5 days | 4,820 (48.2%) | 1,420 | 0.187 |
| 0 days | 4,933 (49.3%) | 1,456 | 0.185 |

A 1.1-point difference. Most customers order infrequently enough that their
previous parcel has already landed, so the in-transit overlap affects only the
frequent-buyer tail. Worth keeping — it costs nothing, it is the honest model,
and it removes an objection a reviewer could otherwise raise — but it is not
doing heavy lifting, and the headline metrics would not move much without it.

(Both figures are post-fix. Pre-fix, lag=0 reported 100% known customers, which
was the bug rather than a measurement.)

### Finding 7 — the rules separate risk cleanly on independent data

Smoke test only: whole dataset, no train/test split, no thresholds tuned. The
rule engine scored the replayed features and the tiers were compared against the
independently generated outcomes.

| Tier | Orders | Share | Actual RTO rate |
|---|---|---|---|
| Low | 4,804 | 48.0% | **7.2%** |
| Medium | 4,711 | 47.1% | **27.8%** |
| High | 434 | 4.3% | **49.1%** |
| Very high | 51 | 0.5% | **70.6%** |

Monotone across all four tiers with a roughly ten-fold spread from Low to Very
High. This is the first evidence that the hand-designed point weights carry real
signal, and it is earned rather than circular: the outcomes came from a separate
module with different coefficients, hidden factors, and its own RNG seed, and
the features contain no information from after each order's timestamp.

Supporting numbers: scores span 0–107, median 35. Evidence is present on 16.2% of
orders. Safeguards fired 150 times for `insufficient_evidence_for_high` and 4
times for `pincode_not_pivotal` — both live, neither decorative.

**The tension to watch:** Medium holds 47.1% of all orders, and that population
delivers fine 72.2% of the time. Every one of those is a confirmation step
applied to a mostly-good customer. Whether that trade is worth it is precisely
what the cost table has to answer, and it is likely to be the most interesting
result of Step 4 rather than the headline PR-AUC.

**Caveat:** this is the full dataset, not a held-out test set, so it is a shape
check and not a result. Nothing from this table goes in the README until it has
been recomputed on the later 30% only.

### Carried into `evaluation.py`

- 70/30 split by timestamp. All reported metrics from the later 30% only; the
  earlier 70% is for threshold selection and tomorrow's calibration layer.
- PR-AUC on the raw score, precision/recall/confusion matrix at each tier
  boundary, broken down by new-vs-known customer and by merchant cohort.
- Cost table at TP +₹200 / FP −₹300 / FN −₹200 / TN +₹300, with conservative /
  balanced / aggressive policies side by side.
- A "flag every COD order" reference row. Without a baseline a PR-AUC number has
  nothing to be judged against, and given COD is 55.6% of orders and fails at
  30.4%, that baseline is not weak.
- Expect C2 to show marginal predictive power that vanishes under stratification —
  report it that way, do not let it inflate a headline metric.
- Suspiciously strong metrics are a signal to hunt for leakage, not to celebrate.
  With `RELIABILITY_COEF = 0.85` of unobservable per-customer variance plus
  courier and weather noise, a high PR-AUC would be evidence of a bug. The lag=0
  incident above is the precedent.

---

## 2026-09-01 (Step 4) — chronological evaluation and the cost model

### Built

**`app/evaluation.py`** — 70/30 chronological split, PR-AUC, operating points at
every tier boundary, the required breakdowns, the brief's cost table, and a
graduated per-action cost model.

**`tests/test_evaluation.py`** — 23 tests. Suite total is now 81.

`requirements.txt` pins `scikit-learn==1.9.0`, used for
`average_precision_score` and nothing else. Confusion matrices and all cost
arithmetic are computed directly so they stay readable and auditable rather than
hidden behind a library call.

Split: train 7,000 orders (Jun-01 → Aug-09), test 3,000 (Aug-09 → Aug-29). Every
number below is from the test slice.

### Judgement call — the headline PR-AUC is COD-only

Prepaid orders fire nothing in the payment group and almost all land in Low.
Including them lets a ranking metric take credit for separating prepaid from COD,
which is not a prediction — payment method is known at checkout.

| Metric | Value | Prevalence baseline | Lift |
|---|---|---|---|
| **PR-AUC, COD only** | **0.417** | 0.312 | 1.34× |
| PR-AUC, all orders | 0.392 | 0.199 | 1.97× |

Note the all-orders figure has the *higher* lift while being the less honest
number. Both are printed so the gap is visible rather than hidden. **Quote 0.417.**

Modest, and it should be. A large share of failure variance is invisible to the
rules by construction (`RELIABILITY_COEF = 0.85`, courier strain, weather). This
landed at the low end of the 0.4–0.55 predicted before running, so no leak-hunt
was triggered.

### Operating points (test slice, COD only)

| Policy | TP | FP | FN | TN | Precision | Recall | F1 |
|---|---|---|---|---|---|---|---|
| medium+ (confirm and above) | 481 | 1,020 | 42 | 131 | 0.320 | 0.920 | 0.475 |
| high+ (nudge and above) | 114 | 131 | 409 | 1,020 | 0.465 | 0.218 | 0.297 |
| very_high (review only) | 20 | 10 | 503 | 1,141 | 0.667 | 0.038 | 0.072 |

### Breakdowns (COD test orders)

| Segment | n | RTO | PR-AUC | vs baseline |
|---|---|---|---|---|
| new (no resolved history) | 621 | 31.1% | 0.324 | 1.04× |
| known | 1,053 | 31.3% | 0.454 | 1.45× |
| gemstone | 359 | 29.8% | 0.401 | 1.35× |
| fast_fashion | 1,315 | 31.6% | 0.423 | 1.34× |

**The rules are near-useless on strangers.** New customers score 1.04× baseline —
barely better than random ranking. Known customers reach 1.45×. That is the
honest characterisation and it should be stated plainly rather than averaged
away: ShipGate's value comes almost entirely from customers with delivery
history. Consistent with Finding 2 — for a first-time buyer there is genuinely
almost nothing to go on.

Cohort PR-AUCs are close (0.401 vs 0.423), consistent with Finding 3.

### Error found and fixed

**Format string crash.** `"INR %+,.0f"` — `%`-formatting does not support comma
grouping, unlike `str.format`. `ValueError: unsupported format character ','`.
Replaced with a `_money()` helper using `"{:+,.0f}".format`. Trivial, caught on
first run, recorded only because this log claims to record everything.

Also removed two unused imports (`bisect`, `tier_from_score`) left from an
earlier draft of the threshold sweep.

### Finding 8 — the brief's cost table argues against the product

This is the significant result of Step 4 and it was escalated mid-step rather
than reported at the end.

Under the brief's numbers, per order:

```
flag:        p·(+200) + (1−p)·(−300)
don't flag:  p·(−200) + (1−p)·(+300)
```

Flagging wins only when **p > 0.60**. Missing a bad order costs 400 relative to
catching it; disrupting a good one costs 600 relative to leaving it alone.

| Policy | Flagged | Precision | Recall | Net value |
|---|---|---|---|---|
| reference: flag nothing | 0 | — | — | **₹+602,000** |
| value-optimal on train (≥87) | 28 | 0.643 | 0.030 | ₹+603,200 |
| conservative (≥61) | 248 | 0.468 | 0.195 | ₹+569,200 |
| balanced (≥31) | 1,614 | 0.305 | 0.827 | ₹+126,600 |
| reference: flag every COD | 1,674 | 0.312 | 0.878 | ₹+120,600 |
| aggressive (≥21) | 1,960 | 0.280 | 0.921 | ₹−25,000 |
| reference: flag everything | 3,000 | 0.199 | 1.000 | ₹−602,000 |

**Doing nothing is very nearly optimal.** The best policy the rules can find beats
shipping everything blind by ₹1,200 across 3,000 orders — about 40 paise per
order. Our own Medium tier destroys ₹475,400 relative to inaction.

The arithmetic is correct and pinned by `test_break_even_probability_is_sixty_percent`.
The threshold of 87 was selected on train and applied unchanged to test, so this
is not a hindsight artifact.

**Root cause:** the −300 false-positive cost prices *every* intervention as
"customer lost entirely, at full margin". That is the correct price for a hard
block. It is the wrong price for a confirmation SMS, which is what the Medium
tier actually does — and "least-disruptive action" is the entire pitch. As
specified, the hero artifact proved the opposite of what the demo needs.

### Finding 9 — the brief's table is exactly a hard block, and that is the argument

Rather than change the brief's numbers, the resolution was to notice what they
are. Modelling an action with three parameters — how often it prevents a real
RTO, how often it loses a good customer, what it costs to run — and setting
`prevent_rate = abandon_rate = 1.0` **reproduces the brief's +200/−300 swings
exactly**. The brief's table is not a generic cost table; it is the arithmetic of
a hard block.

Absolute accounting used by the graduated model, per order:

```
delivered and kept   +300   (margin)
came back as RTO     -200   (two-way freight)
abandoned/cancelled     0   (no sale, but no freight either)
minus the action's operating cost
```

`test_block_action_reproduces_the_briefs_swings` asserts the equivalence, so it
is a derivation rather than a claim in a docstring.

Each action then gets its own break-even, and **every tier clears the one
belonging to its action**:

| Action | Prevents | Abandons | Op cost | Break-even | Tier's measured RTO |
|---|---|---|---|---|---|
| confirmation / OTP | 35% | 3% | ₹5 | 17.7% | **27.6%** ✓ |
| prepaid nudge | 55% | 12% | ₹15 | 34.9% | **49.1%** ✓ |
| manual review | 75% | 20% | ₹40 | 47.6% | **66.7%** ✓ |
| hard block | 100% | 100% | ₹0 | 60.0% | — |

| Policy | Net value | vs shipping everything |
|---|---|---|
| ship everything (do nothing) | ₹602,000 | — |
| **ShipGate graduated tiers** | **₹617,748** | **+₹15,748** |
| confirm every order | ₹607,084 | +₹5,084 |
| hard block Very High only | ₹603,000 | +₹1,000 |
| hard block everything above Low | ₹365,500 | **−₹236,500** |

Operational load: 1,390 ship · 1,409 confirm · 171 nudge · 30 review.

**The demo's strongest single number is the last row.** Applying blunt blocking to
*exactly the same orders* ShipGate intervenes on destroys ₹236,500, while the
graduated policy gains ₹15,748. Same detection, same tiers, same scores — the
entire ₹252,000 difference is choosing the least-disruptive action instead of the
harshest one. That is the locked pitch, demonstrated rather than asserted.

### The weakest link, stated plainly

The prevent/abandon rates are **assumptions about human behaviour that no
synthetic data can supply**. Nothing in this project measures how many real
customers abandon after an OTP prompt. The report prints the sensitivity rather
than burying it:

| Action | Assumed abandon | Stops paying above |
|---|---|---|
| confirmation / OTP | 3% | **7%** |
| prepaid nudge | 12% | 26% |
| manual review | 20% | 60% |

**The confirmation row is the fragile one.** 7% abandonment on an extra checkout
step is entirely plausible in reality, and confirmation carries 1,409 of the
1,610 interventions — so most of the ₹15,748 rests on that assumption. If a judge
pushes on one number, it will be this one. The correct answer is that the model
is explicit about exactly where it breaks, and that a real deployment would
measure the rate rather than assume it. Do not present ₹15,748 as a projection of
real savings.

### Open question, deferred by decision

Medium tier flags 54% of test orders at 0.305 precision. Under graduated costs it
is justified (27.6% RTO against a 17.7% break-even) and contributes most of the
gain — but that rests entirely on the 3% abandonment assumption above. Decision
taken to leave the tier boundaries alone and revisit only if that assumption
starts to look optimistic. Tuning rule boundaries against a metric derived from
synthetic data is precisely what CLAUDE.md warns against.
