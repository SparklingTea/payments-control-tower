import common
import duckdb
import plotly.express as px
import streamlit as st

from core import ledger, lifecycle

common.page("Lifecycle", "🔁")
c = common.con()
st.title("Module 1 · Payment lifecycle")
st.markdown("How a payment moves from request to final outcome. A settled payment can still be returned or "
            "disputed; declines and failures never revive the original payment, a retry is always a new one.")

left, right = st.columns([3, 2])
with left:
    st.graphviz_chart("""
digraph {
  rankdir=LR; node [shape=box, style="rounded,filled", fillcolor="#eef2ff", fontname="Helvetica"];
  initiated -> authorised; initiated -> declined;
  authorised -> settled; authorised -> failed;
  settled -> returned; settled -> disputed;
  declined -> retry [style=dashed, label="customer retry\\n(new payment, retry_of)"];
  retry [label="new payment\\n(initiated)", fillcolor="#fff7ed"];
  declined [fillcolor="#fee2e2"]; failed [fillcolor="#fee2e2"];
  settled [fillcolor="#dcfce7"]; returned [fillcolor="#fef9c3"]; disputed [fillcolor="#fef9c3"];
}""")
with right:
    sc = c.execute(lifecycle.state_counts_sql()).df()
    st.plotly_chart(px.bar(sc, x="payments", y="final_status", orientation="h", title="Final state of every payment"),
                    use_container_width=True)
    common.show_sql(lifecycle.state_counts_sql())

st.subheader("Configured vs simulated rates")
cv = c.execute(lifecycle.configured_vs_actual_sql()).df()
cv["auth_gap_pp"] = (cv["actual_auth_rate"] - cv["configured_auth_rate"]) * 100
cv["success_gap_pp"] = (cv["actual_success_rate"] - cv["configured_success_rate"]) * 100
st.dataframe(cv.style.format({col: "{:.2%}" for col in cv.columns if "rate" in col} | {"auth_gap_pp": "{:+.2f}", "success_gap_pp": "{:+.2f}"}),
             hide_index=True, use_container_width=True)
ok = (cv["auth_gap_pp"].abs() < 1).all() and (cv["success_gap_pp"].abs() < 1).all()
if ok:
    st.success("All rails within 1 percentage point of config.")
else:
    st.error("A rail is more than 1pp from config.")
st.caption("Configured rates are assumptions. Planted effects (the category rule, the timeout incident, score-based "
           "declines) are calibrated so each rail still lands on its configured authorisation rate.")
common.show_sql(lifecycle.configured_vs_actual_sql())

st.subheader("Event-log validator")
v = lifecycle.validate(c)
a, b, d = st.columns(3)
a.metric("Invalid transitions", len(v["invalid_transitions"]))
b.metric("Final-state mismatches", len(v["final_state_mismatches"]))
d.metric("Valid transitions", len(lifecycle.VALID_TRANSITIONS))
st.write("Valid transitions: " + ", ".join(f"`{x} → {y}`" for x, y in sorted(lifecycle.VALID_TRANSITIONS)))
if st.button("Inject 3 invalid transitions and re-validate"):
    tmp = duckdb.connect()
    events = c.execute("SELECT * FROM payment_events").df()
    tmp.execute("CREATE TABLE ev AS SELECT * FROM events")
    inj = lifecycle.inject_invalid_transitions(tmp, 3, source="ev")
    caught = tmp.execute(lifecycle.invalid_transitions_sql("events_injected")).df()
    st.write("Injected:")
    st.dataframe(inj, hide_index=True)
    st.write(f"Validator caught **{len(caught)}**:")
    st.dataframe(caught, hide_index=True)
    tmp.close()
common.show_sql(lifecycle.invalid_transitions_sql(), "Show validator SQL")

st.subheader("Reason codes")
cov = lifecycle.reason_code_coverage(c)
unm = cov[cov["reason_group"] == "UNMAPPED"]
g = cov[cov["reason_group"] != "UNMAPPED"].groupby(["reason_group", "stage"], as_index=False)["events"].sum()
st.plotly_chart(px.bar(g, x="reason_group", y="events", color="stage", title="Declines, failures, returns and disputes by reason group"),
                use_container_width=True)
if len(unm):
    st.warning(f"{len(unm)} reason code(s) are not in `config/reason_codes.yaml` and are listed here, not hidden:")
    st.dataframe(unm, hide_index=True)
with st.expander("All codes and their mapping"):
    st.dataframe(cov, hide_index=True, use_container_width=True)

st.subheader("Idempotency")
st.markdown("A repeated request with the same idempotency key returns the original payment and never creates a second one.")
if st.button("Send 100 requests, each twice with the same key"):
    tmp = duckdb.connect()
    tmp.execute(lifecycle.PAYMENTS_DDL)
    gw = lifecycle.PaymentGateway(tmp)
    results = []
    for i in range(100):
        req = {"idempotency_key": f"demo-{i:03d}", "customer_id": "C000001", "rail": "faster_payments", "amount_gbp": 25.0 + i}
        results.append(gw.submit(req))
        results.append(gw.submit(req))
    n = tmp.execute("SELECT count(*) FROM payments").fetchone()[0]
    created = sum(r["created"] for r in results)
    st.success(f"200 requests → {created} payments created, {200 - created} returned the original. Rows in table: {n}.")
    tmp.close()

st.subheader("Payment explorer")
pid = st.text_input("Payment ID", value=c.execute("SELECT payment_id FROM payments WHERE final_status='returned' LIMIT 1").fetchone()[0])
if pid:
    p = c.execute("SELECT * FROM v_payments WHERE payment_id = ?", [pid]).df()
    if p.empty:
        st.info("No such payment.")
    else:
        st.dataframe(p[["payment_id", "customer_id", "segment", "rail", "amount_gbp", "category", "final_status",
                        "decline_reason", "reason_group", "retry_of", "idempotency_key"]], hide_index=True)
        e1, e2 = st.columns(2)
        e1.write("Event log")
        e1.dataframe(c.execute("SELECT ts, state, reason_code FROM payment_events WHERE payment_id = ? ORDER BY ts", [pid]).df(), hide_index=True)
        e2.write("Ledger postings")
        e2.dataframe(ledger.postings_for(c, pid)[["posted_at", "event", "account", "debit_gbp", "credit_gbp"]], hide_index=True)
        retry = c.execute("SELECT payment_id, final_status FROM payments WHERE retry_of = ?", [pid]).df()
        if len(retry):
            st.write("Retried as:", retry)
