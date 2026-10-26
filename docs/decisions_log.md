# Decisions log

What was chosen, what the alternative was, and why. The first build of this repo was generated with an AI
coding assistant (Claude Code) from the PRD. **Review each row and record in the last column where you agreed,
overrode or changed the AI's choice** — that column is yours, and is the honest answer to "how did you use AI?".

| # | Area | Decision | Alternative considered | Why | Your call |
|---|------|----------|------------------------|-----|-----------|
| 1 | Generator | Planted effects (category rule, timeout incident, score-based declines) are **calibrated** so each rail still lands on its configured auth rate | Add planted declines on top of the base rate | The PRD asks for configured rates within 1pp *and* visible planted problems; calibration gets both | |
| 2 | Generator | Fraud has its own logistic model (amount, new account, new payee, risk band); the score is `sigmoid(signal·is_fraud + features + noise)` | Score drawn independently of features | Makes rules partly redundant with the score, which is realistic and gives the rules comparison something to say | |
| 3 | Generator | 30% of card/FPS fraud comes as a burst of three payments within minutes | No bursts | Without it the burst rule could never earn its keep, so the comparison would be one-sided | |
| 4 | Generator | Scheme file uses scheme codes (`CARD`, `FPS`, `BACS`, `DD`) while payments use internal rail names | Same names everywhere | The PRD's "one deliberately inconsistent field"; recon has to map it. Unmapped reason codes `D99`/`F77` are the second inconsistency | |
| 5 | Lifecycle | Retries are new payments with `retry_of`; hard declines (risk rule) are never retried by default | Retry any decline | PRD rule; hard declines are configurable in `retries.retry_hard_declines` | |
| 6 | Metrics | Success rate counts a payment as settled if it reached `settled`, even if later returned or disputed | Exclude returned/disputed | Keeps success a lifecycle measure; returns and disputes have their own rates. Stated as a caveat in the registry | |
| 7 | Metrics | Decline-by-group denominators are **all** initiated originals, so the groups sum to the decline rate | Rate within each group | Matches the PRD definition and avoids groups that look comparable but aren't | |
| 8 | Metrics | Selecting every rail/segment means "no filter" | Always add `IN (...)` | Otherwise orphan scheme breaks (no segment) silently vanish from value at risk | |
| 9 | Reconciliation | Exact vs timing match compares settlement **calendar dates**; tolerance 1 day | Compare hour gaps | Matches how ops teams talk about value dates; a few seconds of jitter across midnight becomes a timing difference, never a break | |
| 10 | Reconciliation | The fee for pass 3 is the rail's configured `cost_gbp` | A separate scheme-fee config | One number per rail is simpler; split it if real scheme fees differ from processing cost | |
| 11 | Reconciliation | R5 is planted on the customer-side debit so the settlement credit still matches the scheme | Alter the settlement credit | Keeps each planted break to exactly one detected type, so classification accuracy is measurable | |
| 12 | Reconciliation | Age is measured from the settlement date to the as-of date (7 days after period end) | Model break resolution over time | Simple and honest, but most open breaks land in "over 5 days". A daily-run simulation with resolution would make buckets more realistic | |
| 13 | Analytics | Incidents: an hour's technical declines more than 8 binomial SDs above the period rate, with at least 10 declines | Robust z on hourly rates (median/MAD) | Robust z on small hourly counts flagged quiet night hours; the binomial test accounts for volume | |
| 14 | Analytics | Bimodal settlement: 1-D two-means, flagged when centres are ≥3 pooled SDs apart and both clusters hold ≥10% | Histogram peak finding | Few lines, explainable, and gives cluster sizes for the evidence | |
| 15 | Analytics | Cost drift compares months 4–6 with a months 1–3 baseline per segment × rail, flag at +10% | Changepoint detection | The baseline is explicit in config, and the check works without knowing when the drift started | |
| 16 | Risk | Population is every scored payment, retries included; labels come from `fraud_truth` | Originals only | Every payment is scored at initiation, so every payment is a decision point | |
| 17 | Risk | Scores bucketed on a 0.005 grid in integer basis points (`round(score·10000) // 50`) | Float comparisons | Exact, so the SQL curve and the generator's independent numpy curve agree to the penny | |
| 18 | Risk | "Current" threshold set to 0.60 | 0.90 (first draft) | At 0.90 the saving looked implausibly large (5×); 0.60 is a more believable status quo. Either way it is an assumption | |
| 19 | Risk | Sensitivity margin 0.05 absolute; 3 of 6 scenarios exceed it, so the recommendation is presented as a range | Hide the scenarios that move | The PRD wants robustness shown honestly | |
| 20 | Notes | Notes are templates filled from query results; Claude may reword them only if every number in the rewrite appears in the evidence, otherwise the template is kept | Let the LLM write notes from raw data | PRD principle: AI drafts text, DuckDB produces every figure. Tested with a fake client that invents a number | |
| 21 | Tests | Dashboard-vs-registry test compares at 1e-12 relative tolerance | Exact equality | Same SQL, but DuckDB's parallel float sums can differ in the last bit between runs | |
| 22 | Scope | No weekends/bank holidays in Bacs or DD timing | Business-day calendar | Out of scope for four days; noted as a limitation | |
