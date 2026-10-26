import pytest

from core import ledger, reconcile


@pytest.fixture(scope="module")
def exceptions(con):
    return con.execute("SELECT * FROM recon_exceptions").df()


@pytest.fixture(scope="module")
def score(con, exceptions, truth):
    return reconcile.score_against_truth(con, exceptions, truth)


def test_ledger_balances_except_planted_r5(con, truth):
    unbalanced = set(ledger.unbalanced(con)["payment_id"])
    planted = {b["payment_id"] for b in truth["breaks"] if b["type"] == "R5"}
    assert unbalanced == planted


def test_recall_at_least_95pct(score):
    assert score["recall"] >= 0.95


def test_classification_at_least_90pct(score):
    assert score["classification_accuracy"] >= 0.90


def test_false_positive_rate_under_2pct(score):
    assert score["false_positive_rate"] < 0.02


def test_timing_differences_never_flagged(score, con, truth):
    assert score["r6_flagged"] == 0
    r6 = [b["payment_id"] for b in truth["breaks"] if b["type"] == "R6"]
    statuses = con.execute("SELECT DISTINCT status FROM recon_results WHERE payment_id IN (SELECT unnest(?))", [r6]).fetchall()
    assert {s[0] for s in statuses} <= {"matched_timing"}


def test_queue_sorted_by_value_then_age(exceptions):
    v = exceptions["amount_at_risk"].to_numpy()
    assert (v[:-1] >= v[1:]).all()


def test_each_break_explains_itself(exceptions):
    assert exceptions["pass_failed"].notna().all()
    assert exceptions["values_compared"].str.len().gt(0).all()
    assert exceptions["suggested_owner"].isin(["Operations", "Finance", "Scheme liaison"]).all()
    assert exceptions["age_bucket"].notna().all()


def test_fee_and_typo_mismatches_go_to_different_passes(exceptions):
    passes = exceptions.groupby("break_type")["pass_failed"].first()
    assert passes["amount_mismatch_fee"] == "Pass 3 amount after fees"
    assert passes["amount_mismatch_unexplained"] == "Pass 4 unexplained amount"
