"""Detectors that surface performance problems from the data, each with its SQL visible."""
import numpy as np
import pandas as pd

from core import config
from core.metrics import filter_clause


def _acfg():
    return config.assumptions()["analytics"]


# ---------- incidents (technical decline spikes) ----------

def hourly_technical_sql(filters=None):
    return f"""SELECT date_trunc('hour', created_at) AS hour,
       count(*) AS initiated,
       count(*) FILTER (WHERE is_declined) AS declines,
       count(*) FILTER (WHERE is_declined AND reason_group = 'technical') AS technical_declines
FROM v_payments
WHERE NOT is_retry AND {filter_clause(filters)}
GROUP BY 1
ORDER BY 1"""


def detect_incidents(con, filters=None):
    """Flag hours whose technical-decline count is implausible under the period's rate (binomial z-score)."""
    c = _acfg()
    sql = hourly_technical_sql(filters)
    h = con.execute(sql).df()
    if h.empty:
        return sql, h, pd.DataFrame()
    p0 = h["technical_declines"].sum() / h["initiated"].sum()
    sd = np.sqrt(h["initiated"] * p0 * (1 - p0)).replace(0, np.nan)
    h["technical_rate"] = h["technical_declines"] / h["initiated"]
    h["z"] = (h["technical_declines"] - h["initiated"] * p0) / sd
    h["incident"] = (h["z"] > c["incident_z"]) & (h["technical_declines"] >= c["incident_min_events"])
    flagged = h[h["incident"]].copy()
    incidents = []
    if not flagged.empty:
        flagged["grp"] = (flagged["hour"].diff() != pd.Timedelta(hours=1)).cumsum()
        for _, g in flagged.groupby("grp"):
            incidents.append({
                "start": g["hour"].min(), "end": g["hour"].max() + pd.Timedelta(hours=1),
                "hours": len(g), "initiated": int(g["initiated"].sum()),
                "technical_declines": int(g["technical_declines"].sum()),
                "technical_rate": g["technical_declines"].sum() / g["initiated"].sum(),
                "baseline_rate": p0, "max_z": float(g["z"].max()),
            })
    return sql, h, pd.DataFrame(incidents)


def daily_trend(hourly):
    """Daily decline rate with and without incident hours: incidents are excluded from the trend baseline."""
    h = hourly.copy()
    h["day"] = h["hour"].dt.floor("D")
    allh = h.groupby("day")[["declines", "initiated"]].sum()
    clean = h[~h["incident"]].groupby("day")[["declines", "initiated"]].sum()
    out = pd.DataFrame({"with_incidents": allh["declines"] / allh["initiated"],
                        "baseline_excl_incidents": clean["declines"] / clean["initiated"]}).reset_index()
    return out


def decline_heatmap_sql(filters=None):
    return f"""SELECT hour(created_at) AS hour_of_day, coalesce(reason_group, 'unmapped') AS reason_group,
       count(*) FILTER (WHERE is_declined) / count(*) AS decline_rate
FROM v_payments
WHERE NOT is_retry AND {filter_clause(filters)}
GROUP BY ALL"""


def decline_by_hour_group_sql(filters=None):
    return f"""WITH base AS (
    SELECT hour(created_at) AS hour_of_day, count(*) AS initiated
    FROM v_payments WHERE NOT is_retry AND {filter_clause(filters)} GROUP BY 1
)
SELECT d.hour_of_day, d.reason_group, d.declines / b.initiated AS decline_rate
FROM (
    SELECT hour(created_at) AS hour_of_day, coalesce(reason_group, 'unmapped') AS reason_group, count(*) AS declines
    FROM v_payments WHERE NOT is_retry AND is_declined AND {filter_clause(filters)} GROUP BY ALL
) d JOIN base b USING (hour_of_day)
ORDER BY 1, 2"""


# ---------- false-decline hotspots by category ----------

def category_sql(filters=None, rail="card"):
    f = dict(filters or {})
    f.pop("rails", None)
    return f"""WITH c AS (
    SELECT category, count(*) AS initiated, sum(is_declined::INT) AS declines,
           sum((decline_reason = 'D57')::INT) AS category_rule_declines
    FROM v_payments
    WHERE NOT is_retry AND rail = '{rail}' AND {filter_clause(f)}
    GROUP BY 1
)
SELECT category, initiated, declines,
       declines / initiated AS decline_rate,
       (sum(declines) OVER () - declines) / (sum(initiated) OVER () - initiated) AS rest_rate,
       (declines / initiated) / ((sum(declines) OVER () - declines) / (sum(initiated) OVER () - initiated)) AS ratio,
       category_rule_declines
FROM c
ORDER BY ratio DESC"""


def detect_hotspots(con, filters=None, rail="card"):
    c = _acfg()
    sql = category_sql(filters, rail)
    df = con.execute(sql).df()
    df["hotspot"] = (df["ratio"] >= c["hotspot_ratio"]) & (df["initiated"] >= c["hotspot_min_volume"])
    return sql, df


# ---------- settlement time clusters ----------

def settle_sql(filters=None):
    return f"""SELECT rail, settle_hours, hour(created_at) AS created_hour
FROM v_payments
WHERE reached_settled AND {filter_clause(filters)}"""


def two_means(x, iters=50):
    """1-D k-means with k=2. Returns (centers, labels)."""
    x = np.asarray(x, float)
    c = np.percentile(x, [25, 75]).astype(float)
    lab = np.zeros(len(x), int)
    for _ in range(iters):
        lab = (np.abs(x - c[1]) < np.abs(x - c[0])).astype(int)
        new = np.array([x[lab == k].mean() if (lab == k).any() else c[k] for k in (0, 1)])
        if np.allclose(new, c):
            break
        c = new
    return c, lab


def detect_settle_clusters(con, filters=None):
    sep_thr = _acfg()["bimodal_separation"]
    sql = settle_sql(filters)
    df = con.execute(sql).df()
    rows = []
    for rail, g in df.groupby("rail"):
        x = g["settle_hours"].to_numpy()
        if len(x) < 50:
            continue
        c, lab = two_means(x)
        sd = np.sqrt(np.mean([x[lab == k].var() for k in (0, 1) if (lab == k).sum() > 1]))
        sep = abs(c[1] - c[0]) / sd if sd > 0 else 0.0
        share_hi = lab.mean()
        rows.append({"rail": rail, "n": len(x), "median_hours": float(np.median(x)),
                     "p95_hours": float(np.percentile(x, 95)),
                     "cluster_low_hours": float(min(c)), "cluster_high_hours": float(max(c)),
                     "share_in_high_cluster": float(share_hi if c[1] > c[0] else 1 - share_hi),
                     "separation_sd": float(sep),
                     "bimodal": bool(sep >= sep_thr and 0.1 <= share_hi <= 0.9),
                     "_labels": lab if c[1] > c[0] else 1 - lab})
    return sql, df, pd.DataFrame(rows)


def cutoff_evidence(df, rail, cutoff_hour):
    g = df[df["rail"] == rail]
    late = g["created_hour"] >= cutoff_hour
    return {"after_cutoff_median": float(g.loc[late, "settle_hours"].median()),
            "before_cutoff_median": float(g.loc[~late, "settle_hours"].median()),
            "share_after_cutoff": float(late.mean())}


# ---------- cost drift ----------

def cost_monthly_sql(filters=None):
    return f"""SELECT month, segment, rail,
       sum(cost_gbp) AS cost_gbp,
       sum(reached_settled::INT) AS settled,
       sum(cost_gbp) / nullif(sum(reached_settled::INT), 0) AS cost_per_settled
FROM v_payments
WHERE {filter_clause(filters)}
GROUP BY ALL
ORDER BY month, segment, rail"""


def detect_cost_drift(con, filters=None):
    c = _acfg()
    sql = cost_monthly_sql(filters)
    df = con.execute(sql).df()
    if df.empty:
        return sql, df, pd.DataFrame(), pd.DataFrame()
    df["m"] = df["month"].dt.month
    base_m = set(c["cost_baseline_months"])
    blended = df.groupby("month")[["cost_gbp", "settled"]].sum()
    blended["cost_per_settled"] = blended["cost_gbp"] / blended["settled"]
    blended = blended.reset_index()
    blended["m"] = blended["month"].dt.month

    def change(g):
        b = g[g["m"].isin(base_m)]
        r = g[~g["m"].isin(base_m)]
        if b["settled"].sum() == 0 or r["settled"].sum() == 0:
            return np.nan, np.nan, np.nan
        bv = b["cost_gbp"].sum() / b["settled"].sum()
        rv = r["cost_gbp"].sum() / r["settled"].sum()
        return bv, rv, rv / bv - 1

    blended_base, blended_recent, blended_change = change(blended)
    rows = []
    for (seg, rail), g in df.groupby(["segment", "rail"]):
        bv, rv, ch = change(g)
        rows.append({"segment": seg, "rail": rail, "baseline_cps": bv, "recent_cps": rv, "change": ch,
                     "blended_change": blended_change, "settled": int(g["settled"].sum())})
    drift = pd.DataFrame(rows)
    drift["flagged"] = drift["change"] > c["cost_drift_threshold"]
    drift = drift.sort_values("change", ascending=False).reset_index(drop=True)
    return sql, df, blended, drift
