import common
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from core import analytics, config, metrics, notes

common.page("Analytics", "📈")
c = common.con()
flt = common.sidebar_filters(c)
st.title("Module 3 · Performance analytics")
st.markdown("Every tile is built from the metric registry (`config/metrics.yaml`) and shows the exact SQL it ran.")
common.assumption_badge()

# ---------- headline row ----------
st.subheader("Headline metrics")
tiles = metrics.headline(c, flt)
for row in range(0, len(tiles), 5):
    cols = st.columns(5)
    for col, t in zip(cols, tiles[row:row + 5]):
        with col:
            delta = None
            if t["wow"] is not None:
                delta = f"{t['wow'] * 100:+.2f}pp WoW" if t["format"] == "pct" else f"{t['wow'] * 100:+.1f}% WoW"
            st.metric(t["label"], t["display"], delta,
                      delta_color="normal" if t["higher_is_better"] else "inverse", help=t["definition"])
            ser = t["series"].dropna()
            if len(ser):
                fig = go.Figure(go.Scatter(x=ser["week"], y=ser["value"], mode="lines", line=dict(width=1.5)))
                fig.update_layout(height=60, margin=dict(l=0, r=0, t=0, b=0), xaxis_visible=False, yaxis_visible=False)
                st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False}, key=f"spark_{t['name']}")
            with st.popover("SQL"):
                st.code(t["sql"], language="sql")
                st.caption("Weekly series:")
                st.code(t["weekly_sql"], language="sql")
                if t["caveats"]:
                    st.caption(f"Caveat: {t['caveats']}")

tab1, tab2, tab3, tab4, tab5 = st.tabs(["Declines & incidents", "Category hotspots", "Time to settle", "Cost", "Insights & stories"])

# ---------- P1 ----------
with tab1:
    gsql = metrics.decline_by_group_sql(flt)
    g = c.execute(gsql).df()
    a, b = st.columns(2)
    a.plotly_chart(px.bar(g, x="reason_group", y="decline_rate", title="Decline rate by reason group",
                          text=g["decline_rate"].map(lambda v: f"{v:.2%}")).update_yaxes(tickformat=".1%"), use_container_width=True)
    with a:
        common.show_sql(gsql)
    hsql = analytics.decline_by_hour_group_sql(flt)
    hg = c.execute(hsql).df()
    b.plotly_chart(px.bar(hg, x="hour_of_day", y="decline_rate", color="reason_group",
                          title="Decline rate by hour of day and reason group").update_yaxes(tickformat=".1%"), use_container_width=True)
    with b:
        common.show_sql(hsql)

    isql, hourly, inc = analytics.detect_incidents(c, flt)
    st.markdown("#### Incident detection")
    st.caption(f"An hour is an incident when its technical declines sit more than {config.assumptions()['analytics']['incident_z']} "
               "binomial standard deviations above the period rate, with at least "
               f"{config.assumptions()['analytics']['incident_min_events']} declines.")
    if not hourly.empty:
        fig = px.line(hourly, x="hour", y="technical_rate", title="Technical decline rate by hour")
        fl = hourly[hourly["incident"]]
        fig.add_scatter(x=fl["hour"], y=fl["technical_rate"], mode="markers", marker=dict(color="red", size=10), name="Incident")
        fig.update_yaxes(tickformat=".0%")
        st.plotly_chart(fig, use_container_width=True)
    if inc.empty:
        st.info("No incidents in the selected filters.")
    else:
        st.error(f"{len(inc)} incident window(s) flagged and excluded from the trend baseline.")
        st.dataframe(inc.style.format({"technical_rate": "{:.1%}", "baseline_rate": "{:.2%}", "max_z": "{:.1f}"}), hide_index=True)
        tr = analytics.daily_trend(hourly)
        fig = px.line(tr, x="day", y=["with_incidents", "baseline_excl_incidents"], title="Daily decline rate: trend baseline excludes incident hours")
        fig.update_yaxes(tickformat=".1%")
        st.plotly_chart(fig, use_container_width=True)
    common.show_sql(isql, "Show incident SQL")

# ---------- P2 ----------
with tab2:
    csql, cats = analytics.detect_hotspots(c, flt)
    if cats.empty:
        st.info("No card payments in the selected filters.")
    else:
        cats["status"] = cats["hotspot"].map({True: "Hotspot", False: "Normal"})
        fig = px.bar(cats, x="category", y=["decline_rate", "rest_rate"], barmode="group",
                     title="Card decline rate: each category against all other categories")
        fig.update_yaxes(tickformat=".0%")
        st.plotly_chart(fig, use_container_width=True)
        hot = cats[cats["hotspot"]]
        for _, h in hot.iterrows():
            st.error(f"**{h['category']}** declines at {h['decline_rate']:.1%} vs {h['rest_rate']:.1%} for the rest "
                     f"({h['ratio']:.1f}×). {int(h['category_rule_declines']):,} declines carry code D57 "
                     "(merchant-category rule): a false-decline hotspot.")
        st.dataframe(cats.drop(columns="status").style.format({"decline_rate": "{:.2%}", "rest_rate": "{:.2%}", "ratio": "{:.2f}"}),
                     hide_index=True, use_container_width=True)
    common.show_sql(csql)

# ---------- P3 ----------
with tab3:
    ssql, sdf, clusters = analytics.detect_settle_clusters(c, flt)
    if sdf.empty:
        st.info("No settled payments in the selected filters.")
    else:
        rail = st.selectbox("Rail", sorted(sdf["rail"].unique()), index=sorted(sdf["rail"].unique()).index("bacs_credit")
                            if "bacs_credit" in set(sdf["rail"]) else 0)
        sub = sdf[sdf["rail"] == rail]
        st.plotly_chart(px.histogram(sub, x="settle_hours", nbins=120, title=f"Time to settle, {rail} (hours)"), use_container_width=True)
        tsql, tts = metrics.run(c, "time_to_settle_p50", flt, by=["rail"])
        _, tts95 = metrics.run(c, "time_to_settle_p95", flt, by=["rail"])
        tts = tts.rename(columns={"value": "median_hours"}).merge(tts95.rename(columns={"value": "p95_hours"}), on="rail")
        st.dataframe(tts, hide_index=True)
        st.dataframe(clusters.drop(columns="_labels"), hide_index=True, use_container_width=True)
        for _, r in clusters[clusters["bimodal"]].iterrows():
            p3 = config.assumptions()["planted"]["P3"]
            ce = analytics.cutoff_evidence(sdf, r["rail"], p3["cutoff_hour"])
            st.warning(f"**{r['rail']}** is bimodal: clusters at {r['cluster_low_hours']:.0f}h and {r['cluster_high_hours']:.0f}h. "
                       f"Created after {p3['cutoff_hour']}:00 → median {ce['after_cutoff_median']:.0f}h; before → {ce['before_cutoff_median']:.0f}h.")
        common.show_sql(ssql)
        common.show_sql(tsql, "Show time-to-settle registry SQL")

# ---------- P4 ----------
with tab4:
    dsql, monthly, blended, drift = analytics.detect_cost_drift(c, flt)
    if monthly.empty:
        st.info("No data in the selected filters.")
    else:
        monthly["series"] = monthly["segment"] + " · " + monthly["rail"]
        fig = px.line(monthly, x="month", y="cost_per_settled", color="series", markers=True,
                      title="Cost per settled payment by rail and segment, with the blended average")
        fig.add_scatter(x=blended["month"], y=blended["cost_per_settled"], mode="lines+markers",
                        line=dict(color="black", width=4, dash="dash"), name="Blended average")
        st.plotly_chart(fig, use_container_width=True)
        flagged = drift[drift["flagged"]]
        if len(flagged):
            st.error("Flagged cost drift (recent months vs baseline months "
                     f"{config.assumptions()['analytics']['cost_baseline_months']}):")
            st.dataframe(flagged.style.format({"baseline_cps": "£{:.4f}", "recent_cps": "£{:.4f}", "change": "{:+.1%}",
                                               "blended_change": "{:+.1%}"}), hide_index=True)
        with st.expander("All segment × rail series"):
            st.dataframe(drift.style.format({"baseline_cps": "£{:.4f}", "recent_cps": "£{:.4f}", "change": "{:+.1%}",
                                             "blended_change": "{:+.1%}"}), hide_index=True)
        common.show_sql(dsql)
        common.show_sql(metrics.metric_sql("cost_per_settled", flt, by=["rail", "segment"]), "Show registry SQL by rail and segment")

# ---------- stories ----------
with tab5:
    ins = notes.build_insights(c, flt)
    use_llm = st.toggle("Reword notes with Claude (needs ANTHROPIC_API_KEY; numbers are verified against the evidence)")
    if not ins:
        st.info("No findings in the selected filters.")
    for n in ins:
        with st.container(border=True):
            st.markdown(f"### {n['title']}")
            text, src = (notes.llm_rewrite(n["note"], n["evidence"]) if use_llm else (n["note"], "template"))
            st.write(text)
            st.caption("Source: " + ("Claude rewrite, numbers verified" if src == "llm" else "template filled from query results"))
            st.markdown(f"**Story:** {n['story']}")
            st.markdown("**Acceptance criteria**\n" + "\n".join(f"- {x}" for x in n["acceptance"]))
            st.markdown(f"**Success metric:** {n['metric']}")
            with st.expander("Evidence (query results)"):
                st.json(n["evidence"])

with st.expander("Metric registry"):
    reg = pd.DataFrame([{"metric": k, **{f: v.get(f) for f in ("label", "definition", "from", "where", "expr", "caveats")}}
                        for k, v in metrics.registry().items()])
    st.dataframe(reg, hide_index=True, use_container_width=True)
