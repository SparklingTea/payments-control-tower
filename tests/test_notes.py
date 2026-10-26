from types import SimpleNamespace

import pytest

from core import notes


@pytest.fixture(scope="module")
def insights(con):
    return notes.build_insights(con)


def test_four_insights_with_stories(insights):
    assert [n["id"] for n in insights] == ["incident", "hotspot", "bimodal", "cost_drift"]
    for n in insights:
        assert n["story"].startswith("As a")
        assert all(a.startswith("Given") and "when" in a and "then" in a for a in n["acceptance"])
        assert n["metric"]


def test_every_number_in_a_note_comes_from_its_evidence(insights):
    for n in insights:
        assert notes.check_numbers(n["note"], n["evidence"]) == [], n["id"]


class FakeClient:
    def __init__(self, reply):
        self.messages = SimpleNamespace(create=lambda **kw: SimpleNamespace(
            stop_reason="end_turn", content=[SimpleNamespace(type="text", text=reply)]))


def test_llm_rewrite_with_invented_number_is_rejected(insights):
    n = insights[0]
    text, src = notes.llm_rewrite(n["note"], n["evidence"], client=FakeClient("Declines jumped to 99.9% in one hour."))
    assert src == "template" and text == n["note"]


def test_llm_rewrite_keeping_numbers_is_accepted(insights):
    n = insights[1]
    ev = n["evidence"]
    reply = f"{ev['category']} card payments fail {ev['decline_rate']} of the time versus {ev['rest_rate']} elsewhere."
    text, src = notes.llm_rewrite(n["note"], ev, client=FakeClient(reply))
    assert src == "llm" and text == reply


def test_no_credentials_falls_back_to_template(insights, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    n = insights[2]
    assert notes.llm_rewrite(n["note"], n["evidence"]) == (n["note"], "template")
