"""DuckDB connection, shared views and derived tables used by every module."""
import duckdb
import pandas as pd

from core import config

VIEWS = """
CREATE OR REPLACE VIEW v_payment_outcomes AS
SELECT payment_id,
       bool_or(state = 'authorised') AS reached_auth,
       bool_or(state = 'settled')    AS reached_settled,
       min(ts) FILTER (WHERE state = 'settled') AS settled_at
FROM payment_events
GROUP BY payment_id;

CREATE OR REPLACE VIEW v_payments AS
SELECT p.*,
       c.segment, c.region, c.risk_band, c.opened_at,
       p.retry_of IS NOT NULL                      AS is_retry,
       o.reached_auth, o.reached_settled, o.settled_at,
       date_diff('second', p.created_at, o.settled_at) / 3600.0 AS settle_hours,
       p.final_status = 'declined'                 AS is_declined,
       p.final_status = 'failed'                   AS is_failed,
       p.final_status = 'returned'                 AS is_returned,
       p.final_status = 'disputed'                 AS is_disputed,
       coalesce(m.reason_group, CASE WHEN p.decline_reason IS NOT NULL THEN 'unmapped' END) AS reason_group,
       date_trunc('month', p.created_at)           AS month,
       date_diff('day', c.opened_at, p.created_at) AS account_age_days
FROM payments p
JOIN customers c USING (customer_id)
JOIN v_payment_outcomes o USING (payment_id)
LEFT JOIN reason_code_map m ON m.code = p.decline_reason;

CREATE OR REPLACE VIEW v_originals AS
SELECT o.*,
       r.retry_id IS NOT NULL                          AS was_retried,
       o.reached_settled OR coalesce(r.retry_settled, false) AS ever_settled
FROM v_payments o
LEFT JOIN (
    SELECT retry_of, any_value(payment_id) AS retry_id, bool_or(reached_settled) AS retry_settled
    FROM v_payments WHERE is_retry GROUP BY retry_of
) r ON r.retry_of = o.payment_id
WHERE NOT o.is_retry;
"""


def connect(path=None, read_only=False):
    con = duckdb.connect(str(path or config.DB_PATH), read_only=read_only)
    return con


def load_reason_codes(con):
    rc = config.reason_codes()
    df = pd.DataFrame([
        {"code": str(code), "reason_group": v["group"], "stage": v["stage"],
         "hard": bool(v.get("hard", False)), "description": v["description"]}
        for code, v in rc["codes"].items()
    ])
    con.register("_rc", df)
    con.execute("CREATE OR REPLACE TABLE reason_code_map AS SELECT * FROM _rc")
    con.unregister("_rc")


def prepare(con):
    """Create the reason-code map, shared views and the reconciliation result tables."""
    load_reason_codes(con)
    con.execute(VIEWS)
    from core import reconcile
    reconcile.materialise(con)


def is_prepared(con):
    names = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    return {"payments", "v_payments", "recon_exceptions"} <= names
