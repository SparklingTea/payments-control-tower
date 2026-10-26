"""Reconciliation engine: ledger settlements against the external scheme file.

Passes run in order and a payment leaves as soon as it matches:
  1 exact (reference + amount, same date)   -> matched
  2 timing (reference + amount, within tol) -> matched, timing difference, NOT a break
  3 amount after fees (diff == rail fee)    -> break: amount_mismatch_fee
  4 anything else on amount                 -> break: amount_mismatch_unexplained
Then: missing records, orphans, duplicates, and a separate ledger balance check.
"""
import pandas as pd

from core import config

BREAK_CODE = {
    "missing_from_scheme": "R1",
    "amount_mismatch_fee": "R2",
    "amount_mismatch_unexplained": "R2",
    "duplicate_settlement": "R3",
    "orphan_scheme_record": "R4",
    "unbalanced_ledger": "R5",
}
OWNER = {
    "missing_from_scheme": "Scheme liaison",
    "amount_mismatch_fee": "Finance",
    "amount_mismatch_unexplained": "Operations",
    "duplicate_settlement": "Scheme liaison",
    "orphan_scheme_record": "Operations",
    "unbalanced_ledger": "Finance",
    "late_settlement": "Operations",
    "unexpected_scheme_record": "Operations",
}


def default_as_of():
    g = config.assumptions()["generator"]
    return (pd.Timestamp(g["end_date"]) + pd.Timedelta(days=g["recon_as_of_days_after_end"] + 1)
            - pd.Timedelta(seconds=1))


def _rail_case(col):
    rails = config.assumptions()["rails"]
    whens = " ".join(f"WHEN '{rc['scheme_code']}' THEN '{r}'" for r, rc in rails.items())
    return f"CASE {col} {whens} ELSE lower({col}) END"


def _fee_case(col):
    rails = config.assumptions()["rails"]
    whens = " ".join(f"WHEN '{r}' THEN {rc['cost_gbp']}" for r, rc in rails.items())
    return f"CASE {col} {whens} ELSE 0 END"


def reconcile_sql(as_of=None, tolerance_days=None, eps=None):
    rc = config.assumptions()["reconciliation"]
    tol = rc["tolerance_days"] if tolerance_days is None else tolerance_days
    eps = rc["amount_epsilon_gbp"] if eps is None else eps
    as_of = pd.Timestamp(as_of) if as_of is not None else default_as_of()
    return f"""
WITH ledger_settle AS (
    SELECT l.payment_id, sum(l.credit_gbp) AS ledger_amount, min(l.posted_at) AS ledger_settled_at
    FROM ledger_entries l
    WHERE l.event = 'settlement' AND l.account = 'settlement'
    GROUP BY l.payment_id
),
scheme AS (
    SELECT s.*, {_rail_case('s.rail')} AS rail_internal,
           count(*) OVER (PARTITION BY s.payment_id) AS n_records,
           row_number() OVER (PARTITION BY s.payment_id ORDER BY s.settled_at, s.scheme_ref) AS rn,
           p.payment_id IS NOT NULL AS known_payment
    FROM scheme_file s
    LEFT JOIN payments p USING (payment_id)
),
matched AS (
    SELECT l.payment_id, s.scheme_ref, p.rail,
           l.ledger_amount, s.amount_gbp AS scheme_amount,
           l.ledger_settled_at, s.settled_at AS scheme_settled_at,
           {_fee_case('p.rail')} AS rail_fee,
           abs(date_diff('day', CAST(l.ledger_settled_at AS DATE), CAST(s.settled_at AS DATE))) AS day_gap,
           CASE
             WHEN s.scheme_ref IS NULL AND date_diff('day', l.ledger_settled_at, TIMESTAMP '{as_of}') > {tol}
                  THEN 'missing_from_scheme'
             WHEN s.scheme_ref IS NULL THEN 'pending'
             WHEN abs(l.ledger_amount - s.amount_gbp) <= {eps} AND
                  CAST(l.ledger_settled_at AS DATE) = CAST(s.settled_at AS DATE) THEN 'matched_exact'
             WHEN abs(l.ledger_amount - s.amount_gbp) <= {eps} AND
                  abs(date_diff('day', CAST(l.ledger_settled_at AS DATE), CAST(s.settled_at AS DATE))) <= {tol}
                  THEN 'matched_timing'
             WHEN abs(l.ledger_amount - s.amount_gbp) <= {eps} THEN 'late_settlement'
             WHEN abs((l.ledger_amount - s.amount_gbp) - {_fee_case('p.rail')}) <= {eps} THEN 'amount_mismatch_fee'
             ELSE 'amount_mismatch_unexplained'
           END AS status,
           CASE
             WHEN s.scheme_ref IS NULL THEN 'Missing-record check'
             WHEN abs(l.ledger_amount - s.amount_gbp) <= {eps} AND
                  CAST(l.ledger_settled_at AS DATE) = CAST(s.settled_at AS DATE) THEN 'Pass 1 exact'
             WHEN abs(l.ledger_amount - s.amount_gbp) <= {eps} THEN 'Pass 2 timing'
             WHEN abs((l.ledger_amount - s.amount_gbp) - {_fee_case('p.rail')}) <= {eps} THEN 'Pass 3 amount after fees'
             ELSE 'Pass 4 unexplained amount'
           END AS pass
    FROM ledger_settle l
    JOIN payments p USING (payment_id)
    LEFT JOIN scheme s ON s.payment_id = l.payment_id AND s.rn = 1
),
unexpected AS (
    SELECT s.payment_id, s.scheme_ref, s.rail_internal AS rail, NULL::DOUBLE AS ledger_amount,
           s.amount_gbp AS scheme_amount, NULL::TIMESTAMP AS ledger_settled_at, s.settled_at AS scheme_settled_at,
           0.0 AS rail_fee, NULL::BIGINT AS day_gap,
           'unexpected_scheme_record' AS status, 'Reference lookup' AS pass
    FROM scheme s
    WHERE s.known_payment AND s.rn = 1 AND s.payment_id NOT IN (SELECT payment_id FROM ledger_settle)
),
orphans AS (
    SELECT s.payment_id, s.scheme_ref, s.rail_internal AS rail, NULL::DOUBLE, s.amount_gbp, NULL::TIMESTAMP,
           s.settled_at, 0.0, NULL::BIGINT, 'orphan_scheme_record', 'Orphan check'
    FROM scheme s WHERE NOT s.known_payment
),
duplicates AS (
    SELECT s.payment_id, s.scheme_ref, s.rail_internal, l.ledger_amount, s.amount_gbp, l.ledger_settled_at,
           s.settled_at, 0.0, NULL::BIGINT, 'duplicate_settlement', 'Duplicate check'
    FROM scheme s LEFT JOIN ledger_settle l USING (payment_id)
    WHERE s.known_payment AND s.rn > 1
),
unbalanced AS (
    SELECT l.payment_id, NULL AS scheme_ref, p.rail, sum(l.debit_gbp) AS ledger_amount, sum(l.credit_gbp),
           min(l.posted_at), NULL::TIMESTAMP, 0.0, NULL::BIGINT, 'unbalanced_ledger', 'Ledger balance check'
    FROM ledger_entries l JOIN payments p USING (payment_id)
    GROUP BY l.payment_id, p.rail
    HAVING abs(sum(l.debit_gbp) - sum(l.credit_gbp)) > {eps}
)
SELECT * FROM matched
UNION ALL SELECT * FROM unexpected
UNION ALL SELECT * FROM orphans
UNION ALL SELECT * FROM duplicates
UNION ALL SELECT * FROM unbalanced
"""


def _cause(row):
    s = row["status"]
    la, sa = row["ledger_amount"], row["scheme_amount"]
    tol = config.assumptions()["reconciliation"]["tolerance_days"]
    if s == "missing_from_scheme":
        return f"Ledger settled £{la:,.2f} but no scheme record arrived within the {tol}-day window; settlement may have been rejected or the file is incomplete."
    if s == "amount_mismatch_fee":
        return f"Scheme paid £{sa:,.2f} against £{la:,.2f} in the ledger; the gap equals the £{row['rail_fee']:.2f} {row['rail']} fee, so the scheme netted its fee and a fee posting is missing."
    if s == "amount_mismatch_unexplained":
        return f"Scheme paid £{sa:,.2f} against £{la:,.2f} in the ledger; the £{abs(la - sa):,.2f} gap is not a known fee, so check the source instruction for a keying error."
    if s == "duplicate_settlement":
        return f"A second scheme record of £{sa:,.2f} exists for a payment already settled; raise a recall for the duplicate."
    if s == "orphan_scheme_record":
        return f"Scheme record of £{sa:,.2f} references a payment we do not hold; likely misrouted, investigate before accepting funds."
    if s == "unbalanced_ledger":
        return f"Ledger debits £{la:,.2f} and credits £{sa:,.2f} differ by £{abs(la - sa):,.2f}; a posting is missing or mis-keyed."
    if s == "late_settlement":
        return f"Amounts agree but settlement dates are {row['day_gap']} days apart, beyond the {tol}-day tolerance."
    if s == "unexpected_scheme_record":
        return "Scheme settled a payment that never settled in our ledger; check whether a ledger posting failed."
    return ""


def _values_compared(row):
    def f(x):
        return "-" if pd.isna(x) else f"£{x:,.2f}"

    def d(x):
        return "-" if pd.isna(x) else pd.Timestamp(x).strftime("%Y-%m-%d %H:%M")

    if row["status"] == "unbalanced_ledger":
        return f"debits {f(row['ledger_amount'])} vs credits {f(row['scheme_amount'])}"
    return (f"ledger {f(row['ledger_amount'])} @ {d(row['ledger_settled_at'])} | "
            f"scheme {f(row['scheme_amount'])} @ {d(row['scheme_settled_at'])}")


def run(con, as_of=None, tolerance_days=None):
    """Return (results, exceptions). results has one row per compared item; exceptions are the breaks."""
    as_of = pd.Timestamp(as_of) if as_of is not None else default_as_of()
    res = con.execute(reconcile_sql(as_of, tolerance_days)).df()
    brk = res[~res["status"].isin(["matched_exact", "matched_timing", "pending"])].copy()
    brk["break_code"] = brk["status"].map(BREAK_CODE)
    la, sa = brk["ledger_amount"], brk["scheme_amount"]
    brk["amount_at_risk"] = (la - sa).abs()
    sel = brk["status"].isin(["missing_from_scheme"])
    brk.loc[sel, "amount_at_risk"] = la[sel]
    sel = brk["status"].isin(["duplicate_settlement", "orphan_scheme_record", "unexpected_scheme_record", "late_settlement"])
    brk.loc[sel, "amount_at_risk"] = sa[sel]
    ref_date = brk["ledger_settled_at"].fillna(brk["scheme_settled_at"])
    brk["age_days"] = (as_of.normalize() - pd.to_datetime(ref_date).dt.normalize()).dt.days.clip(lower=0)
    buckets = config.assumptions()["reconciliation"]["age_buckets"]
    brk["age_bucket"] = brk["age_days"].map(lambda a: next(lbl for lo, hi, lbl in buckets if lo <= a <= hi))
    brk["suggested_owner"] = brk["status"].map(OWNER)
    brk["suggested_cause"] = brk.apply(_cause, axis=1)
    brk["values_compared"] = brk.apply(_values_compared, axis=1)
    brk = brk.rename(columns={"status": "break_type", "pass": "pass_failed"})
    brk = brk.sort_values(["amount_at_risk", "age_days"], ascending=False).reset_index(drop=True)
    brk.insert(0, "break_id", [f"B{i + 1:05d}" for i in range(len(brk))])
    brk["amount_at_risk"] = brk["amount_at_risk"].round(2)
    return res, brk


def materialise(con, as_of=None, tolerance_days=None):
    res, brk = run(con, as_of, tolerance_days)
    for name, df in (("recon_results", res), ("recon_exceptions", brk)):
        con.register("_df", df)
        con.execute(f"CREATE OR REPLACE TABLE {name} AS SELECT * FROM _df")
        con.unregister("_df")
    con.execute("""
        CREATE OR REPLACE VIEW v_open_breaks AS
        SELECT e.*, c.segment, coalesce(p.created_at, e.scheme_settled_at) AS created_at
        FROM recon_exceptions e
        LEFT JOIN payments p USING (payment_id)
        LEFT JOIN customers c USING (customer_id)
    """)
    return res, brk


def summary(res, brk):
    """Match rate, break rate by type, value at risk and age buckets."""
    compared = res[res["pass"].str.startswith("Pass") | (res["status"] == "missing_from_scheme")]
    matched = compared["status"].isin(["matched_exact", "matched_timing"]).sum()
    n = len(compared)
    by_type = brk.groupby("break_type").agg(breaks=("break_id", "count"), value_at_risk=("amount_at_risk", "sum"))
    by_type["break_rate"] = by_type["breaks"] / max(n, 1)
    by_age = brk.groupby("age_bucket").agg(breaks=("break_id", "count"), value_at_risk=("amount_at_risk", "sum"))
    return {
        "compared": int(n),
        "matched": int(matched),
        "match_rate": matched / max(n, 1),
        "timing_differences": int((res["status"] == "matched_timing").sum()),
        "pending": int((res["status"] == "pending").sum()),
        "breaks": int(len(brk)),
        "value_at_risk": float(brk["amount_at_risk"].sum()),
        "by_type": by_type.reset_index(),
        "by_age": by_age.reset_index(),
    }


def score_against_truth(con, brk, truth):
    """Recall, classification accuracy and false-positive rate vs planted breaks (tests only)."""
    planted = pd.DataFrame(truth["breaks"])
    planted["key"] = planted["payment_id"].where(planted["type"] != "R4", planted.get("scheme_ref"))
    real = planted[planted["type"] != "R6"]
    brk = brk.copy()
    brk["key"] = brk["payment_id"].where(brk["break_type"] != "orphan_scheme_record", brk["scheme_ref"])
    flagged = brk.drop_duplicates("key").set_index("key")
    found = real[real["key"].isin(flagged.index)]
    correct = (flagged.loc[found["key"], "break_code"].to_numpy() == found["type"].to_numpy()).sum()
    settled = con.execute("SELECT count(DISTINCT payment_id) FROM ledger_entries WHERE event='settlement'").fetchone()[0]
    planted_keys = set(planted["key"])
    clean_flagged = flagged.index.difference(list(planted_keys))
    r6_keys = set(planted.loc[planted["type"] == "R6", "key"])
    return {
        "planted": int(len(real)),
        "found": int(len(found)),
        "recall": len(found) / max(len(real), 1),
        "classification_accuracy": correct / max(len(found), 1),
        "false_positives": int(len(clean_flagged)),
        "false_positive_rate": len(clean_flagged) / max(settled - len(planted_keys), 1),
        "r6_flagged": int(len(r6_keys & set(flagged.index))),
    }
