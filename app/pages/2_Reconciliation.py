import common
import plotly.express as px
import streamlit as st

from core import config, ledger, notes, reconcile

common.page("Reconciliation", "⚖️")
c = common.con()
rc = config.assumptions()["reconciliation"]
st.title("Module 2 · Ledger and reconciliation")
st.markdown(f"Ledger settlements compared with the external scheme file, as of **{reconcile.default_as_of():%d %b %Y}**. "
            f"Passes run in order and a payment leaves as soon as it matches. Timing differences within "
            f"**{rc['tolerance_days']} day** are matched, not breaks.")
common.assumption_badge()

res = c.execute("SELECT * FROM recon_results").df()
brk = c.execute("SELECT * FROM recon_exceptions").df()
s = reconcile.summary(res, brk)

k = st.columns(5)
k[0].metric("Match rate", f"{s['match_rate']:.2%}", help="Exact + timing matches / settled payments compared")
k[1].metric("Open breaks", f"{s['breaks']:,}")
k[2].metric("Value at risk", f"£{s['value_at_risk']:,.0f}")
k[3].metric("Timing differences (not breaks)", f"{s['timing_differences']:,}")
k[4].metric("Pending (inside window)", f"{s['pending']:,}")

c1, c2 = st.columns(2)
bt = s["by_type"].copy()
bt["break_code"] = bt["break_type"].map(reconcile.BREAK_CODE)
c1.plotly_chart(px.bar(bt, x="breaks", y="break_type", orientation="h", color="value_at_risk",
                       hover_data=["break_rate", "break_code"], title="Breaks by type"), use_container_width=True)
order = [b[2] for b in rc["age_buckets"]]
ba = s["by_age"].set_index("age_bucket").reindex(order).fillna(0).reset_index()
c2.plotly_chart(px.bar(ba, x="age_bucket", y="breaks", text="breaks", title="Breaks by age bucket"), use_container_width=True)

st.subheader("Exceptions queue")
f = st.columns(4)
types = f[0].multiselect("Break type", sorted(brk["break_type"].unique()))
owners = f[1].multiselect("Owner", sorted(brk["suggested_owner"].unique()))
ages = f[2].multiselect("Age bucket", order)
min_amt = f[3].number_input("Min value at risk (£)", 0.0, value=0.0, step=10.0)
sort = st.radio("Sort by", ["Value at risk, then age", "Age, then value at risk"], horizontal=True)
q = brk.copy()
if types:
    q = q[q["break_type"].isin(types)]
if owners:
    q = q[q["suggested_owner"].isin(owners)]
if ages:
    q = q[q["age_bucket"].isin(ages)]
q = q[q["amount_at_risk"] >= min_amt]
q = q.sort_values(["amount_at_risk", "age_days"] if sort.startswith("Value") else ["age_days", "amount_at_risk"], ascending=False)
cols = ["break_id", "break_type", "break_code", "amount_at_risk", "age_days", "age_bucket", "suggested_owner",
        "payment_id", "scheme_ref", "rail", "pass_failed", "values_compared", "suggested_cause"]
st.caption(f"{len(q):,} breaks · £{q['amount_at_risk'].sum():,.2f} at risk")
event = st.dataframe(q[cols], hide_index=True, use_container_width=True, on_select="rerun", selection_mode="single-row",
                     column_config={"amount_at_risk": st.column_config.NumberColumn("Value at risk", format="£%.2f")})
st.download_button("Download queue (CSV)", q[cols].to_csv(index=False), "exceptions_queue.csv", "text/csv")

rows = event.selection.rows if event and event.selection else []
if rows:
    r = q.iloc[rows[0]]
    st.markdown(f"#### {r['break_id']} · {r['break_type']}")
    st.write(f"**Pass failed:** {r['pass_failed']}  \n**Values compared:** {r['values_compared']}  \n"
             f"**Suggested owner:** {r['suggested_owner']}  \n**Rule-based cause:** {r['suggested_cause']}")
    if st.button("Rewrite as a plain-English note (LLM)"):
        ev = {"cause": r["suggested_cause"], "values": r["values_compared"], "age": f"{r['age_days']} days",
              "id": r["break_id"]}
        text, src = notes.llm_rewrite(f"{r['suggested_cause']} Open for {r['age_days']} days.", ev)
        st.info(text)
        st.caption("Rewritten by Claude; numbers checked against the break." if src == "llm"
                   else "No API key or rewrite rejected, showing the rule-based text. The LLM never decides the classification.")
    if r["payment_id"] and str(r["payment_id"]) != "None":
        st.dataframe(ledger.postings_for(c, r["payment_id"]), hide_index=True)
        st.dataframe(c.execute("SELECT * FROM scheme_file WHERE payment_id = ?", [r["payment_id"]]).df(), hide_index=True)
else:
    st.caption("Select a row to see why it was classified that way.")

with st.expander("Matching passes and SQL"):
    st.markdown("""
1. **Exact** – same reference and amount, settled the same day → matched
2. **Timing** – same reference and amount, dates within tolerance → matched, timing difference (not a break)
3. **Amount after fees** – difference equals the rail's configured fee → break: fee netted by scheme
4. **Unexplained amount** – any other difference → break
- No scheme record after the window → **missing**; scheme record with no payment → **orphan**; two records → **duplicate**
- Separately, any payment whose ledger debits and credits differ → **unbalanced ledger**
""")
    st.code(reconcile.reconcile_sql().strip(), language="sql")

st.subheader("Ledger")
l1, l2 = st.columns(2)
l1.write("Posting rules (every payment produces balanced entries)")
l1.dataframe(ledger.POSTING_RULES, hide_index=True)
l2.write("Trial balance by account")
l2.dataframe(ledger.trial_balance(c), hide_index=True)
ub = ledger.unbalanced(c)
st.write(f"**{len(ub)}** payments have debits ≠ credits.")
st.dataframe(ub.head(50), hide_index=True)
common.show_sql(ledger.UNBALANCED_SQL, "Show balance-check SQL")
