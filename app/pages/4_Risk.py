import common
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from core import risk

common.page("Risk trade-off", "🎯")
c = common.con()
r = risk.risk_cfg()
st.title("Module 4 · Fraud threshold trade-off")
st.markdown("Payments with a risk score at or above the threshold are declined (or sent to review). "
            "Where should the threshold sit?")
st.latex(r"C(t) = \sum_{i:\,s_i<t,\,y_i=1} a_i \;+\; c_d \cdot \#\{i: s_i \ge t,\, y_i=0\} \;+\; c_r \cdot \#\{i: s_i \ge t\}")

st.sidebar.header("Cost assumptions")
st.sidebar.caption("All inputs are assumptions, not real figures.")
mode = st.sidebar.radio("Mode", ["decline", "review"], index=0 if r["default_mode"] == "decline" else 1,
                        help="Decline-only: c_r = 0. Review: every flagged payment costs a manual review.")
c_d = st.sidebar.number_input("c_d · false-decline cost (£)", 0.0, 200.0, float(r["false_decline_cost_gbp"]), 1.0)
extra = st.sidebar.number_input("Chargeback fee added to fraud loss (£)", 0.0, 200.0, float(r["fraud_loss_extra_gbp"]), 1.0)
c_r = st.sidebar.number_input("c_r · review cost (£)", 0.0, 50.0, float(r["review_cost_gbp"]), 0.5, disabled=mode == "decline")
cur_t = st.sidebar.number_input("Current threshold", 0.0, 1.0, float(r["current_threshold"]), r["grid_step"], format="%.3f")
costs = risk.costs(mode, c_d, extra, 1.0, c_r)

sql, buckets, curve, opt = risk.analyse(c, costs)
cur = risk.at_threshold(curve, cur_t)
chosen_t = st.slider("Explore a threshold", 0.0, 1.0, float(opt["threshold"]), r["grid_step"], format="%.3f")
ch = risk.at_threshold(curve, chosen_t)

k = st.columns(4)
k[0].metric("Cost-minimising threshold", f"{opt['threshold']:.3f}")
k[1].metric("Expected cost at optimum", f"£{opt['total_cost']:,.0f}")
k[2].metric(f"Cost at current ({cur_t:.2f})", f"£{cur['total_cost']:,.0f}")
k[3].metric("Saving vs current", f"£{cur['total_cost'] - opt['total_cost']:,.0f}")

fig = go.Figure()
for col, name in (("total_cost", "Total cost"), ("fraud_loss", "Missed fraud loss"),
                  ("false_decline_cost", "False-decline cost"), ("review_cost", "Review cost")):
    if col != "review_cost" or mode == "review":
        fig.add_scatter(x=curve["threshold"], y=curve[col], name=name, line=dict(width=4 if col == "total_cost" else 1.5))
fig.add_vline(opt["threshold"], line_dash="dash", line_color="green", annotation_text=f"optimum {opt['threshold']:.3f}")
fig.add_vline(cur_t, line_dash="dot", line_color="red", annotation_text=f"current {cur_t:.2f}", annotation_position="bottom right")
fig.add_vline(chosen_t, line_color="grey", opacity=0.4)
fig.update_layout(title="Cost against threshold", xaxis_title="Threshold", yaxis_title="£ over six months", height=430,
                  yaxis_range=[0, 1.3 * max(curve["total_cost"].iloc[-1], cur["total_cost"])])
st.plotly_chart(fig, use_container_width=True)

a, b = st.columns(2)
with a:
    st.markdown(f"#### Confusion table at {chosen_t:.3f}")
    st.table({"": ["Fraud", "Genuine"],
              "Blocked (s ≥ t)": [f"{int(ch['fraud_caught']):,} caught", f"{int(ch['genuine_blocked']):,} blocked"],
              "Passed (s < t)": [f"{int(ch['fraud_missed']):,} missed", f"{int(ch['genuine_passed']):,} passed"]})
    st.write(f"Fraud loss **£{ch['fraud_loss']:,.0f}** · false-decline cost **£{ch['false_decline_cost']:,.0f}** · "
             f"genuine customers affected (friction) **{ch['friction']:.2%}** · total **£{ch['total_cost']:,.0f}**")
with b:
    pr = curve.melt(id_vars="threshold", value_vars=["precision", "recall", "friction"])
    fig2 = px.line(pr, x="threshold", y="value", color="variable", title="Precision, recall and friction against threshold")
    fig2.add_vline(chosen_t, line_color="grey", opacity=0.5)
    fig2.update_yaxes(tickformat=".0%")
    st.plotly_chart(fig2, use_container_width=True)

st.subheader("Sensitivity")
t_base, sens = risk.sensitivity(buckets, costs)
fig3 = px.bar(sens, x="optimal_threshold", y="scenario", orientation="h", color="beyond_margin",
              color_discrete_map={True: "#dc2626", False: "#16a34a"}, title=f"Optimal threshold under six cost scenarios (base {t_base:.3f})")
fig3.add_vline(t_base, line_dash="dash")
st.plotly_chart(fig3, use_container_width=True)
moved = sens["beyond_margin"].sum()
msg = f"{moved} of 6 scenarios move the optimum by more than the ±{r['sensitivity_margin']} margin."
if moved:
    st.warning(msg)
else:
    st.success(msg)
st.dataframe(sens.style.format({"optimal_threshold": "{:.3f}", "expected_cost": "£{:,.0f}", "shift": "{:+.3f}"}), hide_index=True)

st.subheader("Does one threshold fit all?")
seg = risk.segment_optimum(c, costs)
st.dataframe(seg.drop(columns="sql").style.format({"fraud_rate": "{:.2%}", "optimal_threshold": "{:.3f}", "expected_cost": "£{:,.0f}"}),
             hide_index=True)
gap = abs(seg["optimal_threshold"].iloc[0] - seg["optimal_threshold"].iloc[1])
st.caption(f"Gap between segment optima: {gap:.3f}. "
           + ("Small enough that one threshold fits both." if gap <= r["sensitivity_margin"] else "Large enough to consider segment-specific thresholds."))

st.subheader("Transparent rules")
rsql, rules = risk.rules_comparison(c, opt["threshold"], costs)
st.dataframe(rules.style.format({"catch_rate": "{:.1%}", "false_decline_rate": "{:.2%}", "precision": "{:.1%}",
                                 "net_saving_on_top_of_score": "£{:,.0f}"}), hide_index=True, use_container_width=True)
st.caption("Net saving on top of the score = fraud loss avoided on payments the rule catches that the score threshold misses, "
           "minus false-decline (and review) cost of the extra genuine payments it blocks. Positive means the rule earns its keep.")
common.show_sql(rsql, "Show rules SQL")

st.subheader("Recommendation")
text, saving = risk.recommendation(opt, cur, costs, t_base, sens)
with st.container(border=True):
    st.markdown(text)
    st.caption(f"{len(text.split())} words")

st.subheader("Honest caveats")
st.markdown("""
- **Labels are late and incomplete.** Real fraud is confirmed weeks later and some is never reported, so the curve here is optimistic.
- **Fraudsters adapt.** A threshold that is optimal today shifts as attackers learn it; monitor and re-fit.
- **Costs are assumptions.** The false-decline cost and chargeback fee are invented for a synthetic book. The result shows the *method*, not a real optimum.
""")
common.show_sql(sql, "Show score-bucket SQL")
