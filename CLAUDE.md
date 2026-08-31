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

| Group | Signals | Group cap |
|---|---|---|
| **Payment exposure** | COD; high-value COD (>₹2,000) | 45 |
| **Customer history** | Repeat RTO/refusal (2 of last 3); overall return rate >30%; new customer (+small); trusted customer (capped discount, not flat −30) | 45 |
| **Order context** | Size-dependent category; multiple size/variant bracketing (kept small, capped 10–15) | 25 |
| **Deliverability** | Incomplete address (call it "needs verification," not "risky"); pincode delivery risk (smoothed, minimum-sample gated) | 30 |

**Formula:**
```
risk_score = min(payment_group, 45) + min(history_group, 45) + min(context_group, 25) + min(deliverability_group, 30)
```

### Explicitly dropped rules
- ❌ "Late-night order" — too weak and unjustifiable, drop entirely

### Hard safeguards (must be enforced in code, not just described)
- Weak contextual signals ALONE can never trigger High or Very High tier
- Trusted-customer discount can never cancel out a hard address/deliverability problem
- Pincode risk alone can never force a restrictive action
- Bracketing is a weak context signal only — it relates more to post-delivery returns than pre-shipment RTO, never treat it as near-fraud evidence

### Risk tiers and actions (only these three actions get built — nothing else)

| Score | Tier | Action | What gets built |
|---|---|---|---|
| 0–30 | Low | Ship normally | No extra step |
| 31–60 | Medium | Confirmation/OTP simulation | Customer confirms/rejects/corrects address before shipping |
| 61–85 | High | Prepaid-incentive nudge simulation | Merchant offers small discount for switching to prepaid |
| 86+ (with real evidence, not just context) | Very high | Manual review queue | Reviewer sees evidence, approves/overrides/holds, logs a reason |

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
6. **Document daily, in Obsidian, same day.** What got built, what got tested, what changed and why. This becomes the README's reasoning section later — don't rebuild it from memory afterward.
7. **Freeze scope.** If a new idea doesn't improve the end-to-end demo, the audit trail, or the cost evidence, don't build it. No more pivots, no more external re-validation — the plan is final.
8. **No claims we haven't earned.** Every "honest metrics" and "synthetic ≠ production" caveat in this doc must appear in the real README and demo video, worded the same way.

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
