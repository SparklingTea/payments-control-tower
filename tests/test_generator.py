from core import config, lifecycle
from generator.generate import generate, table_fingerprint


def _cfg():
    cfg = dict(config.assumptions())
    cfg["_reason_codes"] = config.reason_codes()
    return cfg


def test_same_seed_gives_identical_tables():
    a, ta = generate(_cfg(), seed=7, n_customers=2000, n_payments=20000)
    b, tb = generate(_cfg(), seed=7, n_customers=2000, n_payments=20000)
    for name in a:
        assert table_fingerprint(a[name]) == table_fingerprint(b[name]), name
    assert ta == tb


def test_different_seed_differs():
    a, _ = generate(_cfg(), seed=7, n_customers=2000, n_payments=20000)
    b, _ = generate(_cfg(), seed=8, n_customers=2000, n_payments=20000)
    assert table_fingerprint(a["payments"]) != table_fingerprint(b["payments"])


def test_configured_rates_reproduced_within_1pp(con):
    df = con.execute(lifecycle.configured_vs_actual_sql()).df()
    for _, r in df.iterrows():
        assert abs(r["actual_auth_rate"] - r["configured_auth_rate"]) < 0.01, r["rail"]
        assert abs(r["actual_failure_rate"] - r["configured_failure_rate"]) < 0.01, r["rail"]
        assert abs(r["actual_success_rate"] - r["configured_success_rate"]) < 0.01, r["rail"]


def test_scale_and_tables(con):
    n_cust = con.execute("SELECT count(*) FROM customers").fetchone()[0]
    n_pay = con.execute("SELECT count(*) FROM payments").fetchone()[0]
    assert n_cust == 20000
    assert 280_000 <= n_pay <= 320_000
    for t in ["customers", "payments", "payment_events", "ledger_entries", "scheme_file", "fraud_truth", "config_assumptions"]:
        assert con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] > 0


def test_truth_file_contents(truth):
    assert set(truth["planted_problems"]) == {"P1", "P2", "P3", "P4"}
    assert {"R1", "R2", "R3", "R4", "R5", "R6"} <= set(truth["break_counts"])
    assert 0 < truth["fraud_optimum"]["threshold"] < 1


def test_generation_under_90_seconds(built):
    assert built["seconds"] < 90
