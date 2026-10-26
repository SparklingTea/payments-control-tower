"""Insight notes and stories. Evidence numbers come from query results; the LLM only rewords.

If ANTHROPIC_API_KEY (or another Anthropic credential) is available, notes can be rewritten in plain English
by Claude. Any rewrite that contains a number not present in the evidence is rejected and the template is kept.
"""
import os
import re

from core import analytics, config

NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def numbers_in(text):
    return {n.replace(",", "") for n in NUM_RE.findall(text)}


def allowed_numbers(evidence):
    allowed = set()
    for v in evidence.values():
        allowed |= numbers_in(str(v))
    return allowed


def check_numbers(text, evidence, extra_allowed=()):
    """Return the numbers in text that are not backed by the evidence."""
    allowed = allowed_numbers(evidence) | {str(x) for x in extra_allowed}
    return sorted(numbers_in(text) - allowed)


def llm_rewrite(text, evidence, client=None, model=None):
    """Ask Claude to reword a note. Returns (text, source) where source is 'llm' or 'template'."""
    if client is None:
        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            return text, "template"
        try:
            import anthropic
            client = anthropic.Anthropic()
        except Exception:
            return text, "template"
    model = model or os.environ.get("LLM_MODEL", "claude-opus-5-5")
    prompt = (
        "Rewrite this payments analytics note in short, plain English for a payments product squad. "
        "Keep every number exactly as written, do not add any new number, date or claim, "
        "and keep it under 90 words. Return only the rewritten note.\n\n" + text)
    try:
        resp = client.messages.create(model=model, max_tokens=1024,
                                      output_config={"effort": "low"},
                                      messages=[{"role": "user", "content": prompt}])
        if resp.stop_reason == "refusal":
            return text, "template"
        out = "".join(b.text for b in resp.content if b.type == "text").strip()
    except Exception:
        return text, "template"
    if not out or check_numbers(out, evidence, numbers_in(text)):
        return text, "template"
    return out, "llm"


def _pct(x, dp=1):
    return f"{x * 100:.{dp}f}%"


def build_insights(con, filters=None):
    """Four insight notes, one per performance finding, each with evidence, a story and acceptance criteria."""
    a = config.assumptions()
    notes = []

    _, hourly, inc = analytics.detect_incidents(con, filters)
    if not inc.empty:
        top = inc.sort_values("technical_declines", ascending=False).iloc[0]
        ev = {"window": f"{top['start']:%Y-%m-%d %H:%M} to {top['end']:%H:%M}",
              "technical_rate": _pct(top["technical_rate"]),
              "baseline_rate": _pct(top["baseline_rate"], 2),
              "technical_declines": f"{top['technical_declines']:,}",
              "initiated": f"{top['initiated']:,}"}
        notes.append({
            "id": "incident", "title": "Technical-timeout incident",
            "evidence": ev,
            "note": (f"From {ev['window']} technical declines hit {ev['technical_rate']} of payments "
                     f"against a baseline of {ev['baseline_rate']} ({ev['technical_declines']} declines out of "
                     f"{ev['initiated']} payments). This is an incident, not a trend, and is excluded from the trend baseline."),
            "story": "As a customer paying during an outage, I want my payment to go through or fail fast with a clear message so that I am not left unsure whether I paid.",
            "acceptance": ["Given technical declines exceed the incident threshold for an hour, when the hour closes, then an alert is raised to the on-call payments engineer.",
                           "Given an incident window, when the trend dashboard is built, then the window is excluded from the baseline and shown separately."],
            "metric": "decline_rate (technical group) and time to detect an incident",
        })

    _, cats = analytics.detect_hotspots(con, filters)
    hot = cats[cats["hotspot"]]
    if not hot.empty:
        h = hot.iloc[0]
        ev = {"category": h["category"], "decline_rate": _pct(h["decline_rate"]), "rest_rate": _pct(h["rest_rate"]),
              "ratio": f"{h['ratio']:.1f}", "initiated": f"{int(h['initiated']):,}",
              "rule_declines": f"{int(h['category_rule_declines']):,}"}
        notes.append({
            "id": "hotspot", "title": f"False-decline hotspot: {h['category']}",
            "evidence": ev,
            "note": (f"Card payments in {ev['category']} are declined at {ev['decline_rate']} against {ev['rest_rate']} "
                     f"for every other category, {ev['ratio']} times higher across {ev['initiated']} payments. "
                     f"{ev['rule_declines']} of those declines carry the category-rule code, which points to an over-strict rule rather than customer behaviour."),
            "story": f"As a card customer, I want fewer wrongful declines in {h['category']} so that I complete my purchase.",
            "acceptance": [f"Given a genuine card payment in {h['category']}, when it is scored, then it is not blocked by the category rule alone.",
                           f"Given the rule is relaxed, when four weeks have passed, then the {h['category']} decline rate is within 1.5 times the rest and fraud loss in the category has not risen."],
            "metric": "auth_rate for the category against the rest",
        })

    sdf_sql, sdf, clusters = analytics.detect_settle_clusters(con, filters)
    p3 = a["planted"]["P3"]
    bim = clusters[clusters["bimodal"]] if not clusters.empty else clusters
    if not bim.empty:
        b = bim.iloc[0]
        ce = analytics.cutoff_evidence(sdf, b["rail"], p3["cutoff_hour"])
        ev = {"rail": b["rail"], "low": f"{b['cluster_low_hours']:.0f}", "high": f"{b['cluster_high_hours']:.0f}",
              "share_high": _pct(b["share_in_high_cluster"], 0), "cutoff": f"{p3['cutoff_hour']}:00",
              "after_median": f"{ce['after_cutoff_median']:.0f}", "before_median": f"{ce['before_cutoff_median']:.0f}"}
        notes.append({
            "id": "bimodal", "title": f"Two settlement clusters on {b['rail']}",
            "evidence": ev,
            "note": (f"{ev['rail']} settlement time has two clusters, around {ev['low']} and {ev['high']} hours, with "
                     f"{ev['share_high']} of payments in the slower one. Payments created after {ev['cutoff']} settle in a median "
                     f"{ev['after_median']} hours against {ev['before_median']} hours before it, so the cut-off adds a day."),
            "story": "As a business paying suppliers, I want to know before I submit whether my payment misses today's cut-off so that I can plan when funds arrive.",
            "acceptance": ["Given a Bacs payment submitted after the cut-off, when the customer confirms it, then the expected settlement date shown is one day later.",
                           "Given the cut-off message ships, when a month has passed, then support contacts about late Bacs payments fall."],
            "metric": "time_to_settle_p50 and time_to_settle_p95 by rail",
        })

    _, _, _, drift = analytics.detect_cost_drift(con, filters)
    flagged = drift[drift["flagged"]] if not drift.empty else drift
    if not flagged.empty:
        d = flagged.iloc[0]
        ev = {"segment": d["segment"], "rail": d["rail"], "baseline": f"£{d['baseline_cps']:.3f}",
              "recent": f"£{d['recent_cps']:.3f}", "change": _pct(d["change"]),
              "blended_change": _pct(d["blended_change"])}
        notes.append({
            "id": "cost_drift", "title": f"Cost drift: {d['segment']} on {d['rail']}",
            "evidence": ev,
            "note": (f"Cost per settled payment for {ev['segment']} customers on {ev['rail']} rose from {ev['baseline']} "
                     f"to {ev['recent']}, up {ev['change']}, while the blended average moved only {ev['blended_change']}. "
                     f"The drift is hidden inside the blended figure."),
            "story": f"As a finance partner, I want cost per payment tracked by segment and rail so that a drift like {d['segment']} {d['rail']} is caught before it shows in the blended average.",
            "acceptance": ["Given monthly cost data, when any segment and rail moves more than the drift threshold against its baseline, then it appears in the flagged list.",
                           "Given a flagged drift, when the owner reviews it, then the cause (pricing change, routing, scheme fee) is recorded within two weeks."],
            "metric": "cost_per_settled by rail and segment",
        })
    return notes
