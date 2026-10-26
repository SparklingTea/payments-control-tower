import common
import streamlit as st

from core import metrics, reconcile, risk

common.page("Payment Performance & Risk Control Tower", "🛰️")
c = common.con()

st.title("Payment Performance & Risk Control Tower")
st.markdown(
    "Follows money through its lifecycle, finds where records disagree, measures where payments fail or cost "
    "too much, and ends on a fraud-threshold decision with the economics attached.")
common.assumption_badge()

n_pay, n_cust, lo, hi = c.execute(
    "SELECT count(*), count(DISTINCT customer_id), min(created_at)::DATE, max(created_at)::DATE FROM payments").fetchone()
k = st.columns(4)
k[0].metric("Payments", f"{n_pay:,}")
k[1].metric("Customers", f"{n_cust:,}")
k[2].metric("Period", f"{lo:%d %b} – {hi:%d %b %Y}")
k[3].metric("Rails", "Card · FPS · Bacs · DD")

st.subheader("Headlines")
_, auth = metrics.value(c, "auth_rate")
_, succ = metrics.value(c, "success_rate")
res = c.execute("SELECT * FROM recon_results").df()
brk = c.execute("SELECT * FROM recon_exceptions").df()
s = reconcile.summary(res, brk)
_, _, curve, opt = risk.analyse(c)
cur = risk.at_threshold(curve, risk.risk_cfg()["current_threshold"])
h = st.columns(4)
h[0].metric("Authorisation rate", f"{auth:.2%}")
h[1].metric("End-to-end success", f"{succ:.2%}")
h[2].metric("Reconciliation match rate", f"{s['match_rate']:.2%}", f"{s['breaks']:,} open breaks", delta_color="off")
h[3].metric("Recommended fraud threshold", f"{opt['threshold']:.3f}",
            f"saves £{cur['total_cost'] - opt['total_cost']:,.0f} vs {cur['threshold']:.2f}")

st.subheader("Modules")
m = st.columns(4)
m[0].markdown("**1 · Lifecycle**  \n7 states, retries, idempotency, reason-code groups, and a validator for every event log.")
m[0].page_link("pages/1_Lifecycle.py", label="Open lifecycle", icon="🔁")
m[1].markdown("**2 · Reconciliation**  \nDouble-entry ledger against the scheme file: matching passes, classified breaks, exceptions queue.")
m[1].page_link("pages/2_Reconciliation.py", label="Open reconciliation", icon="⚖️")
m[2].markdown("**3 · Analytics**  \nMetric registry with visible SQL; incidents, hotspots, settlement clusters and cost drift turned into stories.")
m[2].page_link("pages/3_Analytics.py", label="Open analytics", icon="📈")
m[3].markdown("**4 · Risk trade-off**  \nCost of a fraud threshold: missed fraud vs blocked customers, sensitivity, rules and a recommendation.")
m[3].page_link("pages/4_Risk.py", label="Open risk", icon="🎯")

st.subheader("Tailor the first minute")
roles = {
    "Payments product analyst or owner": ("Analytics findings turned into stories", "the risk recommendation as a decision", "pages/3_Analytics.py"),
    "Data or BI analyst": ("the metric registry and visible SQL", "the planted incident separated from the trend", "pages/3_Analytics.py"),
    "Fraud or risk analyst": ("the cost curve and sensitivity", "the caveats on labels and cost assumptions", "pages/4_Risk.py"),
    "Payments operations or finance ops": ("the exceptions queue and break classification", "why timing differences are not breaks", "pages/2_Reconciliation.py"),
    "Payments or solutions engineer": ("the lifecycle states and idempotency test", "how ledger balance checks catch errors", "pages/1_Lifecycle.py"),
}
role = st.selectbox("Role in front of you", list(roles))
lead, then, link = roles[role]
st.info(f"Lead with **{lead}**, then show **{then}**.")
st.page_link(link, label="Go there", icon="➡️")
