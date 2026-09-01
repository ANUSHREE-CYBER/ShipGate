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

---

## 2026-09-01 (Step 5) — the policy engine

### Built

**`app/policy_engine.py`** — maps a risk assessment to one of four actions under
merchant-configurable policy, and produces the full decision record the audit
trail needs.

**`tests/test_policy_engine.py`** — 24 tests. Suite total is now 105.

Small refactor to `app/rule_engine.py`: the safeguard block inside `assess()` was
extracted into a public `apply_safeguards()`. Behaviour is unchanged and
`RULE_VERSION` stays `1.0.0` — all 28 existing rule-engine tests pass untouched.
`RiskAssessment` also gained an `order_id` field, because an assessment of an
order should know which order it assessed.

### Design — what a merchant may and may not configure

The locked pitch says "merchant-configurable", so configurability is the point of
this module rather than a nicety. The constraint that makes it safe:

**May configure:** score boundaries (`thresholds`), a gentler action for any tier
(`tier_actions`), and a ceiling on the harshest action they will take at all
(`max_action`).

**May not configure:** anything that reaches a harsh action the evidence does not
support. The policy layer derives a tier from the merchant's own thresholds and
then hands it to `rule_engine.apply_safeguards` — the identical function
`assess()` uses.

**There is no block action, and none can be configured.** `Action` is
`ship < confirm < nudge < review` and nothing else. A merchant using ShipGate
cannot refuse a customer outright through it. This is structural, not policy:
Step 4 measured that blunt blocking is both disproportionate and economically
worse (−₹236,500 against +₹15,748 on the same orders).

Validation refuses incoherent configurations at construction: non-ascending
thresholds, a mapping where a higher tier gets a gentler action than a lower one,
a Low tier that does anything other than ship, or a missing tier.

### Error found and fixed — the policy layer silently undid Safeguard 3

**The tell:** the default policy produced 4,800 `ship` decisions where the rule
engine had placed 4,804 orders in Low. A four-order discrepancy where there
should have been none.

**The cause:** `decide()` re-derived a tier from the merchant's thresholds and
then re-applied *only the evidence gate*, reimplementing half of the engine's
safeguards. The pincode safeguard was not reapplied, so four orders the engine
had demoted via `pincode_not_pivotal` were escalated straight back up.

Those four orders — ORD-007518, ORD-008448, ORD-008891, ORD-009879, all scoring
31 with `evidence_score` 0 — are clean customers in high-RTO pincodes. The rule
engine demoted them to "ship normally". The policy layer put them back to
"confirm". **That is exactly the case Safeguard 3 was written for**, and exactly
the case the Aug 31 hand-check singled out as the one that mattered
("clean customer in a bad-delivery pincode → knocked back down to ship normally").

**The fix:** stop duplicating. `apply_safeguards()` now lives in `rule_engine`
and both `assess()` and `decide()` call it. It takes a `tier_fn` so the policy
layer asks "would removing D2 lower the tier?" against the *merchant's*
boundaries rather than the default ones. Two copies of a safeguard means one of
them is out of date; now there is only one.

**Why this was fixed and reported rather than escalated mid-step:** it changed
nothing about the design and contradicted no decision — the safeguard was
correct, my implementation of the new layer failed to honour it. Nothing ShipGate
claims had to change. Had the safeguard itself turned out to be unworkable, that
would have been a stop.

**Pinned:** `test_pincode_safeguard_survives_the_policy_layer` uses a
purpose-built order (P1 +25 and D2 +6 = 31, one band above the 25 it would score
without the pincode points) and asserts it still ships.
`test_default_policy_reproduces_the_engine_tier_exactly` walks all 10,000 orders
and asserts the two layers never disagree.

### Verified — configuration cannot escape the safeguards

Action mix across all 10,000 orders under three policies:

| Policy | ship | confirm | nudge | review | Safeguard demotions |
|---|---|---|---|---|---|
| default (31/61/86) | 4,804 (48.0%) | 4,711 (47.1%) | 434 (4.3%) | 51 (0.5%) | 154 |
| gentle (41/71/91, capped at nudge) | 6,805 (68.0%) | 3,173 (31.7%) | 22 (0.2%) | 0 | 19 |
| strict (21/51/76) | 3,601 (36.0%) | 5,526 (55.3%) | 757 (7.6%) | 116 (1.2%) | 746 |

Default reproduces the engine's tier distribution exactly (4,804 / 4,711 / 434 /
51), and its 154 demotions are the 150 evidence-gate plus 4 pincode cases already
recorded in Finding 7. That equality is now a test.

**The strict row is the interesting one for the demo.** A merchant who drops
every boundary gets stopped **746 times** from reaching a harsher action than the
evidence supports. Being merchant-configurable does not mean the safeguards are
optional, and that number demonstrates it rather than asserting it.

### Hand-checked decisions

| Order | Score | Evidence | Engine tier | default | gentle | strict |
|---|---|---|---|---|---|---|
| Clean prepaid regular, ₹800 books | 0 | 0 | low | ship | ship | ship |
| Repeat refuser, COD ₹3,500 apparel | 85 | 40 | high | nudge | confirm | review |
| Clean customer, bad pincode, COD ₹1,500 | 31 | 0 | low | ship | ship | **confirm** |
| Stranger, COD ₹12,000 apparel, 2 variants | 68 | 0 | medium | confirm | confirm | confirm |

Row 3 is worth understanding rather than treating as a bug. Under `strict`
(medium starts at 21), the order scores 31 *and would still score 25 without the
pincode points* — 25 is above 21, so it lands in Medium either way. D2 is
genuinely not the pivotal rule under those boundaries, so the safeguard correctly
does not fire. The safeguard asks "did this rule change the outcome?", and the
answer depends on where the merchant put their boundaries. That is the right
behaviour and it is why `apply_safeguards` takes a `tier_fn`.

Row 4 continues to hold the Aug 31 line: a ₹12,000 order from a total stranger
gets a confirmation step and nothing harsher, under every policy including the
strictest, because order value is not evidence.

### Overrides

`apply_override` requires an action, a reason of at least 10 characters, and an
actor, and raises otherwise. It returns a new decision rather than mutating —
`recommended_action` is never erased, so the audit trail always shows both what
the system recommended and what the human did instead. `final_action` resolves to
the override when there is one.

The 10-character minimum is a judgement call: long enough that "ok" and "fine"
fail, short enough not to be obstructive. It is a nudge toward a real reason, not
a guarantee of one — nothing can stop someone typing "aaaaaaaaaa".

### Not certain about

- **The `max_action` cap interacts oddly with `tier_actions`.** Under `gentle`,
  Very High maps to nudge and `max_action` is also nudge, so the cap never
  actually binds and `capped_at_` never appears in that policy's audit records.
  The cap is exercised by tests but not by any preset. Harmless, slightly
  redundant, worth revisiting only if the dashboard wants to display it.
- **`reasons` are generated strings, not structured data.** Good for the decision
  drawer and the audit log, awkward if the dashboard later wants to render each
  reason as its own component. Deferred until Step 8 shows what it needs.

---

## 2026-09-01 (Step 6) — the audit log

### Built

**`app/audit_service.py`** — SQLite-backed append-only log of every decision,
override and outcome, plus the read side that `GET /orders/{id}/audit` and the
dashboard timeline will use.

**`tests/test_audit_service.py`** — 29 tests. Suite total is now 134.

`data/audit.db` is generated by `python -m app.audit_service` and gitignored
(10.7 MB for 10,000 orders — reproducible, not source).

### Design — append-only, enforced by the database

"Every score, rule, threshold, override and final delivery outcome recorded in an
audit trail" is in the locked pitch. An audit trail that can be quietly edited is
not one, so nothing in this module ever updates or deletes a row. A correction is
a new row; the superseded one stays visible forever.

That is enforced by **six SQLite triggers** that `RAISE(ABORT)` on any UPDATE or
DELETE against `decisions`, `overrides` and `outcomes` — not by convention and
not by the Python API being polite. Someone who opens the file in a SQLite
browser and runs an UPDATE gets an error.

Verified by going around the Python layer entirely and issuing raw SQL:

```
UPDATE decisions SET score = 0 ...  -> audit log is append-only: decisions cannot be updated
DELETE FROM decisions ...           -> audit log is append-only: decisions cannot be deleted
UPDATE outcomes SET is_rto = 0      -> audit log is append-only: outcomes cannot be updated
DELETE FROM outcomes ...            -> audit log is append-only: outcomes cannot be deleted
```

The override reason minimum is enforced twice — once in Python, once as a schema
`CHECK (length(trim(reason)) >= 10)`. A direct INSERT with reason `'ok'` is
rejected by the database. "The reason is required" is a promise the trail makes
to whoever reads it in six months, not a form validation.

Three consequences that fall out of append-only, all tested:

- **Re-assessment appends.** An order re-scored after an address correction gets
  a second decision row. Both survive; the newest is current.
- **Outcome correction appends.** A courier feed saying "delivered" followed by a
  merchant saying "no, it came back" leaves both rows. The trail shows the
  correction happened and who made it.
- **An override never erases what it overruled.** `recommended_action` stays on
  the decision row; the override is a separate row pointing at it.

### Privacy — what is deliberately not stored

No names, phone numbers or full addresses. The log holds an order id, a merchant
id, which rules fired and by how much, the decisions taken, and the outcome. The
address appears only as its quality band, which is the only part any rule
actually used. This keeps the trail able to answer "why was this order treated
this way" without becoming a second copy of the customer database — and it is
what the README's privacy note will describe, so it needed to be true in the code
first rather than asserted afterwards.

### Error found and fixed — outcomes stamped at decision time

The backfill originally recorded each outcome with `at=record.timestamp`, the
same instant as the decision. Every audit timeline therefore showed an order
being scored and its delivery result arriving simultaneously, which is both
factually wrong and would have looked wrong on the dashboard's timeline — the
one screen whose entire job is showing a sequence of events.

Fixed to stamp outcomes at `timestamp + DEFAULT_RESOLUTION_LAG_DAYS`, reusing the
same 5-day lag the feature replay uses. A real timeline now reads:

```
2026-08-13T22:25:00  decision  Scored 107 (very_high) - recommended review
2026-08-18T22:25:00  outcome   Recorded as RTO (source: simulation)
```

Small bug, but it is exactly the kind that survives to a demo video because
nothing fails — the numbers were all correct, only the story they told was wrong.

### Test-expectation error worth recording

`test_reassessment_appends_rather_than_replacing` initially asserted that a
repeat refuser re-scored with a clean record would drop to `ship`. It drops to
`confirm`. The order is COD ₹3,500 apparel: P1 +25, P2 +10, C1 +10 = 45, with the
trust discount clamped to 0 by the history group's floor. 45 is Medium.

**The code was right and my expectation was wrong.** Worth recording because the
tempting move is to "fix" the code to match the test. The trust discount being
clamped at 0 rather than pulling the total down is Safeguard 2 working exactly as
designed — a clean history cannot cancel out the fact that this is still a
mid-value COD apparel order.

### Verified against the full dataset

`python -m app.audit_service` over all 10,000 orders:

| | |
|---|---|
| decisions | 10,000 |
| distinct orders | 10,000 |
| outcomes | 10,000 |
| action mix | ship 4,804 · confirm 4,711 · nudge 434 · review 51 |
| review queue | 51 orders, none yet handled |

The action mix matches `policy_engine`'s output exactly, so nothing is lost or
altered in the round trip through SQLite.

Top of the review queue, ordered by score:

| Order | Score | Evidence | Tier |
|---|---|---|---|
| ORD-007587 | 107 | 50 | very_high |
| ORD-006720 | 103 | 40 | very_high |
| ORD-008588 | 103 | 40 | very_high |

`review_queue()` deliberately excludes anything already overridden — a handled
review leaves the queue, which is what makes it a queue rather than a list.

### Hand-checked audit trail

A single order taken through the full lifecycle, including a correction:

```
2026-08-20T10:00:00  decision  Scored 85 (high) - recommended nudge
2026-08-20T11:30:00  override  anushree overrode to ship: regular wholesale buyer, verified by phone
2026-08-25T09:00:00  outcome   Recorded as delivered (source: courier)
2026-08-27T16:00:00  outcome   Recorded as RTO (source: merchant)
```

Final action `ship`, current outcome RTO, and both the overruled `nudge` and the
superseded `delivered` still on the record. This is the screenshot for the demo:
it shows the system's recommendation, a human disagreeing with a stated reason,
the merchant paying for that disagreement, and none of it being quietly tidied
away.

### Not certain about

- **`review_queue` ordering is by score, not by age.** A reviewer working top-down
  processes the riskiest first, which is defensible, but an order can in
  principle sit at the bottom indefinitely. No aging or SLA. Fine for a demo;
  a real deployment would want one.
- **Nothing enforces that a recorded outcome belongs to a known order.** The
  `outcomes` table has no foreign key to `decisions`, deliberately — an outcome
  can legitimately arrive for an order ShipGate never scored. But it also means a
  typo in an order id creates an orphan outcome silently. The API layer in Step 7
  should probably warn on that; noting rather than fixing since it is an endpoint
  concern.
- **`get_audit` loads every decision for an order.** Fine at 1–2 decisions per
  order. If an order were re-scored hundreds of times it would be wasteful, and
  there is no pagination.

---

## 2026-09-01 (Step 7) — the API

### Built

**`app/main.py`** — FastAPI wrapper exposing exactly the four endpoints CLAUDE.md
specifies, and nothing else.

**`tests/test_main.py`** — 30 tests. Plus 11 more added to
`tests/test_audit_service.py` for the orphan guard and batched writes. Suite
total is now 175.

`requirements.txt` pins `fastapi==0.141.1`, `uvicorn==0.52.4`, `httpx==0.28.1`
(httpx is what FastAPI's `TestClient` runs on).

```
POST /risk/assess          score an order, return tier + action + reasons
POST /orders/{id}/outcome  record whether it actually became an RTO
POST /orders/{id}/override merchant override, requires a reason
GET  /orders/{id}/audit    full audit trail for one order
```

`test_only_the_four_endpoints_exist` asserts the route table contains exactly
those four. Scope freeze is a working rule, so it is enforced by a test rather
than by remembering.

### Design — ShipGate has no customer database, on purpose

`/risk/assess` takes the customer-history counts (`completed_orders`,
`returned_orders`, `refusals_in_last_3`) and the pincode counts as **inputs**
rather than looking them up. Those facts belong to the merchant's own order
system.

This is the same stance the audit log already takes by storing no names, phone
numbers or addresses. ShipGate is a decision layer over signals it is handed, not
a second copy of the customer database. It is also what makes the locked pitch's
"whether from transparent local rules or an upstream risk provider" literally
true of the code: the service does not care where the signals came from.

`/risk/assess` writes its decision to the audit log as a side effect. An
assessment nobody can later account for is not much use to a merchant being asked
why a customer was treated a particular way.

### Requested change — validation on the outcome endpoint

Step 6 flagged that nothing tied an outcome to a known order, so a typo in an
order id would silently create an orphan row. That is now closed.

`AuditLog.record_outcome` refuses an order that has never been scored, raising
`UnknownOrderError` (a `ValueError` subclass, so existing callers that catch
`ValueError` keep working, but distinguishable enough for the API to answer 404
rather than 400 — "I have never heard of this order" is a different problem from
"your request was malformed").

The check is deliberate policy rather than a schema foreign key, and can be
waived with `require_known_order=False` for the one legitimate case: a merchant
back-loading historical outcomes for orders ShipGate never saw. The default is
strict, because the overwhelmingly likelier cause of an unrecognised id is a
typo, and a silent orphan in an audit log is worse than an error.

Live, against a real uvicorn server:

```
POST /orders/LIVE-TYPO/outcome  ->  HTTP 404
  no decision has ever been recorded for order 'LIVE-TYPO', so there is
  nothing for this outcome to attach to - check the order id
```

`test_a_refused_outcome_leaves_nothing_behind` asserts the point of the guard: it
is not merely that the caller gets an error, it is that the table stays empty.

The same guard covers overrides, via `latest_decision_id`.

### Error semantics

| Code | Meaning |
|---|---|
| 404 | this order has never been scored, so there is nothing to attach to |
| 422 | understood but unacceptable — reason too short, unknown action, malformed field, unknown policy |

Every refusal carries a message that says what to do about it. The unknown-policy
error lists the policies that do exist; the short-reason error states the
minimum and echoes what was sent.

Verified live:

```
POST /orders/LIVE-1/override {"reason": "ok"}  ->  HTTP 422
  an override needs a reason of at least 10 characters - got 'ok'
```

An override with `"action": "block"` is rejected at the schema level. There is no
block action anywhere in ShipGate, and the API must not be the place one gets
invented.

### Performance problem found and fixed — 107s backfill

Rebuilding `data/audit.db` from the 10,000-order dataset took **107 seconds**.
The cause was not the volume: 2,000 decisions into an in-memory database take
0.1s. It was that every `record_*` call committed its own transaction, so the
backfill did 20,000 separate commits and therefore 20,000 fsyncs.

Per-call commits are *right* for the API — a decision should be durable the
moment it is returned to the caller. They are wrong for a bulk load. Added an
`AuditLog.batch()` context manager that holds one transaction open across many
appends.

**107s → 1.5s**, a ~70× improvement, and the counts and action mix come out
identical.

This mattered enough to fix rather than note because the README promises
one-command setup, and two minutes of an apparently-hung terminal is a bad first
impression in a demo video.

Nothing about the append-only guarantee changes — `test_append_only_still_holds_inside_a_batch`
asserts the triggers still fire during a batch. A failed batch rolls back whole
rather than leaving the log half-written, which for an audit log is the
behaviour you want:
`test_a_failed_batch_leaves_no_half_written_trail`.

### Verified end to end against a live server

Not just `TestClient` — a real uvicorn process, driven with curl:

```
POST /risk/assess       action=nudge tier=high score=85 evidence=40
POST .../override       422, reason too short
POST .../override       accepted with a real reason
POST .../outcome        accepted
GET  .../audit

  23:19:11 decision  Scored 85 (high) - recommended nudge
  23:19:12 override  anushree overrode to ship: regular wholesale buyer, verified by phone
  23:19:12 outcome   Recorded as RTO (source: courier)

  final_action=ship  current_outcome=True
```

The reasons returned by `/risk/assess` are the ones a merchant would actually
read:

```
Score 85 of a possible 145 puts this order in the high band for merchant 'default'.
Largest contributors: Repeat refusal +40 (H1); COD order +25 (P1); High-value COD +10 (P2).
Evidence score 40 - there is a real signal about this customer or address, not just context.
Action chosen: nudge (the least disruptive step this policy allows at this tier).
```

Both `/risk/assess` and `/orders/{id}/audit` carry the synthetic-data disclaimer
in the response body, so the caveat travels with the data rather than living only
in the README.

### Test-expectation error worth recording

`test_override_without_a_real_reason_is_refused` initially indexed
`r.json()["detail"][0]["msg"]`, assuming Pydantic's list-of-errors shape. Our
short-reason refusal comes from `HTTPException`, whose `detail` is a plain
string. The status code was right all along; the assertion was wrong. Second time
this has happened — my test expectation being wrong rather than the code — and
both times the temptation was to change the code first.

### Not certain about

- **A new SQLite connection is opened per request.** Correct and simple, since
  connections are not thread-safe and FastAPI runs sync endpoints in a
  threadpool. It also means every request pays connection setup. Fine at demo
  scale; a real deployment wants a pool.
- **No authentication of any kind.** Anyone who can reach the port can override a
  decision as any actor they care to name. The `actor` field is self-reported and
  entirely untrusted. This has to be stated plainly in the README rather than
  left for a judge to notice — an audit trail whose "who did this" field is
  unauthenticated is weaker than it looks.
- **`_db_path` is a module-level global**, overridden by tests via monkeypatch.
  It works and is honest, but a settings object would be tidier if the dashboard
  step needs to point at a different database.

---

## 2026-09-01 (Step 8) — the dashboard

### Built

**`frontend/`** — React 18 + Vite dashboard with four views the brief calls for:
order queue, decision drawer, audit timeline, cost comparison.

**`app/main.py`** — added `GET /orders` plus a static mount that serves the built
dashboard from the same origin as the API.

**`app/audit_service.py`** — added `list_orders()`, the query behind the queue.

**`app/evaluation.py`** — added `report_data()` and a `--json` flag that emits
the evaluation numbers for the cost view.

**`tests/test_main.py`** — 13 more tests for the queue endpoint. Suite total is
now 188.

### Contradiction found in CLAUDE.md, escalated before building

CLAUDE.md requires a dashboard with an **order queue**, and separately freezes the
API at **four endpoints, "only these"**. Those cannot both hold: every one of the
four requires an order id you already have, so nothing can list orders. A
dashboard built on them alone would be a lookup box, not a queue.

Flagged and agreed rather than resolved quietly. `GET /orders` was added — one
read-only listing over data the other endpoints already produce, adding no new
behaviour — and **CLAUDE.md's endpoint list was updated to match** so the
document and the code do not drift apart. The list is frozen again at five, and
`test_only_the_five_endpoints_exist` enforces it.

The alternative considered and rejected: generating the queue as a static JSON
snapshot to preserve the four-endpoint freeze. Scope-pure, but the queue would be
frozen, so an override performed live in the demo video would not update the list
until the snapshot was regenerated. A visible wart on camera in exchange for a
technicality.

### The cost view reads a static file, and that is the correct design

`evaluation.json` is generated by `python -m app.evaluation --json`, not served
by an endpoint. These numbers come from a batch evaluation over a held-out time
slice; the 70/30 split is a property of the whole dataset rather than of any one
order, so recomputing per request would be both slow and conceptually wrong.
Generating it as a build artifact is the honest shape, not a shortcut.

### Design decisions worth recording

**The dashboard never patches local state after a write.** Every override or
outcome triggers a re-read from the audit log. A dashboard that showed a
different final action from the audit trail would undermine the single thing this
product claims, and the cheapest way to guarantee they agree is to never keep a
second copy.

**The queue shows the overruled recommendation as well as the final action** —
rendered as `review → confirm` with the original struck through, not replaced.
Same principle as the audit log: an override never erases what it overruled.

**Built output is committed.** `frontend/dist/` is in git so a judge can clone
the repo and run the demo with one command and no Node toolchain at all.
Committing build artifacts is unusual and will be stated in the README rather
than left to be discovered. `frontend/node_modules/` is gitignored.

**One origin, one process.** FastAPI serves the built bundle, so there is no CORS
configuration and no second server to start. The Vite dev proxy exists only for
`npm run dev`.

**The static mount is conditional.** If `frontend/dist` does not exist because
nobody has run the build, the API still works and only the UI is missing. A
missing dashboard should not take the service down.

### Errors found and fixed

**1. React key warning in the rule table.** Rules are grouped by rule group with
a fragment per group, and the fragment carried no key — React warns and, worse,
can reuse DOM nodes across groups. Switched to `<Fragment key={group}>`. Caught
by reading the code rather than by a test, since there is no JS test runner.

**2. Stale module docstring.** `main.py` still opened with "the four endpoints,
and nothing else" after the fifth was added. Trivial, but this file is the first
thing a judge reads when they open the API, and a doc that contradicts its own
route table is worse than no doc.

### Verified end to end against a live server

Real uvicorn, real HTTP, dashboard served from the same origin:

```
GET /                            200   (React shell)
GET /assets/index-*.js           200
GET /assets/index-*.css          200
GET /evaluation.json             200

GET /orders?limit=4              total 10,000
  ORD-007587 score=107 very_high review  outcome=True
  ORD-006720 score=103 very_high review  outcome=True

POST /orders/ORD-007587/override {"action":"confirm", ...}
GET /orders?overridden=true
  ORD-007587  recommended=review -> final=confirm  by anushree
```

The override round-trips through the queue exactly as the drawer will drive it.

Cost view data, read straight from the served JSON:

```
ShipGate vs nothing: +15,748 | blunt block: -236,500 | spread: 252,248
actions justified: confirm, nudge, review
```

### What the cost view leads with

The headline tile is **₹252,248 — "same detection, gentler response"**, the gap
between the graduated policy and applying a hard block to exactly the same
orders. That is the locked pitch stated as a number rather than a claim.

Two caveats are rendered as first-class panels rather than footnotes, because
Working Rule 8 says the honesty caveats belong in the demo, worded the same way:

- the prevent/abandon rates are assumptions, with the abandonment rate at which
  each action stops paying printed alongside;
- **"the rules are near-useless on a first-time customer"** — stated in those
  words, next to the 1.04× segment figure that proves it.

The synthetic-data disclaimer sits permanently below the masthead on every view.

### Not certain about

- ~~**The rendered UI has not been visually confirmed.**~~ **Closed 2026-09-02.**
  Checked in a browser end to end: order queue, decision drawer, override flow,
  outcome recording and the cost evidence page all behave correctly and render
  cleanly. No visual issues found. This was the largest open risk on the
  dashboard and it is now retired — the demo video can be recorded against the
  UI as it stands.
- **There are no JavaScript tests.** The backend contract the dashboard depends
  on is covered by 43 endpoint tests, but component rendering is not. Adding
  Vitest is a real cost with three days left; the honest trade is that a
  rendering bug would be caught by looking at the page, which has to happen
  anyway.
- **`evaluation.json` can go stale.** It is generated from the dataset at build
  time. If the rules or the cost model change and nobody re-runs
  `python -m app.evaluation --json`, the cost view will quietly show old numbers.
  Nothing detects this. A version stamp comparison against `RULE_VERSION` would,
  and is worth doing if there is time.
- **The queue's default sort is by score**, so the demo opens on the very-high
  tier. Good for showing the product; it does mean the first screen is not
  representative of a typical order.

---

## 2026-09-02 (Step 9) — README and one-command setup

### Built

**`README.md`** — the public document, built entirely from measurements recorded
in this file. Nothing is claimed in it that is not traceable to a number here.

**`app/bootstrap.py`** — one command that builds every artifact and serves the
demo.

**`tests/test_bootstrap.py`** — 5 tests. Suite total is now 193.

### Why bootstrap.py was added despite the scope freeze

CLAUDE.md's README skeleton asks for "[One-command run instructions]". Setup was
four separate module invocations in a dependency order a reader would have to
infer. Writing that honestly in the README would have meant either four commands
or a shell one-liner that hides the ordering.

`python -m app.bootstrap --serve` runs generator → latent outcomes → scoring into
the audit log → evaluation, then starts the API with the dashboard attached.
**1.3–4.9 seconds from a clean slate.** It reuses existing CSVs unless `--force`
is passed, and it prints what it built.

Judged worth the file rather than scope creep: it serves a stated brief
requirement, adds no product behaviour, and directly improves the thing a judge
experiences first. Its docstring says plainly that it is a demo bootstrapper and
not a migration tool — it deletes and rebuilds the audit database every run,
which is right for a demo and would be catastrophic in production.

### Verified against the README's own instructions

Deleted `orders.csv`, `outcomes.csv` and `audit.db`, then ran exactly what the
README tells a reader to run:

```
[1/4] generating 10000 synthetic orders
[2/4] deciding outcomes with independent latent logic
[3/4] scoring every order and writing the audit log
[4/4] evaluating on the held-out later 30% and emitting cost data
built in 4.9s

dashboard: http://127.0.0.1:8080/     -> 200
api docs:  http://127.0.0.1:8080/docs -> 200
/orders                                -> 200
/evaluation.json                       -> 200
```

`test_forcing_regenerates_identical_data` asserts a forced rebuild is byte-for-byte
identical, so the seeds hold.

### Compliance checks run against the README, not assumed

CLAUDE.md carries a list of claims that must never appear and wording that must
appear verbatim. Both were checked mechanically rather than by rereading:

| Check | Result |
|---|---|
| "only blocks" / "fraudster" / "better than Razorpay" / "replace" / "proves real-world" | none present |
| Limitations paragraph, verbatim from CLAUDE.md | present, whitespace-normalised match |
| Locked pitch, verbatim | present |
| "Synthetic simulation result — validates policy logic, not production accuracy." | present, three times |

The README instead says Razorpay's COD Intelligence "already offers confirmation,
address-correction and prepaid-incentive flows for medium-risk orders" and that
ShipGate "does not compete with that detection. It complements it."

### Error found and fixed — an unverified number in my own draft

The draft claimed *"a ₹12,000 COD order from a complete stranger scores 68"*.
Checking it: 68 requires `variant_count = 2`. The base case — one variant,
complete address, new customer — is **60** (P1 25 + P2 20 + H4 5 + C1 10).

The 68 came from a hand-check earlier in the project that happened to use two
variants, and the Aug 31 Obsidian entry quotes 72 for a third variation. Three
different numbers for "the same" illustrative order, none of them wrong in
context, all of them wrong to state without their assumptions.

Corrected to 60 with the assumptions implied by the base case. **This is the
exact failure mode this file exists to prevent** — a number remembered from a
previous session, restated in a public document, drifting from anything anyone
actually measured. Every other figure in the README was pulled from
`evaluation.json` or re-measured today.

### What the README leads with

- The locked pitch, verbatim, as the first line.
- A synthetic-data warning above the fold, before any number appears.
- The ₹252,248 graduated-versus-blunt comparison as the central cost result.
- The 1.04× new-customer figure and the sentence "the rules are close to useless
  on a first-time customer", in the Evaluation section rather than buried in
  Limitations.
- The 6.6% confirmation-abandonment break point, with the observation that
  confirmation carries 1,409 of 1,610 interventions so most of the ₹15,748 rests
  on that one assumption.
- "There is no authentication" as the first bullet of Limitations.

### Not certain about

- **The README is long.** Thorough for a judge who reads it properly; possibly
  too long for one skimming a submission list. The pitch, the warning and the
  problem statement are all above the fold, so a skim still lands on the right
  things, but a shorter version might land better. Worth a second opinion.
- **`frontend/dist` being committed will look odd to some reviewers.** It is
  stated and justified in the README, but a reviewer who dislikes committed build
  artifacts may mark it down regardless. The alternative — requiring Node — costs
  more than it saves for a three-day demo.
- **`evaluation.json` can still go stale silently.** Flagged in Step 8, still
  true: `bootstrap.py` regenerates it every run, which makes the common path
  safe, but nothing detects a hand-edited rule set with a stale JSON alongside.
- **The demo video is not recorded**, and the README references figures the video
  will need to match. If any number changes, both need updating together.
