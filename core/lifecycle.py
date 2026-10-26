"""Payment lifecycle: valid transitions, event-log validation, reason-code mapping, idempotent submission."""
import uuid

import pandas as pd

from core import config

STATES = ["initiated", "authorised", "declined", "settled", "failed", "returned", "disputed"]
VALID_TRANSITIONS = {
    ("initiated", "authorised"), ("initiated", "declined"),
    ("authorised", "settled"), ("authorised", "failed"),
    ("settled", "returned"), ("settled", "disputed"),
}
TERMINAL = {"declined", "failed", "settled", "returned", "disputed"}


def _valid_pairs_sql():
    return ", ".join(f"('{a}', '{b}')" for a, b in sorted(VALID_TRANSITIONS))


def invalid_transitions_sql(events_table="payment_events"):
    return f"""
WITH seq AS (
    SELECT payment_id, event_id, ts, state,
           lag(state) OVER (PARTITION BY payment_id ORDER BY ts, event_id) AS prev_state
    FROM {events_table}
)
SELECT payment_id, event_id, ts, prev_state, state,
       CASE WHEN prev_state IS NULL THEN 'first event is not initiated'
            ELSE 'invalid transition' END AS problem
FROM seq
WHERE (prev_state IS NULL AND state <> 'initiated')
   OR (prev_state IS NOT NULL AND (prev_state, state) NOT IN ({_valid_pairs_sql()}))
ORDER BY payment_id, ts
"""


def final_state_mismatch_sql(events_table="payment_events", payments_table="payments"):
    return f"""
WITH last_event AS (
    SELECT payment_id, arg_max(state, ts) AS last_state, count(*) AS n_events
    FROM {events_table} GROUP BY payment_id
)
SELECT p.payment_id, p.final_status, l.last_state, l.n_events
FROM {payments_table} p LEFT JOIN last_event l USING (payment_id)
WHERE l.last_state IS NULL OR p.final_status IS DISTINCT FROM l.last_state
   OR p.final_status NOT IN ({", ".join(f"'{s}'" for s in sorted(TERMINAL))})
"""


def validate(con, events_table="payment_events", payments_table="payments"):
    invalid = con.execute(invalid_transitions_sql(events_table)).df()
    mismatched = con.execute(final_state_mismatch_sql(events_table, payments_table)).df()
    return {"invalid_transitions": invalid, "final_state_mismatches": mismatched}


def inject_invalid_transitions(con, n=3, source="payment_events", target="events_injected"):
    """Copy the event log and inject n deliberately invalid transitions, for the validator demo/test."""
    con.execute(f"CREATE OR REPLACE TEMP TABLE {target} AS SELECT * FROM {source}")
    patterns = [
        ("declined", "settled", "a declined payment that later settles"),
        ("settled", "authorised", "a settled payment re-authorised"),
        ("failed", "returned", "a failed payment returned"),
    ]
    injected = []
    max_id = con.execute(f"SELECT max(event_id) FROM {target}").fetchone()[0]
    for i in range(n):
        after, new_state, label = patterns[i % len(patterns)]
        pid, ts = con.execute(f"""
            SELECT payment_id, max(ts) FROM {target}
            WHERE payment_id IN (SELECT payment_id FROM {target} GROUP BY payment_id
                                 HAVING arg_max(state, ts) = '{after}')
            GROUP BY payment_id ORDER BY payment_id LIMIT 1 OFFSET {i}
        """).fetchone()
        con.execute(f"INSERT INTO {target} (event_id, payment_id, ts, state, reason_code) "
                    f"VALUES (?, ?, ?, ?, NULL)", [max_id + i + 1, pid, ts + pd.Timedelta(minutes=5), new_state])
        injected.append({"payment_id": pid, "transition": f"{after} -> {new_state}", "description": label})
    return pd.DataFrame(injected)


def reason_code_coverage(con):
    """Every reason code seen in the data with its group; unmapped codes are surfaced, never hidden."""
    return con.execute("""
        SELECT e.reason_code AS code, coalesce(m.reason_group, 'UNMAPPED') AS reason_group,
               coalesce(m.description, '(not in reason_codes.yaml)') AS description,
               e.state AS stage, count(*) AS events
        FROM payment_events e LEFT JOIN reason_code_map m ON m.code = e.reason_code
        WHERE e.reason_code IS NOT NULL
        GROUP BY ALL ORDER BY events DESC
    """).df()


def state_counts_sql():
    return "SELECT final_status, count(*) AS payments FROM payments GROUP BY 1 ORDER BY 2 DESC"


PAYMENTS_DDL = """
CREATE TABLE IF NOT EXISTS payments (
    payment_id VARCHAR PRIMARY KEY, customer_id VARCHAR, created_at TIMESTAMP, rail VARCHAR,
    amount_gbp DOUBLE, payee_type VARCHAR, category VARCHAR, final_status VARCHAR, decline_reason VARCHAR,
    retry_of VARCHAR, idempotency_key VARCHAR UNIQUE, risk_score DOUBLE, cost_gbp DOUBLE, customer_fee_gbp DOUBLE
)"""


class PaymentGateway:
    """Idempotent payment submission against the payments table.

    A repeated request with the same idempotency key returns the original payment and never creates another.
    """

    def __init__(self, con, table="payments"):
        self.con = con
        self.table = table

    def submit(self, request: dict) -> dict:
        key = request["idempotency_key"]
        existing = self.con.execute(
            f"SELECT payment_id FROM {self.table} WHERE idempotency_key = ?", [key]).fetchone()
        if existing:
            return {"payment_id": existing[0], "created": False}
        payment_id = "P" + uuid.uuid4().hex[:12].upper()
        self.con.execute(
            f"INSERT INTO {self.table} (payment_id, customer_id, created_at, rail, amount_gbp, payee_type, "
            f"category, final_status, idempotency_key) VALUES (?, ?, now(), ?, ?, ?, ?, 'initiated', ?)",
            [payment_id, request["customer_id"], request["rail"], request["amount_gbp"],
             request.get("payee_type", "existing_payee"), request.get("category", "transfers"), key])
        return {"payment_id": payment_id, "created": True}


def configured_vs_actual_sql():
    rails = config.assumptions()["rails"]
    rows = " UNION ALL ".join(
        f"SELECT '{r}' AS rail, {rc['auth_rate']} AS cfg_auth, {rc['failure_rate']} AS cfg_fail" for r, rc in rails.items())
    return f"""
WITH cfg AS ({rows})
SELECT v.rail, count(*) AS originals,
       cfg.cfg_auth AS configured_auth_rate,
       avg(v.reached_auth::INT) AS actual_auth_rate,
       cfg.cfg_fail AS configured_failure_rate,
       sum(v.is_failed::INT) / sum(v.reached_auth::INT) AS actual_failure_rate,
       cfg.cfg_auth * (1 - cfg.cfg_fail) AS configured_success_rate,
       avg(v.reached_settled::INT) AS actual_success_rate
FROM v_payments v JOIN cfg USING (rail)
WHERE NOT v.is_retry
GROUP BY v.rail, cfg.cfg_auth, cfg.cfg_fail
ORDER BY originals DESC
"""
