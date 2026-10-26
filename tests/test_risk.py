import numpy as np
import pytest

from core import config, risk
from generator.generate import fraud_cost_curve


@pytest.fixture(scope="module")
def analysis(con):
    return risk.analyse(con)


def test_recommended_threshold_within_2pct_of_truth(analysis, truth):
    _, _, _, opt = analysis
    t_true = truth["fraud_optimum"]["threshold"]
    assert abs(opt["threshold"] - t_true) / t_true <= 0.02


def test_sql_curve_matches_direct_evaluation(con, analysis):
    _, _, curve, _ = analysis
    df = con.execute("SELECT p.risk_score, f.is_fraud, p.amount_gbp FROM payments p JOIN fraud_truth f USING (payment_id)").df()
    direct = fraud_cost_curve(df["risk_score"].to_numpy(), df["is_fraud"].to_numpy(), df["amount_gbp"].to_numpy(),
                              config.assumptions()["risk"])
    for k in (0, 40, 71, 120, 200):
        assert curve.iloc[k]["threshold"] == pytest.approx(direct[k][0])
        assert curve.iloc[k]["total_cost"] == pytest.approx(direct[k][1], rel=1e-9)


def test_optimum_is_interior_and_cheaper_than_current(analysis):
    _, _, curve, opt = analysis
    cur = risk.at_threshold(curve, risk.risk_cfg()["current_threshold"])
    assert 0 < opt["threshold"] < 1
    assert opt["total_cost"] < cur["total_cost"]
    assert opt["total_cost"] < curve.iloc[0]["total_cost"] and opt["total_cost"] < curve.iloc[-1]["total_cost"]


def test_friction_updates_with_threshold(analysis):
    _, _, curve, _ = analysis
    f = curve["friction"].to_numpy()
    assert (np.diff(f) <= 1e-12).all()
    assert f[0] == pytest.approx(1.0) and f[-1] == 0


def test_six_sensitivity_scenarios(analysis):
    _, buckets, _, opt = analysis
    t_base, sens = risk.sensitivity(buckets)
    assert t_base == opt["threshold"]
    assert len(sens) == 6
    assert set(sens.columns) >= {"scenario", "optimal_threshold", "shift", "beyond_margin"}


def test_sensitivity_flag_respects_margin(analysis, monkeypatch):
    _, buckets, _, _ = analysis
    cfg = dict(risk.risk_cfg())
    monkeypatch.setattr(risk, "risk_cfg", lambda: {**cfg, "sensitivity_margin": 1.0})
    assert not risk.sensitivity(buckets)[1]["beyond_margin"].any()
    monkeypatch.setattr(risk, "risk_cfg", lambda: {**cfg, "sensitivity_margin": 0.0})
    sens = risk.sensitivity(buckets)[1]
    assert (sens["beyond_margin"] == (sens["shift"].abs() > 1e-9)).all()
    assert sens["beyond_margin"].any()


def test_review_mode_costs_more_and_raises_threshold(con, analysis):
    _, _, _, opt = analysis
    _, _, _, opt_r = risk.analyse(con, risk.costs("review"))
    assert opt_r["total_cost"] > opt["total_cost"]
    assert opt_r["threshold"] >= opt["threshold"]


def test_recommendation_under_120_words(analysis):
    _, buckets, curve, opt = analysis
    cur = risk.at_threshold(curve, risk.risk_cfg()["current_threshold"])
    t_base, sens = risk.sensitivity(buckets)
    text, saving = risk.recommendation(opt, cur, risk.costs(), t_base, sens)
    assert len(text.split()) < 120
    assert f"{opt['threshold']:.3f}" in text and "ask" in text
    assert saving > 0


def test_rules_report_catch_and_false_decline_rates(con, analysis):
    _, _, _, opt = analysis
    _, rules = risk.rules_comparison(con, opt["threshold"])
    assert len(rules) == 3
    assert rules["catch_rate"].between(0, 1).all() and rules["false_decline_rate"].between(0, 1).all()
    burst = rules[rules["rule"].str.startswith("Burst")].iloc[0]
    assert burst["precision"] > 0.5
