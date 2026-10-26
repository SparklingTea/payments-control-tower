"""Metric registry: builds runnable DuckDB SQL for every metric from config/metrics.yaml.

The SQL string returned is exactly the SQL executed, so 'show SQL' always matches the number shown.
"""
import datetime as dt

import pandas as pd

from core import config


def registry():
    return config.metrics_registry()["metrics"]


def _quote_list(values, allowed):
    bad = set(values) - set(allowed)
    if bad:
        raise ValueError(f"unknown filter values: {sorted(bad)}")
    return ", ".join(f"'{v}'" for v in values)


def filter_clause(filters=None, date_col="created_at"):
    """Render rail / segment / date filters as a SQL predicate with literal, whitelisted values."""
    filters = filters or {}
    a = config.assumptions()
    parts = []
    # Selecting every value means "no filter", so rows with a NULL segment (orphan breaks) are kept.
    if filters.get("rails") and set(filters["rails"]) != set(a["rails"]):
        parts.append(f"rail IN ({_quote_list(filters['rails'], a['rails'])})")
    if filters.get("segments") and set(filters["segments"]) != set(a["segments"]):
        parts.append(f"segment IN ({_quote_list(filters['segments'], a['segments'])})")
    if filters.get("start"):
        parts.append(f"{date_col} >= DATE '{dt.date.fromisoformat(str(filters['start']))}'")
    if filters.get("end"):
        parts.append(f"{date_col} < DATE '{dt.date.fromisoformat(str(filters['end']))}' + INTERVAL 1 DAY")
    return " AND ".join(parts) if parts else "TRUE"


def metric_sql(name, filters=None, by=None, period=None):
    """SQL for one metric. by: list of dimension columns. period: 'week' or 'month' for a time series."""
    m = registry()[name]
    dims = list(by or [])
    if period:
        dims = [f"date_trunc('{period}', {m['date_col']}) AS {period}"] + dims
    select = ", ".join(dims + [f"{m['expr']} AS value"])
    sql = (f"SELECT {select}\nFROM {m['from']}\nWHERE ({m['where']})\n  AND {filter_clause(filters, m['date_col'])}")
    if dims:
        n = len(dims)
        sql += f"\nGROUP BY {', '.join(str(i + 1) for i in range(n))}\nORDER BY {', '.join(str(i + 1) for i in range(n))}"
    return sql


def run(con, name, filters=None, by=None, period=None):
    sql = metric_sql(name, filters, by, period)
    return sql, con.execute(sql).df()


def value(con, name, filters=None):
    sql, df = run(con, name, filters)
    v = df["value"].iloc[0]
    return sql, (None if pd.isna(v) else float(v))


def fmt(v, kind):
    if v is None or pd.isna(v):
        return "n/a"
    if kind == "pct":
        return f"{v * 100:.2f}%"
    if kind == "hours":
        return f"{v:.1f} h"
    if kind == "gbp":
        return f"£{v:.4f}"
    if kind == "gbp0":
        return f"£{v:,.0f}"
    return f"{v:,.2f}"


def _complete_weeks(series, filters):
    """Drop a trailing partial week so week-on-week compares like with like."""
    if series.empty:
        return series
    end = pd.Timestamp(filters.get("end") or config.assumptions()["generator"]["end_date"])
    return series[series["week"] + pd.Timedelta(days=6) <= end]


def headline(con, filters=None):
    filters = filters or {}
    out = []
    for name in config.metrics_registry()["headline"]:
        m = registry()[name]
        sql, v = value(con, name, filters)
        wsql, weekly = run(con, name, filters, period="week")
        full = _complete_weeks(weekly, filters)
        wow = None
        if len(full) >= 2:
            last, prev = full["value"].iloc[-1], full["value"].iloc[-2]
            if pd.notna(last) and pd.notna(prev):
                wow = (last - prev) if m["format"] == "pct" else ((last - prev) / prev if prev else None)
        out.append({"name": name, "label": m["label"], "value": v, "display": fmt(v, m["format"]),
                    "format": m["format"], "wow": wow, "higher_is_better": m["higher_is_better"],
                    "series": weekly, "sql": sql, "weekly_sql": wsql,
                    "definition": m["definition"], "caveats": m.get("caveats", "")})
    return out


def decline_by_group_sql(filters=None):
    """Declines in each reason group divided by ALL initiated (non-retry) payments, so groups sum to the decline rate."""
    return f"""SELECT coalesce(reason_group, 'unmapped') AS reason_group,
       count(*) AS declines,
       count(*) / (SELECT count(*) FROM v_payments WHERE NOT is_retry AND {filter_clause(filters)}) AS decline_rate
FROM v_payments
WHERE NOT is_retry AND is_declined AND {filter_clause(filters)}
GROUP BY 1
ORDER BY declines DESC"""
