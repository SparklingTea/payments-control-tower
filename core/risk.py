"""Fraud threshold economics.

C(t) = sum_{s_i < t, y_i = 1} a_i  +  c_d * #{s_i >= t, y_i = 0}  +  c_r * #{s_i >= t}
a_i = amount + chargeback fee, c_d = false-decline cost, c_r = review cost (0 in decline-only mode).
Scores are bucketed on the threshold grid in SQL; the formula is applied to the bucket totals.
"""
import pandas as pd

from core import config


def risk_cfg():
    return config.assumptions()["risk"]


def _step_bp():
    return int(round(risk_cfg()["grid_step"] * 10000))


def buckets_sql(where="TRUE"):
    step = _step_bp()
    return f"""SELECT CAST(round(p.risk_score * 10000) AS INTEGER) // {step} AS bucket,
       count(*) AS payments,
       sum(f.is_fraud::INT) AS fraud,
       coalesce(sum(p.amount_gbp) FILTER (WHERE f.is_fraud), 0) AS fraud_amount
FROM payments p
JOIN fraud_truth f USING (payment_id)
JOIN customers c USING (customer_id)
WHERE {where}
GROUP BY 1
ORDER BY 1"""


def costs(mode=None, false_decline_cost=None, loss_extra=None, loss_multiplier=1.0, review_cost=None):
    r = risk_cfg()
    mode = mode or r["default_mode"]
    return {
        "mode": mode,
        "c_d": r["false_decline_cost_gbp"] if false_decline_cost is None else false_decline_cost,
        "loss_extra": r["fraud_loss_extra_gbp"] if loss_extra is None else loss_extra,
        "loss_multiplier": loss_multiplier,
        "c_r": (r["review_cost_gbp"] if review_cost is None else review_cost) if mode == "review" else 0.0,
    }


def cost_curve(buckets: pd.DataFrame, c: dict) -> pd.DataFrame:
    """Evaluate C(t) at every grid threshold from bucket totals."""
    step = _step_bp()
    n_k = 10000 // step + 1
    b = buckets.set_index("bucket").reindex(range(n_k), fill_value=0)
    loss = (b["fraud_amount"] + c["loss_extra"] * b["fraud"]) * c["loss_multiplier"]
    genuine = b["payments"] - b["fraud"]
    # flagged at threshold k: buckets >= k (reverse cumulative); missed: buckets < k
    flagged = b["payments"][::-1].cumsum()[::-1]
    gen_blocked = genuine[::-1].cumsum()[::-1]
    fraud_caught = b["fraud"][::-1].cumsum()[::-1]
    missed_loss = loss.cumsum().shift(1, fill_value=0)
    tot_fraud, tot_gen = b["fraud"].sum(), genuine.sum()
    out = pd.DataFrame({
        "threshold": [k * step / 10000 for k in range(n_k)],
        "flagged": flagged.to_numpy(),
        "fraud_caught": fraud_caught.to_numpy(),
        "fraud_missed": (tot_fraud - fraud_caught).to_numpy(),
        "genuine_blocked": gen_blocked.to_numpy(),
        "genuine_passed": (tot_gen - gen_blocked).to_numpy(),
        "fraud_loss": missed_loss.to_numpy(),
    })
    out["false_decline_cost"] = c["c_d"] * out["genuine_blocked"]
    out["review_cost"] = c["c_r"] * out["flagged"]
    out["total_cost"] = out["fraud_loss"] + out["false_decline_cost"] + out["review_cost"]
    out["precision"] = out["fraud_caught"] / out["flagged"].where(out["flagged"] > 0)
    out["recall"] = out["fraud_caught"] / max(tot_fraud, 1)
    out["friction"] = out["genuine_blocked"] / max(tot_gen, 1)
    return out


def optimum(curve):
    i = int(curve["total_cost"].to_numpy().argmin())
    return curve.iloc[i]


def at_threshold(curve, t):
    k = int(round(t / risk_cfg()["grid_step"]))
    return curve.iloc[k]


def analyse(con, c=None, where="TRUE"):
    sql = buckets_sql(where)
    b = con.execute(sql).df()
    c = c or costs()
    curve = cost_curve(b, c)
    return sql, b, curve, optimum(curve)


def sensitivity(buckets, base=None):
    """Optimal threshold under six cost scenarios (false-decline cost and fraud loss each moved +/- 50%)."""
    r = risk_cfg()
    ch = r["sensitivity_change"]
    base = base or costs()
    t_base = optimum(cost_curve(buckets, base))["threshold"]
    lo, hi = base["c_d"] * (1 - ch), base["c_d"] * (1 + ch)
    lm = base["loss_multiplier"]
    scen = [
        ("False-decline cost -50%", {"c_d": lo}),
        ("False-decline cost +50%", {"c_d": hi}),
        ("Fraud loss -50%", {"loss_multiplier": lm * (1 - ch)}),
        ("Fraud loss +50%", {"loss_multiplier": lm * (1 + ch)}),
        ("Decline cost -50%, fraud loss +50%", {"c_d": lo, "loss_multiplier": lm * (1 + ch)}),
        ("Decline cost +50%, fraud loss -50%", {"c_d": hi, "loss_multiplier": lm * (1 - ch)}),
    ]
    rows = []
    for name, kw in scen:
        o = optimum(cost_curve(buckets, {**base, **kw}))
        rows.append({"scenario": name, "optimal_threshold": o["threshold"], "expected_cost": o["total_cost"],
                     "shift": o["threshold"] - t_base,
                     "beyond_margin": abs(o["threshold"] - t_base) > r["sensitivity_margin"] + 1e-9})
    return t_base, pd.DataFrame(rows)


def segment_optimum(con, c=None):
    days = risk_cfg()["new_account_days"]
    out = []
    for label, where in ((f"New accounts (< {days} days)", f"date_diff('day', c.opened_at, p.created_at) < {days}"),
                         (f"Established (>= {days} days)", f"date_diff('day', c.opened_at, p.created_at) >= {days}")):
        sql, b, curve, o = analyse(con, c, where)
        out.append({"segment": label, "payments": int(b["payments"].sum()), "fraud": int(b["fraud"].sum()),
                    "fraud_rate": b["fraud"].sum() / max(b["payments"].sum(), 1),
                    "optimal_threshold": o["threshold"], "expected_cost": o["total_cost"], "sql": sql})
    return pd.DataFrame(out)


def rules_sql():
    r = risk_cfg()["rules"]
    return f"""WITH s AS (
    SELECT p.payment_id, p.amount_gbp, p.risk_score, f.is_fraud,
           (p.payee_type = 'new_payee' AND p.amount_gbp >= {r['new_payee_high_amount_gbp']}) AS rule_new_payee_high_amount,
           count(*) OVER (PARTITION BY p.customer_id ORDER BY p.created_at
                          RANGE BETWEEN INTERVAL {r['burst_window_minutes']} MINUTE PRECEDING AND CURRENT ROW)
               >= {r['burst_count']} AS rule_burst,
           date_diff('day', c.opened_at, p.created_at) < {r['new_account_days']} AS rule_new_account
    FROM payments p JOIN fraud_truth f USING (payment_id) JOIN customers c USING (customer_id)
)
SELECT * FROM s"""


RULE_LABELS = {
    "rule_new_payee_high_amount": "High amount to a new payee",
    "rule_burst": "Burst of payments in a short window",
    "rule_new_account": "Account younger than 30 days",
}


def rules_comparison(con, threshold, c=None):
    """Each rule alone, and what it adds on top of the score threshold."""
    c = c or costs()
    sql = rules_sql()
    df = con.execute(sql).df()
    thr_bp = int(round(threshold * 10000))
    by_score = (df["risk_score"] * 10000).round().astype(int) >= thr_bp
    fraud, gen = df["is_fraud"], ~df["is_fraud"]
    loss = (df["amount_gbp"] + c["loss_extra"]) * c["loss_multiplier"]
    rows = []
    for col, label in RULE_LABELS.items():
        f = df[col]
        extra = f & ~by_score
        saved = loss[extra & fraud].sum()
        added = c["c_d"] * (extra & gen).sum() + c["c_r"] * extra.sum()
        rows.append({
            "rule": label, "flagged": int(f.sum()),
            "catch_rate": (f & fraud).sum() / max(fraud.sum(), 1),
            "false_decline_rate": (f & gen).sum() / max(gen.sum(), 1),
            "precision": (f & fraud).sum() / max(f.sum(), 1),
            "extra_fraud_caught_vs_score": int((extra & fraud).sum()),
            "extra_genuine_blocked_vs_score": int((extra & gen).sum()),
            "net_saving_on_top_of_score": saved - added,
        })
    return sql, pd.DataFrame(rows)


def recommendation(opt, current, c, t_base, sens):
    saving = current["total_cost"] - opt["total_cost"]
    moved = sens[sens["beyond_margin"]]
    robust = ("holds within the margin in all six cost scenarios" if moved.empty
              else f"moves beyond the margin in {len(moved)} of six cost scenarios, so treat it as a range")
    text = (
        f"Set the fraud score threshold at {opt['threshold']:.3f} instead of {current['threshold']:.2f}. "
        f"Expected cost falls from £{current['total_cost']:,.0f} to £{opt['total_cost']:,.0f}, "
        f"a saving of £{saving:,.0f} over six months, while {opt['friction'] * 100:.2f}% of genuine payments are blocked. "
        f"The choice {robust}. "
        f"Key assumptions: £{c['c_d']:.0f} lost per false decline and the payment amount plus £{c['loss_extra']:.0f} per missed fraud. "
        f"Before changing anything, ask the fraud and finance owners for confirmed fraud labels with their delay, "
        f"real margin per payment, and chargeback recovery rates."
    )
    return text, saving
