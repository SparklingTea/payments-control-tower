import duckdb
import pandas as pd
import pytest

from core import analytics, db, metrics

# ---------- 20-row hand-computed fixture ----------
# (payment_id, customer, rail, amount, path, reason, retry_of, cost, settle_hours)
ROWS = [
    ("P01", "C1", "card", 10, ["settled"], None, None, 0.20, 24),
    ("P02", "C1", "card", 20, ["settled"], None, None, 0.20, 48),
    ("P03", "C1", "card", 30, ["declined"], "D51", None, 0.02, None),
    ("P04", "C1", "card", 40, ["settled"], None, "P03", 0.20, 24),
    ("P05", "C1", "card", 50, ["declined"], "D68", None, 0.02, None),
    ("P06", "C1", "card", 60, ["declined"], "D68", "P05", 0.02, None),
    ("P07", "C1", "card", 70, ["settled", "disputed"], "4853", None, 0.20, 24),
    ("P08", "C1", "card", 80, ["settled", "returned"], "R10", None, 0.20, 24),
    ("P09", "C2", "faster_payments", 100, ["settled"], None, None, 0.06, 1),
    ("P10", "C2", "faster_payments", 110, ["settled"], None, None, 0.06, 1),
    ("P11", "C2", "faster_payments", 120, ["failed"], "F01", None, 0.02, None),
    ("P12", "C2", "faster_payments", 130, ["declined"], "D14", None, 0.01, None),
    ("P13", "C2", "faster_payments", 140, ["settled"], None, None, 0.06, 2),
    ("P14", "C2", "bacs_credit", 500, ["settled"], None, None, 0.03, 50),
    ("P15", "C2", "bacs_credit", 600, ["settled"], None, None, 0.03, 74),
    ("P16", "C2", "bacs_credit", 700, ["settled", "returned"], "R02", None, 0.03, 50),
    ("P17", "C2", "direct_debit", 40, ["settled"], None, None, 0.04, 72),
    ("P18", "C2", "direct_debit", 45, ["failed"], "F03", None, 0.00, None),
    ("P19", "C1", "direct_debit", 50, ["declined"], "D99", None, 0.00, None),
    ("P20", "C2", "faster_payments", 130, ["settled"], None, "P12", 0.06, 1),
]

# Worked out by hand from the rows above.
EXPECTED = {
    "auth_rate": 13 / 17,             # 17 originals, 13 authorised
    "decline_rate": 4 / 17,
    "success_rate": 13 / 17,          # 11 originals settled + P03 and P12 recovered by retry
    "retry_recovery_rate": 2 / 3,     # P04 and P20 settled, P06 did not
    "return_rate": 2 / 13,            # P08, P16 out of 13 settled
    "dispute_rate": 1 / 5,            # P07 out of 5 settled card payments
    "cost_per_settled": 1.46 / 13,
    "time_to_settle_p50": 24.0,
}


@pytest.fixture(scope="module")
def fixture_con():
    t0 = pd.Timestamp("2026-01-05 10:00")
    pays, events = [], []
    for i, (pid, cust, rail, amt, path, reason, retry_of, cost, hrs) in enumerate(ROWS):
        created = t0 + pd.Timedelta(minutes=10 * i)
        final = path[-1]
        pays.append({"payment_id": pid, "customer_id": cust, "created_at": created, "rail": rail,
                     "amount_gbp": float(amt), "payee_type": "merchant", "category": "x", "final_status": final,
                     "decline_reason": reason, "retry_of": retry_of, "idempotency_key": pid,
                     "risk_score": 0.1, "cost_gbp": cost, "customer_fee_gbp": 0.0})
        ev = [(created, "initiated", None)]
        if path[0] == "declined":
            ev.append((created + pd.Timedelta(seconds=1), "declined", reason))
        else:
            ev.append((created + pd.Timedelta(seconds=1), "authorised", None))
            if path[0] == "failed":
                ev.append((created + pd.Timedelta(minutes=5), "failed", reason))
            else:
                s = created + pd.Timedelta(hours=hrs)
                ev.append((s, "settled", None))
                if len(path) > 1:
                    ev.append((s + pd.Timedelta(days=3), path[1], reason))
        events += [{"payment_id": pid, "ts": ts, "state": st, "reason_code": rc} for ts, st, rc in ev]
    customers = pd.DataFrame({"customer_id": ["C1", "C2"], "segment": ["retail", "business"],
                              "opened_at": [pd.Timestamp("2025-01-01")] * 2, "risk_band": ["low"] * 2,
                              "region": ["London"] * 2})
    payments = pd.DataFrame(pays)
    payment_events = pd.DataFrame(events)
    payment_events.insert(0, "event_id", range(1, len(payment_events) + 1))
    c = duckdb.connect()
    for name, df in (("customers", customers), ("payments", payments), ("payment_events", payment_events)):
        c.register("_df", df)
        c.execute(f"CREATE TABLE {name} AS SELECT * FROM _df")
        c.unregister("_df")
    db.load_reason_codes(c)
    c.execute(db.VIEWS)
    return c


@pytest.mark.parametrize("name", list(EXPECTED))
def test_registry_matches_hand_computed_fixture(fixture_con, name):
    _, v = metrics.value(fixture_con, name)
    assert v == pytest.approx(EXPECTED[name], abs=1e-9)


def test_decline_groups_sum_to_decline_rate(fixture_con):
    g = fixture_con.execute(metrics.decline_by_group_sql()).df()
    assert g["decline_rate"].sum() == pytest.approx(4 / 17)
    assert set(g["reason_group"]) == {"insufficient_funds", "technical", "invalid_details", "unmapped"}


# ---------- planted problems on the full dataset ----------

def test_p1_incident_flagged_and_excluded(con, truth):
    p1 = truth["planted_problems"]["P1"]
    _, hourly, inc = analytics.detect_incidents(con)
    assert len(inc) == 1
    assert inc.iloc[0]["start"] == pd.Timestamp(p1["date"]) + pd.Timedelta(hours=p1["start_hour"])
    assert inc.iloc[0]["end"] == pd.Timestamp(p1["date"]) + pd.Timedelta(hours=p1["end_hour"])
    trend = analytics.daily_trend(hourly)
    day = trend[trend["day"] == pd.Timestamp(p1["date"])].iloc[0]
    assert day["baseline_excl_incidents"] < day["with_incidents"]


def test_p2_hotspot(con, truth):
    _, cats = analytics.detect_hotspots(con)
    hot = cats[cats["hotspot"]]
    assert list(hot["category"]) == [truth["planted_problems"]["P2"]["category"]]
    assert 2.5 <= hot.iloc[0]["ratio"] <= 4.0


def test_p3_bimodal_settlement(con, truth):
    _, _, clusters = analytics.detect_settle_clusters(con)
    assert list(clusters[clusters["bimodal"]]["rail"]) == [truth["planted_problems"]["P3"]["rail"]]


def test_p4_cost_drift_flagged(con, truth):
    p4 = truth["planted_problems"]["P4"]
    _, _, _, drift = analytics.detect_cost_drift(con)
    flagged = drift[drift["flagged"]]
    assert list(zip(flagged["segment"], flagged["rail"])) == [(p4["segment"], p4["rail"])]
    assert abs(flagged.iloc[0]["blended_change"]) < flagged.iloc[0]["change"] / 3


def test_dashboard_tiles_equal_registry_sql(con):
    flt = {"rails": ["card", "faster_payments"], "segments": ["retail"], "start": "2026-02-01", "end": "2026-04-30"}
    for tile in metrics.headline(con, flt):
        shown = con.execute(tile["sql"]).fetchone()[0]
        _, registry_value = metrics.value(con, tile["name"], flt)
        assert tile["sql"] == metrics.metric_sql(tile["name"], flt)
        # same SQL; DuckDB's parallel float sums can differ in the last bit between runs
        assert shown == pytest.approx(registry_value, rel=1e-12)
        assert tile["value"] == pytest.approx(registry_value, rel=1e-12)


def test_filters_reject_unknown_values():
    with pytest.raises(ValueError):
        metrics.filter_clause({"rails": ["card'; DROP TABLE payments; --"]})
