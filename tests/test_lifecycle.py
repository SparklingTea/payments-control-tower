import duckdb

from core import lifecycle


def test_generated_event_logs_are_valid(con):
    v = lifecycle.validate(con)
    assert len(v["invalid_transitions"]) == 0
    assert len(v["final_state_mismatches"]) == 0


def test_three_injected_invalid_transitions_are_caught(con):
    injected = lifecycle.inject_invalid_transitions(con, 3)
    caught = con.execute(lifecycle.invalid_transitions_sql("events_injected")).df()
    assert len(caught) == 3
    assert set(caught["payment_id"]) == set(injected["payment_id"])


def test_every_reason_code_maps_or_is_listed(con):
    cov = lifecycle.reason_code_coverage(con)
    unmapped = cov[cov["reason_group"] == "UNMAPPED"]
    mapped = cov[cov["reason_group"] != "UNMAPPED"]
    assert set(mapped["reason_group"]) <= {"insufficient_funds", "technical", "risk_rule", "invalid_details", "other"}
    # the generator plants unknown codes; they must surface, not vanish
    assert set(unmapped["code"]) == {"D99", "F77"}
    assert cov["events"].sum() == con.execute(
        "SELECT count(*) FROM payment_events WHERE reason_code IS NOT NULL").fetchone()[0]


def test_idempotency_100_duplicated_requests():
    t = duckdb.connect()
    t.execute(lifecycle.PAYMENTS_DDL)
    gw = lifecycle.PaymentGateway(t)
    originals = []
    for i in range(100):
        req = {"idempotency_key": f"key-{i}", "customer_id": "C000001", "rail": "card", "amount_gbp": 10.0 + i}
        first = gw.submit(req)
        dup = gw.submit(req)
        assert first["created"] and not dup["created"]
        assert dup["payment_id"] == first["payment_id"]
        originals.append(first["payment_id"])
    assert t.execute("SELECT count(*) FROM payments").fetchone()[0] == 100
    assert len(set(originals)) == 100


def test_idempotency_keys_unique_in_data(con):
    n, k = con.execute("SELECT count(*), count(DISTINCT idempotency_key) FROM payments").fetchone()
    assert n == k


def test_retries_are_new_payments_linked_to_declines(con):
    bad = con.execute("""
        SELECT count(*) FROM payments r LEFT JOIN payments o ON o.payment_id = r.retry_of
        WHERE r.retry_of IS NOT NULL AND (o.payment_id IS NULL OR o.final_status <> 'declined'
              OR o.created_at >= r.created_at)
    """).fetchone()[0]
    assert bad == 0
    hard_retried = con.execute("""
        SELECT count(*) FROM payments r JOIN payments o ON o.payment_id = r.retry_of
        JOIN reason_code_map m ON m.code = o.decline_reason WHERE m.hard
    """).fetchone()[0]
    assert hard_retried == 0
