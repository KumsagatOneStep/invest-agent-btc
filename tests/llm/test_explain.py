from types import SimpleNamespace

from fakes import FakeClient, spend, usage

from invest_agent.llm.explain import explain_signal
from invest_agent.llm.filter import FilterVerdict

V = FilterVerdict(decision="approve", reason="ok", pros=["тренд вверх"], cons=["BTC устал"])


def text_resp(text, stop="end_turn"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)], stop_reason=stop, usage=usage()
    )


def test_explain_uses_strong_model_with_fallbacks(ctx, budget):
    client = FakeClient(create=text_resp("Почему: тренд. Против: BTC."))
    assert explain_signal(ctx, V, client, budget) == "Почему: тренд. Против: BTC."
    kw = client.calls[0][1]
    assert kw["model"] == "claude-sonnet-5-5"
    assert kw["max_tokens"] == 1024
    assert kw["betas"] == ["server-side-fallback-2026-07-01"]
    assert kw["fallbacks"] == "default"


def test_explain_uses_cheap_model_after_80_percent(ctx, budget, journal):
    spend(journal, 8.0)
    client = FakeClient(create=text_resp("ок"))
    explain_signal(ctx, V, client, budget)
    kw = client.calls[0][1]
    assert kw["model"] == "claude-haiku-5-5"
    assert "betas" not in kw and "fallbacks" not in kw


def test_explain_falls_back_to_verdict_text(ctx, budget):
    for create in (RuntimeError("boom"), text_resp("", "refusal")):
        out = explain_signal(ctx, V, FakeClient(create=create), budget)
        assert "тренд вверх" in out and "BTC устал" in out


def test_explain_budget_exhausted_skips_api(ctx, budget, journal):
    spend(journal, 10.0)
    client = FakeClient(create=text_resp("ок"))
    out = explain_signal(ctx, V, client, budget)
    assert client.calls == [] and "тренд вверх" in out
