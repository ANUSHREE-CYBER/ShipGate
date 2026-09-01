# CLAUDE.md — ShipGate Project Brief

**Read this file fully before writing any code. If anything you're about to build isn't in here, stop and ask instead of guessing.**

---

## What this project is

ShipGate is a decision-policy layer for COD (cash-on-delivery) return risk in Indian e-commerce, built for the Razorpay AI Buildathon 2026, Track 02 (AI Risk Manager). Deadline: **September 5, 2026**.

## The locked final pitch — use this exact wording, nowhere else

> "ShipGate is a merchant-configurable decision-policy layer for COD RTO risk. It converts risk signals — whether from transparent local rules or an upstream risk provider — into the least-disruptive action that is economically justified, with every score, rule, threshold, override, and final delivery outcome recorded in an audit trail."

15-second verbal version:
> "Risk detection tells a merchant an order may fail. ShipGate decides what to do next — ship, verify, nudge toward prepaid, or review — while making the cost and reasoning visible."

## Things we must NEVER say (fact-checked against Razorpay's real product)

- ❌ "Razorpay only blocks pincodes/IPs" — false, their COD Intelligence already does order-level, reason-based risk scoring
- ❌ "Razorpay jumps straight to blocking COD" — false, they already have confirmation/address-correction/prepaid-incentive flows for medium risk
- ❌ "ShipGate detects RTO better than Razorpay" — we cannot prove this with synthetic data
- ❌ "We replace Razorpay's COD Intelligence" — we complement it
- ❌ "Our synthetic PR-AUC proves real-world accuracy" — it proves our software/policy logic works, nothing more
- ❌ Calling any customer a "fraudster" — we predict pre-shipment COD RTO risk, not criminal intent

## What we're allowed to say instead

- "ShipGate demonstrates a transparent, merchant-controlled policy layer."
- "It optimizes the action decision, not merely the detection score."
- "Synthetic experiments validate policy behavior and trade-offs, not production accuracy."
- "A real deployment would calibrate using merchant-specific, time-ordered delivery outcomes."

---

## The problem, plainly

COD orders in India fail to deliver (RTO — Return to Origin) 20–40% of the time. Every failed delivery costs the seller shipping both ways plus handling, with nothing sold. This is a real, proven, expensive problem — Razorpay itself has a whole feature (RTO Intelligence, inside Magic Checkout) built around it, and companies like Delhivery and Cashfree sell similar tools.

## Who uses ShipGate

Small-to-medium online sellers with their own checkout (not Amazon/Flipkart marketplace sub-sellers, who don't control this).

---

## The Rule Engine — the actual core product

Every order gets a risk score from **grouped, capped rules**. No single weak signal can push an order into a harsh action alone — only real evidence (like a repeat refuser) can.

### Rule groups and caps

**This section describes `app/rule_engine.py` as built (`RULE_VERSION = "1.0.0"`). If the code and this table ever disagree, one of them is a bug — fix both together.**

**Group 1 — Payment exposure (cap 45).** Prepaid orders fire nothing here at all; COD is the precondition for refusal-at-the-door.

| ID | Rule | Points | Condition |
|---|---|---|---|
| P1 | COD order | +25 | `payment_method == "cod"` |
| P2 | High-value COD | +10 / +15 / +20 | Order value ₹2,001–5,000 / ₹5,001–10,000 / above ₹10,000 |

P2 is **tiered, not additive** — ₹2,100 and ₹15,000 are not the same exposure. Group maximum is exactly 45 (25 + 20), so the cap binds only at the top band.

**Group 2 — Customer history (cap 45, floored at 0).** The only group carrying real behavioural evidence, and the only one that can go negative.

| ID | Rule | Points | Condition |
|---|---|---|---|
| H4 | New customer | +5 | `completed_orders == 0` — **short-circuits the whole group**, no other history rule can fire |
| H1 | Repeat refusal | +40 | 2 or 3 of last 3 orders refused/returned |
| H2 | One recent refusal | +18 | Exactly 1 of last 3 refused — an incident, not yet a pattern |
| H3 | High return rate | +12 / +20 | `completed_orders >= 5` **and** return rate >30% / >50% |
| H5 | Trusted customer | −8 / −15 / −22 / −30 | `clean_deliveries >= 3` **and** zero recent refusals; 3–5 / 6–10 / 11–20 / 21+ clean deliveries |

H1 and H2 are mutually exclusive tiers. H3's `>= 5` denominator gate exists so "1 of 2 returned = 50%" can never fire. H5 is **graduated, not a flat −30**, and is disqualified outright by any recent refusal — you cannot be trusted and a recent refuser at the same time.

**Group 3 — Order context (cap 25).** Weak signals only, by design. Nothing here counts as evidence.

| ID | Rule | Points | Condition |
|---|---|---|---|
| C1 | Size-dependent category | +10 | Category in `{apparel, footwear, ethnic_wear}` |
| C2 | Variant bracketing | +8 / +12 | 2 variants / 3 or more variants (hard-capped at 12 regardless of count) |

**Group 4 — Deliverability (cap 30).**

| ID | Rule | Points | Condition |
|---|---|---|---|
| D1 | Address needs verification — minor | +6 | Landmark or floor detail missing |
| D1 | Address needs verification — major | +14 | House or street number missing |
| D1 | Address needs verification — severe | +20 | Address unparseable, or pincode missing/invalid |
| D2 | Elevated pincode RTO rate | +6 / +10 | Smoothed area rate is >1.5× / >2.0× the merchant baseline |

D1 tiers are mutually exclusive, and the wording stays "needs verification," never "risky customer" — it is a fixable data problem. D2 is empirical-Bayes smoothed (`alpha = 20`, pulled toward the merchant baseline) and gated on `pincode_total_orders >= 20`, so a handful of orders can never condemn an area.

**Formula:**
```
risk_score = clamp(payment, 0, 45) + clamp(history, 0, 45) + clamp(context, 0, 25) + clamp(deliverability, 0, 30)
```

### What counts as "evidence"

A separate `evidence_score` sums only the rules flagged as evidence: **H1 (+40), H2 (+18), H3 (+12/+20), and D1-severe (+20)**. Everything else — COD, order value, category, bracketing, pincode statistics, address gaps below severe — is *not* evidence. Order value in particular is loss magnitude, not proof of anything about the customer. The safeguard thresholds are `MIN_EVIDENCE_FOR_HIGH = 18` (one real refusal, or a severe address defect) and `MIN_EVIDENCE_FOR_VERY_HIGH = 35` (a genuine repeat-refusal pattern).

### Explicitly dropped rules
- ❌ "Late-night order" — too weak and unjustifiable, dropped entirely. Not in the code, and not to be added.

### Hard safeguards — how each one is actually enforced

1. **Weak contextual signals alone can never reach High or Very High.** After scoring, if the tier is Very High but `evidence_score < 35` it drops to High; if the tier is High but `evidence_score < 18` it drops to Medium. The **score itself is never altered** — only the tier moves, and both `tier_before_safeguards` and the reason are recorded for the audit trail.
2. **The trusted-customer discount can never cancel a deliverability problem.** Enforced structurally: each group is summed and clamped independently with a floor of 0, so a negative history subtotal stops at 0 and is arithmetically unable to subtract from any other group. `assess()` also asserts the deliverability subtotal was not modified outside its own group.
3. **Pincode risk alone can never force a restrictive action.** If D2 fired, removing its points would lower the tier, D2 is the *entire* deliverability subtotal, and `evidence_score == 0`, the tier is demoted and `pincode_not_pivotal` is logged. D2 may still contribute alongside a real address defect or refusal history — it just can never be the pivotal rule.
4. **Bracketing is a weak context signal only** — it relates more to post-delivery returns than pre-shipment RTO. Capped at 12, never marked as evidence, never treated as near-fraud.

### Risk tiers and actions (only these three actions get built — nothing else)

| Score | Tier | Action | What gets built |
|---|---|---|---|
| 0–30 | Low | Ship normally | No extra step |
| 31–60 | Medium | Confirmation/OTP simulation | Customer confirms/rejects/corrects address before shipping |
| 61–85 | High | Prepaid-incentive nudge simulation | Merchant offers small discount for switching to prepaid |
| 86+ (and `evidence_score >= 35`) | Very high | Manual review queue | Reviewer sees evidence, approves/overrides/holds, logs a reason |

Tier boundaries live in `tier_from_score()`. Scoring stops at the tier — mapping a tier to an action belongs to `policy_engine.py`, so scoring and policy stay separable.

**Do NOT build:** refundable deposits, real WhatsApp/SMS sending, real payment/refund rails, device fingerprinting, cross-merchant identity graphs. Mention refundable deposits only as a described future option in the README, never as code.

---

## The ML Calibration Layer — small, honest, secondary

- One XGBoost model, trained on fired-rule features, evaluated honestly (see evaluation section)
- Framed explicitly as a demonstration of responsible ML practice, NOT a claim that it predicts real merchant behavior
- Demo: show 2 merchant cohort profiles (e.g. gemstone/low-return vs fast-fashion/higher-return) getting different calibrated thresholds — this is a **cohort threshold table**, not a fully separate trained model per merchant
- SHAP explains only what the calibration layer adjusted, not the core rule score (which is self-explanatory by design)

---

## Synthetic Data — must be built as TWO separate, independent pieces

This is the single most important technical safeguard. Do not skip or merge these.

1. **`synthetic_generator.py`** — generates realistic order data (payment method, order value, category, customer history, address quality, pincode, timestamp over a ~90 day span)
2. **A separate latent-outcome generator** — decides whether each order actually became an RTO, using DIFFERENT coefficients and additional hidden factors the rule engine never sees (e.g. delivery-agent availability, weather disruption, random refusal chance). This must NOT reuse the rule engine's own logic/weights.

**Why:** if the same logic creates the fake data AND scores it, the evaluation is circular — we'd just be grading our own homework. Keeping them separate makes the results actually mean something.

## Evaluation — chronological, not random

- Sort orders by timestamp
- Train/tune on the first ~70%, evaluate strictly on the remaining ~30% (later in time)
- Compute any "customer history" or "pincode risk" feature using ONLY events before that order's timestamp — never future information
- Report: Precision, Recall, PR-AUC, confusion matrix, broken down by customer state (new vs known) and merchant cohort
- Every metric must be labeled: **"Synthetic simulation result — validates policy logic, not production accuracy."**

## False-positive cost table (this is a hero artifact, not an appendix)

| Outcome | Meaning | Cost/value |
|---|---|---|
| True Positive | Flagged risky order, prevented real RTO | +₹200 saved |
| False Positive | Flagged a genuine customer, caused friction/drop-off | −₹300 lost (margin) |
| False Negative | Missed a real RTO, shipped normally | −₹200 lost (two-way shipping) |
| True Negative | Correctly shipped a safe order | +₹300 gained (full sale) |

Show conservative / balanced / aggressive threshold policies side by side with their net estimated value — this directly answers the track's "honest false-positive cost" requirement.

---

## Architecture — keep it this small, resist adding more

```
Checkout/order payload
        ↓
Feature normalizer
        ↓
Rule engine (grouped, capped)
        ↓
Risk score + fired-rules trace
        ↓
Policy engine (picks least-disruptive action)
        ↓
Simulated confirmation / prepaid nudge / manual review
        ↓
Audit log (SQLite)
        ↓
Outcome ingestion (was it actually an RTO?)
        ↓
Dashboard (React) — order queue, decision detail, audit trail, cost table
```

### File structure

```
app/
  rule_engine.py        # scores an order, returns score + fired rules + group breakdown
  policy_engine.py       # turns score into an action (ship/confirm/nudge/review)
  feature_normalizer.py  # cleans/standardizes incoming order data
  audit_service.py       # writes every decision to the audit log
  synthetic_generator.py # generates fake order data (visible features only)
  latent_outcome.py      # SEPARATE logic that decides the "real" RTO outcome
  evaluation.py          # chronological split, metrics, cost table
  calibration_demo.py    # small XGBoost model + cohort threshold demo
  main.py                # FastAPI app, wires everything together
frontend/
  (React dashboard: order queue, decision drawer, audit timeline, cost comparison view)
data/
  (generated synthetic CSVs / SQLite db — not committed to git if large)
```

Every decision record must include: `rule_version`, fired rules list, score before/after caps, recommended action, merchant override (if any) + required reason, and eventual outcome.

---

## Build order — one step at a time, test by hand before the next

| Step | What it does | Tech |
|---|---|---|
| 1 | `rule_engine.py` — scores one order using grouped, capped rules | Plain Python |
| 2 | `synthetic_generator.py` — invents thousands of fake orders | Plain Python |
| 3 | `latent_outcome.py` — separately decides the "true" RTO outcome per fake order (different/hidden logic, kept independent from Step 1) | Plain Python |
| 4 | Run Step 1 against Step 2+3's data, compute precision/recall/PR-AUC | Python + scikit-learn |
| 5 | `policy_engine.py` — turns a score into an action | Plain Python |
| 6 | `audit_service.py` — logs every decision | SQLite |
| 7 | `main.py` — FastAPI wrapper, 4 endpoints | FastAPI |
| 8 | Dashboard — order queue, decision detail, audit trail, cost table | React |
| (secondary) | `calibration_demo.py` — cohort threshold demo | XGBoost + SHAP |

Do not skip ahead. Each step gets manually verified (a hand-picked clean order, a repeat-refuser, a borderline case) before the next one starts. Suspiciously perfect metrics later are a signal to check for leakage, not a result to celebrate.

## Tech stack (all free, no signups needed)

| Piece | Tool |
|---|---|
| Core scoring | Plain Python |
| Calibration layer | XGBoost |
| Explainability (calibration only) | SHAP |
| Backend/API | FastAPI |
| Frontend | React |
| Storage | SQLite |
| Data | Self-generated synthetic (two-part, see above) |

---

## API endpoints to build (only these)

```
POST /risk/assess          → score an order, return tier + action + reasons
POST /orders/{id}/outcome  → record whether it actually became an RTO
POST /orders/{id}/override → merchant override, requires a reason
GET  /orders/{id}/audit    → full audit trail for one order
```

---

## Working rules — how we build this, every single day

These apply to me (the assistant), to you, and to Claude Code. Nobody skips these.

1. **Explain before code.** Before writing any file, explain the approach in plain English first. If it doesn't match this brief, stop and flag it — don't just proceed.
2. **One file at a time.** Build one module, stop, show it, wait for review. Never chain through multiple files unreviewed.
3. **Test every module by hand before moving on.** Pick 2–3 example orders yourself (a clean one, a repeat-refuser, a borderline one) and check the output makes sense before building the next piece on top.
4. **Justify every number.** If a rule says +40, be able to say why 40 and not 30 — even if the honest answer is "starting estimate, will tune against data."
5. **Commit in small patches, not big dumps.** One commit per module/feature, with a clear message — never one giant "built everything" commit.
6. **Document daily, in Obsidian, same day — in plain, non-technical language.** What got built, what got tested, what changed and why. Write it for a reader with no technical background: short everyday sentences, no jargon, no code, no unexplained abbreviations. If a technical term is unavoidable, explain it in the same sentence the way you'd explain it to a friend. This becomes the README's reasoning section later — don't rebuild it from memory afterward.
7. **Freeze scope.** If a new idea doesn't improve the end-to-end demo, the audit trail, or the cost evidence, don't build it. No more pivots, no more external re-validation — the plan is final.
8. **No claims we haven't earned.** Every "honest metrics" and "synthetic ≠ production" caveat in this doc must appear in the real README and demo video, worded the same way.
9. **Terminal commands don't need permission; code changes still do.** Run installs, scripts, tests, and other shell commands directly without asking first — the review gate is on files, not on the terminal. Rules 1 and 2 are unchanged: every new or edited file is still explained first and shown for review one at a time. Anything destructive or outward-facing (deleting data, `git push`, force operations, anything that leaves this machine) still gets confirmed first.

---

## Day-by-day plan

| Day | Build |
|---|---|
| Aug 31 (today) | `rule_engine.py` (grouped caps) + `synthetic_generator.py` + `latent_outcome.py` (kept separate) |
| Sep 1 | Chronological split, `evaluation.py` (precision/recall/PR-AUC/cost table), small XGBoost calibration demo |
| Sep 2 | FastAPI (`main.py`, 4 endpoints), `audit_service.py`, SQLite |
| Sep 3 | React dashboard: order queue, decision detail, audit timeline, cost comparison |
| Sep 4 | README, limitations section, record 4–5 min demo video |
| Sep 5 | Buffer only — submission checks, no new development |

---

## README skeleton (fill in as we build, don't leave for the last day)

```markdown
# ShipGate

[Locked pitch paragraph goes here]

## The Problem
[RTO explanation, plain]

## How It Works
[Architecture diagram + rule groups table + action policy table]

## Evaluation
[Chronological split explanation, metrics table, cost table]
"Synthetic simulation result — validates policy logic, not production accuracy."

## Limitations
"All evaluation data in this prototype is synthetic. The reported metrics
demonstrate the behavior of the rules, policy safeguards, and evaluation
pipeline under controlled simulated conditions; they do not establish
real-world RTO prediction performance. Production deployment would require
merchant-consented historical outcomes, time-forward validation, privacy
controls, monitoring, and periodic calibration."

## Privacy Note
[One paragraph: purpose limitation, merchant-scoped pseudonymous keys, minimal PII, human override always available]

## Setup
[One-command run instructions]
```

---

## Resources and links

- Razorpay Buildathon tracks: https://razorpay.com/buildathon/
- Apply form: https://forms.gle/d9r2gvxp8cmoZhon9
- Razorpay Thirdwatch → Magic Checkout merge (background): https://razorpay.com/blog/thirdwatch-has-merged-with-magic-checkout/
- Razorpay RTO Analytics Dashboard docs: https://razorpay.com/docs/payments/magic-checkout/rto-analytics/
- FastAPI docs: https://fastapi.tiangolo.com/
- XGBoost docs: https://xgboost.readthedocs.io/
- SHAP docs: https://shap.readthedocs.io/
