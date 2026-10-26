# Payment Performance & Risk Control Tower

A Streamlit + DuckDB app on synthetic payments data that follows money through its lifecycle, finds where the ledger
and the scheme file disagree, measures where payments fail or cost too much, and ends on a fraud-threshold decision
with the economics attached. Every problem it finds was planted on purpose, and the tests check it finds them.

> Screenshot: _add `docs/screenshot.png` here_ · 3-minute demo: _add link here_

**No real data.** 20,000 fictional customers and about 300,000 payments, reproducible from one seed. Every cost, fee
and rate is an assumption in [`config/assumptions.yaml`](config/assumptions.yaml), labelled as such in the app.

## Run it (under 10 minutes)

Python 3.11 or later.

```bash
pip install -r requirements.txt
```

```bash
python -m generator.generate
```

```bash
streamlit run app/Home.py
```

Generation takes about 15 seconds and writes `data/control_tower.duckdb` plus `data/truth.json` (the answer key, read
by tests only). The app can also generate the data from its home page.

```bash
python -m pytest
```

Optional: set `ANTHROPIC_API_KEY` to let Claude reword insight and break notes. The model defaults to
`claude-opus-5-5` (override with `LLM_MODEL`). Any rewrite containing a number that is not in the query results is
thrown away and the template is shown instead. Without a key everything works and the templates are shown.

## The four modules

| Module | What it does | Ends on |
|---|---|---|
| 1 · Lifecycle | 7 states, retries as new linked payments, idempotency keys, reason codes rolled into 5 groups, an event-log validator | Valid state counts you can trust; unmapped codes listed, not hidden |
| 2 · Reconciliation | Double-entry ledger vs scheme file: exact → timing → amount-after-fees → unexplained, plus missing, orphan, duplicate and ledger-balance checks | An exceptions queue sorted by value at risk and age, each row showing the pass it failed and the values compared |
| 3 · Analytics | Metric registry in YAML; every tile shows its runnable SQL; detectors for incidents, category hotspots, settlement clusters, cost drift | Four insight notes with backlog-ready stories and Given/When/Then criteria |
| 4 · Risk | Cost of a fraud threshold, C(t) = missed fraud + c_d·false declines + c_r·reviews; precision/recall/friction; six-scenario sensitivity; new vs established accounts; three transparent rules | A recommendation under 120 words with the saving, assumptions and next data request |

## Results against the success criteria

Seed 42, measured by `python -m pytest` (49 tests).

| Criterion | Target | Result |
|---|---|---|
| Simulator reproduces configured auth and failure rates | within 1pp | Largest gap 0.27pp (direct debit success rate) |
| Reconciliation finds planted breaks | ≥ 95% | 100% (2,309 of 2,309 R1–R5) |
| Breaks classified into the right type | ≥ 90% | 100% |
| False positives on clean payments | < 2% | 0 clean payments flagged; 0 of 651 R6 timing differences flagged |
| Every metric has runnable SQL; P1–P4 surfaced | all | All 10 registry metrics show SQL; P1 incident, P2 electronics hotspot (3.2×), P3 Bacs clusters (50h/74h), P4 business-card drift (+34.6% vs +2.3% blended) all detected |
| Recommended threshold vs planted optimum | within 2% | 0.355 vs 0.355 (0%) |
| Generation time | < 90 s | ~13 s |

Figures above come from the current seed; re-run `python -m core.brief` to rebuild
[`docs/product_brief.md`](docs/product_brief.md) from the database.

## Repo layout

```
config/      assumptions.yaml (every assumption), metrics.yaml (metric registry), reason_codes.yaml
generator/   seeded generator: 7 DuckDB tables + truth.json
core/        lifecycle, ledger, reconcile, metrics, analytics, risk, notes, brief, db (views), config
app/         Home.py + one page per module
tests/       generator, lifecycle, reconciliation, analytics (incl. 20-row hand-computed fixture), risk, notes
docs/        product_brief.md (generated), decisions_log.md
```

## How it is built

- **Plant first, then detect.** The generator plants four performance problems (P1–P4) and six reconciliation
  break types (R1–R6, where R6 is a timing difference that must *not* be flagged), and stores the cost-minimising
  fraud threshold in `truth.json`. The app never reads `truth.json`; the tests do.
- **Every number traces to SQL or a formula.** Dashboard tiles are built from the registry, and the SQL shown is the
  SQL that ran. The risk curve buckets scores in SQL and applies the stated formula to bucket totals; a test checks it
  against an independent numpy evaluation in the generator.
- **AI words, DuckDB counts.** The LLM only rewrites notes, never classifies breaks or computes figures.

## Honest caveats

- Fraud labels here are complete and instant. Real labels arrive late and incomplete, and fraudsters adapt to rules.
- Cost inputs are assumptions; the result shows the method, not a real optimum. 3 of 6 sensitivity scenarios move
  the optimum by more than 0.05, so the recommendation is a range.
- Records are simplified (no ISO 8583 / ISO 20022 / Bacs Standard 18), with no business-day calendar.
- Most open breaks sit in the "over 5 days" age bucket because breaks never resolve in the simulation.

See [`docs/decisions_log.md`](docs/decisions_log.md) for the choices behind these.
