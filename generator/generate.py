"""Seeded synthetic payments book with planted problems.

Writes seven DuckDB tables and data/truth.json (the answer key, read by tests only).
Run: python -m generator.generate [--seed 42] [--customers N] [--payments N] [--out PATH]
"""
import argparse
import hashlib
import json
import time
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

from core import config as cfgmod

DAY = 86400
HOUR = 3600
HOUR_WEIGHTS = np.array([0.5, 0.3, 0.2, 0.2, 0.2, 0.4, 1, 2, 3, 4, 5, 5,
                         6, 5, 5, 5, 5, 5, 6, 6, 5, 4, 2, 1], dtype=float)


def _ts(s):
    return int(pd.Timestamp(s).timestamp())


def _sigmoid(x):
    return 1.0 / (1.0 + np.exp(-x))


def _logit(p):
    return np.log(p / (1 - p))


def _pick(rng, weights: dict, size):
    keys = list(weights)
    p = np.array([weights[k] for k in keys], dtype=float)
    return np.array(keys, dtype=object)[rng.choice(len(keys), size=size, p=p / p.sum())]


def _score_bp(score):
    return np.rint(np.asarray(score) * 10000).astype(np.int64)


def fraud_cost_curve(score, is_fraud, amount, risk_cfg, mode=None):
    """Direct evaluation of C(t) on the threshold grid. Independent of core.risk on purpose."""
    mode = mode or risk_cfg["default_mode"]
    c_d = risk_cfg["false_decline_cost_gbp"]
    c_r = risk_cfg["review_cost_gbp"] if mode == "review" else 0.0
    step_bp = int(round(risk_cfg["grid_step"] * 10000))
    sbp = _score_bp(score)
    loss = amount + risk_cfg["fraud_loss_extra_gbp"]
    rows = []
    for k in range(0, 10000 // step_bp + 1):
        flagged = sbp >= k * step_bp
        missed = (~flagged) & is_fraud
        blocked_genuine = flagged & ~is_fraud
        cost = loss[missed].sum() + c_d * blocked_genuine.sum() + c_r * flagged.sum()
        rows.append((k * step_bp / 10000, cost))
    return rows


def generate(cfg, seed=None, n_customers=None, n_payments=None):
    g = cfg["generator"]
    seed = g["seed"] if seed is None else seed
    n_customers = n_customers or g["n_customers"]
    n_payments = n_payments or g["n_payments"]
    rng = np.random.default_rng(seed)
    rails_cfg = cfg["rails"]
    start, end = _ts(g["start_date"]), _ts(g["end_date"]) + DAY - 1
    as_of = _ts(g["end_date"]) + (g["recon_as_of_days_after_end"] + 1) * DAY - 1

    # ---------------- customers ----------------
    segs = _pick(rng, {k: v["share"] for k, v in cfg["segments"].items()}, n_customers)
    is_new = rng.random(n_customers) < cfg["new_customer_share"]
    opened = np.where(
        is_new,
        rng.integers(start, end - 7 * DAY, n_customers),
        rng.integers(start - 3 * 365 * DAY, start, n_customers),
    )
    customers = pd.DataFrame({
        "customer_id": [f"C{i + 1:06d}" for i in range(n_customers)],
        "segment": segs,
        "opened_at": opened,
        "risk_band": _pick(rng, cfg["risk_bands"], n_customers),
        "region": np.array(cfg["regions"], dtype=object)[rng.integers(0, len(cfg["regions"]), n_customers)],
    })

    # ---------------- original payments ----------------
    vw = np.array([cfg["segments"][s]["volume_weight"] for s in segs]) * rng.gamma(2.0, 0.5, n_customers)
    cust = rng.choice(n_customers, size=n_payments, p=vw / vw.sum())
    lo = np.maximum(start, opened[cust])
    day0 = (lo - start) // DAY
    days = day0 + np.floor(rng.random(n_payments) * ((end - start) // DAY + 1 - day0)).astype(np.int64)
    tod = rng.choice(24, size=n_payments, p=HOUR_WEIGHTS / HOUR_WEIGHTS.sum()) * HOUR + rng.integers(0, HOUR, n_payments)
    created = start + days * DAY + tod
    created = np.where(created < lo, lo + rng.integers(0, HOUR, n_payments), created)
    created = np.minimum(created, end)

    seg_of = segs[cust]
    rail = np.empty(n_payments, dtype=object)
    for s, mix in cfg["rail_mix"].items():
        m = seg_of == s
        rail[m] = _pick(rng, mix, m.sum())

    category = np.empty(n_payments, dtype=object)
    payee = np.empty(n_payments, dtype=object)
    amount = np.empty(n_payments)
    for r, rc in rails_cfg.items():
        m = rail == r
        n = m.sum()
        category[m] = _pick(rng, rc["categories"], n)
        payee[m] = _pick(rng, rc["payee_types"], n)
        amount[m] = rc["amount"]["median"] * np.exp(rc["amount"]["sigma"] * rng.standard_normal(n))
    amount = np.maximum(np.round(amount, 2), 1.0)

    # ---------------- fraud truth ----------------
    fc = cfg["fraud"]
    med = np.array([rails_cfg[r]["amount"]["median"] for r in rail])
    base = np.array([rails_cfg[r]["fraud_base_rate"] for r in rail])
    band = customers["risk_band"].to_numpy()[cust]
    new_acct = (created - opened[cust]) < 30 * DAY
    amt_z = np.log(amount) - np.log(med)
    logit = (_logit(base) + fc["amount_coef"] * amt_z + fc["new_account_coef"] * new_acct
             + fc["new_payee_coef"] * (payee == "new_payee") + fc["high_risk_band_coef"] * (band == "high"))
    is_fraud = rng.random(n_payments) < _sigmoid(logit)

    # fraud bursts: two quick follow-on fraud payments from the same compromised account
    parents = np.flatnonzero(is_fraud & np.isin(rail, ["card", "faster_payments"]))
    parents = parents[rng.random(parents.size) < fc["burst_share"]]
    b_idx = np.repeat(parents, 2)
    b_gap = rng.integers(60, 240, b_idx.size)
    b_gap[1::2] += b_gap[0::2]
    b_created = np.minimum(created[b_idx] + b_gap, end)
    b_rail = rail[b_idx]
    b_amount = np.maximum(np.round(np.array([rails_cfg[r]["amount"]["median"] for r in b_rail])
                                   * np.exp(1.1 * rng.standard_normal(b_idx.size)), 2), 1.0)
    b_payee = np.where(b_rail == "faster_payments", "new_payee", payee[b_idx]).astype(object)

    cust = np.concatenate([cust, cust[b_idx]])
    created = np.concatenate([created, b_created])
    rail = np.concatenate([rail, b_rail])
    category = np.concatenate([category, category[b_idx]])
    payee = np.concatenate([payee, b_payee])
    amount = np.concatenate([amount, b_amount])
    is_fraud = np.concatenate([is_fraud, np.ones(b_idx.size, bool)])
    n0 = created.size

    def risk_score(idx_cust, idx_created, idx_rail, idx_amount, idx_payee, idx_fraud):
        sc = fc["score"]
        m = np.array([rails_cfg[r]["amount"]["median"] for r in idx_rail])
        feats = (np.log(idx_amount) - np.log(m)) + 1.5 * ((idx_created - opened[idx_cust]) < 30 * DAY) \
            + 1.2 * (idx_payee == "new_payee")
        latent = sc["intercept"] + sc["signal"] * idx_fraud + sc["feature_weight"] * feats \
            + sc["noise_sd"] * rng.standard_normal(idx_cust.size)
        return np.clip(np.round(_sigmoid(latent), 4), 0.0, 0.9999)

    score = risk_score(cust, created, rail, amount, payee, is_fraud)

    # ---------------- authorisation / decline ----------------
    rcodes = cfg["_reason_codes"]["codes"]
    thr_bp = int(round(cfg["risk"]["current_threshold"] * 10000))
    p2, p1 = cfg["planted"]["P2"], cfg["planted"]["P1"]
    unmapped_rate = g["unmapped_code_rate"]
    declined = np.zeros(n0, bool)
    reason = np.full(n0, None, dtype=object)
    risk_dec = _score_bp(score) >= thr_bp
    declined[risk_dec] = True
    reason[risk_dec] = "D59"
    u = rng.random(n0)
    for r, rc in rails_cfg.items():
        m = (rail == r) & ~risk_dec
        d = 1 - rc["auth_rate"]
        p_risk = risk_dec[rail == r].mean()
        if r == p2["rail"]:
            hot = m & (category == p2["category"])
            s_x = hot.sum() / m.sum()
            q_rest = (d - p_risk) / ((1 - p_risk) * (1 + (p2["decline_multiplier"] - 1) * s_x))
            q = np.where(hot, q_rest * p2["decline_multiplier"], q_rest)
            dec = m & (u < q)
            rule_block = dec & hot & (u >= q_rest)
            reason[rule_block] = "D57"
            declined |= rule_block
            base_dec = dec & ~rule_block
        else:
            q = (d - p_risk) / (1 - p_risk)
            base_dec = m & (u < q)
        declined |= base_dec
        reason[base_dec] = _pick(rng, rc["decline_codes"], base_dec.sum())

    p1_start = _ts(p1["date"]) + p1["start_hour"] * HOUR
    p1_end = _ts(p1["date"]) + p1["end_hour"] * HOUR
    in_p1 = (~declined) & (created >= p1_start) & (created < p1_end) & np.isin(rail, p1["rails"])
    p1_hit = in_p1 & (rng.random(n0) < p1["extra_timeout_prob"])
    declined[p1_hit] = True
    reason[p1_hit] = "D68"

    soft = declined & ~np.isin(reason, ["D59", "D57", "D68"])
    unm = soft & (rng.random(n0) < unmapped_rate * 8)
    reason[unm] = "D99"

    # ---------------- customer retries ----------------
    rt = cfg["retries"]
    group_of = np.array([rcodes.get(c, {}).get("group", "unmapped") if c else "" for c in reason], dtype=object)
    hard = np.array([bool(rcodes.get(c, {}).get("hard", False)) if c else False for c in reason])
    p_retry = np.array([rt["probability"].get(gp, 0.0) for gp in group_of])
    if not rt["retry_hard_declines"]:
        p_retry[hard] = 0.0
    do_retry = declined & (rng.random(n0) < p_retry)
    orig_idx = np.flatnonzero(do_retry)
    nr = orig_idx.size
    r_created = created[orig_idx] + 4 + rng.integers(rt["delay_minutes"]["min"] * 60, rt["delay_minutes"]["max"] * 60, nr)
    r_score = risk_score(cust[orig_idx], r_created, rail[orig_idx], amount[orig_idx], payee[orig_idx], is_fraud[orig_idx])
    r_success = np.array([rt["success"].get(gp, 0.5) for gp in group_of[orig_idx]])
    r_risk = _score_bp(r_score) >= thr_bp
    r_declined = r_risk | (rng.random(nr) >= r_success)
    r_reason = np.where(r_risk, "D59", np.where(r_declined, reason[orig_idx], None)).astype(object)

    n = n0 + nr
    cust = np.concatenate([cust, cust[orig_idx]])
    created = np.concatenate([created, r_created])
    rail = np.concatenate([rail, rail[orig_idx]])
    category = np.concatenate([category, category[orig_idx]])
    payee = np.concatenate([payee, payee[orig_idx]])
    amount = np.concatenate([amount, amount[orig_idx]])
    is_fraud = np.concatenate([is_fraud, is_fraud[orig_idx]])
    score = np.concatenate([score, r_score])
    declined = np.concatenate([declined, r_declined])
    reason = np.concatenate([reason, r_reason])
    retry_of_tmp = np.concatenate([np.full(n0, -1), orig_idx])

    # ---------------- settlement / failure / post-settlement ----------------
    auth_ts = created + rng.integers(1, 5, n)
    failed = np.zeros(n, bool)
    settled_ts = np.zeros(n, np.int64)
    fail_ts = np.zeros(n, np.int64)
    p3 = cfg["planted"]["P3"]
    hour_of = (created % DAY) // HOUR
    for r, rc in rails_cfg.items():
        m = (rail == r) & ~declined
        k = m.sum()
        f = rng.random(k) < rc["failure_rate"]
        idx = np.flatnonzero(m)
        failed[idx[f]] = True
        reason[idx[f]] = _pick(rng, rc["failure_codes"], f.sum())
        hrs = np.maximum(rc["settle_hours"]["mean"] + rc["settle_hours"]["sd"] * rng.standard_normal(k),
                         rc["settle_hours"]["mean"] * 0.1)
        if r == p3["rail"]:
            hrs = hrs + np.where(hour_of[idx] >= p3["cutoff_hour"], p3["extra_hours"], 0)
        settled_ts[idx] = created[idx] + np.rint(hrs * HOUR).astype(np.int64) + 5
        fail_ts[idx] = created[idx] + rng.integers(60, 2 * HOUR, k)
    unm_f = failed & (rng.random(n) < unmapped_rate * 8)
    reason[unm_f] = "F77"
    settled = ~declined & ~failed

    returned = np.zeros(n, bool)
    disputed = np.zeros(n, bool)
    post_ts = np.zeros(n, np.int64)
    up = rng.random(n)
    card_fraud = settled & is_fraud & (rail == "card") & (up < fc["chargeback_rate"])
    disputed |= card_fraud
    reason[card_fraud] = "4837"
    for r, rc in rails_cfg.items():
        m = settled & (rail == r) & ~disputed
        k = m.sum()
        v = rng.random(k)
        idx = np.flatnonzero(m)
        dsp = idx[v < rc["dispute_rate"]]
        ret = idx[(v >= rc["dispute_rate"]) & (v < rc["dispute_rate"] + rc["return_rate"])]
        disputed[dsp] = True
        reason[dsp] = "4853"
        returned[ret] = True
        reason[ret] = _pick(rng, rc["return_codes"], ret.size)
    post_ts[returned] = settled_ts[returned] + rng.integers(1 * DAY, 10 * DAY, returned.sum())
    post_ts[disputed] = settled_ts[disputed] + rng.integers(5 * DAY, 40 * DAY, disputed.sum())

    final = np.where(declined, "declined", np.where(failed, "failed", np.where(
        disputed, "disputed", np.where(returned, "returned", "settled")))).astype(object)

    # cost per payment (P4: one segment x rail drifts from month 4)
    p4 = cfg["planted"]["P4"]
    segp = segs[cust]
    month = pd.to_datetime(created, unit="s").month.to_numpy()
    rail_cost = np.array([rails_cfg[r]["cost_gbp"] for r in rail])
    dec_cost = np.array([rails_cfg[r]["decline_cost_gbp"] for r in rail])
    drift = np.where((segp == p4["segment"]) & (rail == p4["rail"]) & (month >= p4["from_month"]), p4["cost_multiplier"], 1.0)
    cost = np.where(settled | returned | disputed, rail_cost * drift * (1 + 0.04 * rng.standard_normal(n)), dec_cost)
    cost = np.round(np.maximum(cost, 0), 4)
    fees = cfg.get("customer_fees", {})
    cust_fee = np.array([fees.get(s, {}).get(r, 0.0) for s, r in zip(segp, rail)])
    cust_fee = np.where(settled | returned | disputed, cust_fee, 0.0)

    # ---------------- ids ----------------
    order = np.lexsort((np.arange(n), created))
    pid_of = np.empty(n, dtype=object)
    pid_of[order] = [f"P{i + 1:07d}" for i in range(n)]
    keys = rng.integers(0, 2 ** 62, n)
    idem = np.array([f"{k:016x}" for k in keys], dtype=object)
    assert len(set(idem)) == n
    retry_of = np.where(retry_of_tmp >= 0, pid_of[np.maximum(retry_of_tmp, 0)], None)

    payments = pd.DataFrame({
        "payment_id": pid_of,
        "customer_id": customers["customer_id"].to_numpy()[cust],
        "created_at": created,
        "rail": rail,
        "amount_gbp": amount,
        "payee_type": payee,
        "category": category,
        "final_status": final,
        "decline_reason": reason,
        "retry_of": retry_of,
        "idempotency_key": idem,
        "risk_score": score,
        "cost_gbp": cost,
        "customer_fee_gbp": cust_fee,
    }).iloc[order].reset_index(drop=True)

    # ---------------- events ----------------
    reached_settle = settled | returned | disputed
    ev = [
        (np.arange(n), created, "initiated", None),
        (np.flatnonzero(~declined), auth_ts[~declined], "authorised", None),
        (np.flatnonzero(declined), auth_ts[declined], "declined", reason[declined]),
        (np.flatnonzero(failed), fail_ts[failed], "failed", reason[failed]),
        (np.flatnonzero(reached_settle), settled_ts[reached_settle], "settled", None),
        (np.flatnonzero(returned), post_ts[returned], "returned", reason[returned]),
        (np.flatnonzero(disputed), post_ts[disputed], "disputed", reason[disputed]),
    ]
    stage = {"initiated": 0, "authorised": 1, "declined": 1, "failed": 2, "settled": 2, "returned": 3, "disputed": 3}
    events = pd.DataFrame({
        "payment_id": np.concatenate([pid_of[i] for i, *_ in ev]),
        "ts": np.concatenate([t for _, t, *_ in ev]),
        "state": np.concatenate([np.full(len(i), s, dtype=object) for i, _, s, _ in ev]),
        "reason_code": np.concatenate([np.full(len(i), None, dtype=object) if rc is None else rc for i, _, _, rc in ev]),
    })
    events["_stage"] = events["state"].map(stage)
    events = events.sort_values(["payment_id", "_stage"], kind="mergesort").drop(columns="_stage").reset_index(drop=True)
    events.insert(0, "event_id", np.arange(1, len(events) + 1))

    # ---------------- ledger ----------------
    s_idx = np.flatnonzero(reached_settle)
    led = []

    def post(idx, account, debit, credit, ts, event):
        led.append(pd.DataFrame({"payment_id": pid_of[idx], "account": account, "debit_gbp": debit,
                                 "credit_gbp": credit, "posted_at": ts, "event": event}))

    post(s_idx, "customer", amount[s_idx], 0.0, settled_ts[s_idx], "settlement")
    post(s_idx, "settlement", 0.0, amount[s_idx], settled_ts[s_idx], "settlement")
    f_idx = s_idx[cust_fee[s_idx] > 0]
    post(f_idx, "customer", cust_fee[f_idx], 0.0, settled_ts[f_idx], "fee")
    post(f_idx, "fee_income", 0.0, cust_fee[f_idx], settled_ts[f_idx], "fee")
    r_idx = np.flatnonzero(returned)
    post(r_idx, "settlement", amount[r_idx], 0.0, post_ts[r_idx], "return")
    post(r_idx, "customer", 0.0, amount[r_idx], post_ts[r_idx], "return")
    w_idx = np.flatnonzero(disputed & is_fraud)
    post(w_idx, "fraud_loss", amount[w_idx], 0.0, post_ts[w_idx], "write_off")
    post(w_idx, "settlement", 0.0, amount[w_idx], post_ts[w_idx], "write_off")
    ledger = pd.concat(led, ignore_index=True)

    # ---------------- scheme file ----------------
    scheme = pd.DataFrame({
        "payment_id": pid_of[s_idx],
        "rail": [rails_cfg[r]["scheme_code"] for r in rail[s_idx]],
        "amount_gbp": amount[s_idx],
        "settled_at": settled_ts[s_idx] + rng.integers(0, 120, s_idx.size),
    })

    # ---------------- planted reconciliation breaks ----------------
    rcfg = cfg["reconciliation"]
    n_breaks = int(round(rcfg["break_share"] * n))
    counts = {k: int(round(v * n_breaks)) for k, v in rcfg["break_mix"].items()}
    pool = rng.permutation(s_idx.size)
    pos = 0
    chosen = {}
    for k in ["R1", "R2", "R3", "R5", "R6"]:
        chosen[k] = pool[pos:pos + counts[k]]
        pos += counts[k]
    truth_breaks = []
    fee_by_rail = {rc["scheme_code"]: rc["cost_gbp"] for rc in rails_cfg.values()}

    for i in chosen["R2"]:
        a = scheme.at[i, "amount_gbp"]
        fee = fee_by_rail[scheme.at[i, "rail"]]
        if rng.random() < 0.5:
            new, how = round(a - fee, 2), "fee netted by scheme"
        else:
            how = "typo"
            choice = rng.integers(0, 3)
            if choice == 0:
                new = round(a * 10, 2)
            elif choice == 1:
                new = round(a + rng.integers(1, 50) + 0.37, 2)
            else:
                new = round(max(a - rng.integers(1, 9) * 0.1 - 0.01, 0.01), 2)
            if abs((a - new) - fee) < 0.006 or abs(a - new) < 0.006:
                new = round(a + 1.11, 2)
        scheme.at[i, "amount_gbp"] = new
        truth_breaks.append({"type": "R2", "payment_id": scheme.at[i, "payment_id"], "detail": how})
    for i in chosen["R6"]:
        scheme.at[i, "settled_at"] += DAY
        truth_breaks.append({"type": "R6", "payment_id": scheme.at[i, "payment_id"]})
    dups = scheme.iloc[chosen["R3"]].copy()
    dups["settled_at"] = dups["settled_at"] + rng.integers(0, 6 * HOUR, len(dups))
    truth_breaks += [{"type": "R3", "payment_id": p} for p in dups["payment_id"]]
    truth_breaks += [{"type": "R1", "payment_id": p} for p in scheme.iloc[chosen["R1"]]["payment_id"]]
    for i in chosen["R5"]:
        p = pid_of[s_idx[i]]
        truth_breaks.append({"type": "R5", "payment_id": p})
    r5_ids = set(pid_of[s_idx[chosen["R5"]]])
    m5 = ledger["payment_id"].isin(r5_ids) & (ledger["event"] == "settlement") & (ledger["account"] == "customer")
    deltas = rng.choice([0.01, 0.10, 1.00, 9.00, 10.00, 0.90, 100.0], m5.sum())
    ledger.loc[m5, "debit_gbp"] = np.round(ledger.loc[m5, "debit_gbp"].to_numpy() + deltas, 2)

    n4 = counts["R4"]
    orphan_rail = _pick(rng, {rc["scheme_code"]: 1 for rc in rails_cfg.values()}, n4)
    orphans = pd.DataFrame({
        "payment_id": [f"P8{x:06d}" for x in rng.choice(999999, n4, replace=False)],
        "rail": orphan_rail,
        "amount_gbp": np.round(80 * np.exp(rng.standard_normal(n4)), 2),
        "settled_at": rng.integers(start + DAY, end, n4),
    })
    scheme = scheme.drop(index=chosen["R1"])
    scheme = pd.concat([scheme, dups, orphans], ignore_index=True)
    scheme = scheme[scheme["settled_at"] <= as_of]
    scheme = scheme.sort_values(["settled_at", "payment_id", "amount_gbp"], kind="mergesort").reset_index(drop=True)
    scheme.insert(0, "scheme_ref", [f"S{i + 1:08d}" for i in range(len(scheme))])
    orphan_refs = scheme.loc[scheme["payment_id"].isin(set(orphans["payment_id"])), "scheme_ref"]
    truth_breaks += [{"type": "R4", "scheme_ref": s} for s in orphan_refs]

    ledger = ledger.sort_values(["posted_at", "payment_id"], kind="mergesort").reset_index(drop=True)
    ledger.insert(0, "entry_id", np.arange(1, len(ledger) + 1))

    fraud_truth = pd.DataFrame({"payment_id": pid_of, "is_fraud": is_fraud}).iloc[order].reset_index(drop=True)

    # ---------------- timestamps ----------------
    def to_dt(s):
        return pd.to_datetime(s, unit="s").astype("datetime64[us]")

    customers["opened_at"] = to_dt(customers["opened_at"])
    payments["created_at"] = to_dt(payments["created_at"])
    events["ts"] = to_dt(events["ts"])
    ledger["posted_at"] = to_dt(ledger["posted_at"])
    scheme["settled_at"] = to_dt(scheme["settled_at"])

    flat = cfgmod.flatten({k: v for k, v in cfg.items() if not k.startswith("_") and k != "notes"})
    notes = cfg.get("notes", {})
    config_assumptions = pd.DataFrame({
        "name": list(flat),
        "value": [json.dumps(v) if isinstance(v, (list, dict)) else str(v) for v in flat.values()],
        "note": [notes.get(k, "Assumption, not a real industry figure") for k in flat],
    })

    # ---------------- answer key ----------------
    curve = fraud_cost_curve(score, is_fraud, amount, cfg["risk"])
    costs = np.array([c for _, c in curve])
    k_opt = int(np.argmin(costs))
    current_k = int(round(cfg["risk"]["current_threshold"] / cfg["risk"]["grid_step"]))
    truth = {
        "seed": seed,
        "n_customers": n_customers,
        "n_payments": int(n),
        "recon_as_of": str(pd.to_datetime(as_of, unit="s")),
        "planted_problems": cfg["planted"],
        "configured_rates": {r: {"auth_rate": rc["auth_rate"], "failure_rate": rc["failure_rate"]} for r, rc in rails_cfg.items()},
        "breaks": truth_breaks,
        "break_counts": {k: sum(b["type"] == k for b in truth_breaks) for k in rcfg["break_mix"]},
        "fraud_optimum": {
            "threshold": curve[k_opt][0],
            "cost_gbp": round(float(costs[k_opt]), 2),
            "current_threshold": cfg["risk"]["current_threshold"],
            "current_cost_gbp": round(float(costs[current_k]), 2),
            "mode": cfg["risk"]["default_mode"],
        },
        "fraud_count": int(is_fraud.sum()),
    }
    tables = {
        "customers": customers,
        "payments": payments,
        "payment_events": events,
        "ledger_entries": ledger,
        "scheme_file": scheme,
        "fraud_truth": fraud_truth,
        "config_assumptions": config_assumptions,
    }
    return tables, truth


def write(tables, truth, db_path, truth_path):
    db_path, truth_path = Path(db_path), Path(truth_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    for p in (db_path, Path(str(db_path) + ".wal")):
        if p.exists():
            p.unlink()
    con = duckdb.connect(str(db_path))
    for name, df in tables.items():
        con.register("_df", df)
        con.execute(f"CREATE TABLE {name} AS SELECT * FROM _df")
        con.unregister("_df")
    con.close()
    with open(truth_path, "w", encoding="utf-8") as f:
        json.dump(truth, f, indent=1, default=str)


def table_fingerprint(df):
    return hashlib.sha256(pd.util.hash_pandas_object(df, index=False).values.tobytes()).hexdigest()


def build(seed=None, n_customers=None, n_payments=None, db_path=None, truth_path=None, prepare=True):
    cfg = dict(cfgmod.assumptions())
    cfg["_reason_codes"] = cfgmod.reason_codes()
    tables, truth = generate(cfg, seed, n_customers, n_payments)
    db_path = db_path or cfgmod.DB_PATH
    truth_path = truth_path or cfgmod.TRUTH_PATH
    write(tables, truth, db_path, truth_path)
    if prepare:
        from core import db
        con = duckdb.connect(str(db_path))
        db.prepare(con)
        con.close()
    return tables, truth


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seed", type=int)
    ap.add_argument("--customers", type=int)
    ap.add_argument("--payments", type=int)
    ap.add_argument("--out", help="DuckDB path (default data/control_tower.duckdb)")
    args = ap.parse_args()
    t0 = time.time()
    out = Path(args.out) if args.out else None
    tables, truth = build(args.seed, args.customers, args.payments, out,
                          out.with_name("truth.json") if out else None)
    print({k: len(v) for k, v in tables.items()})
    print("fraud optimum:", truth["fraud_optimum"])
    print(f"generated in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
