from types import SimpleNamespace

import anthropic
import httpx
from fakes import FakeClient, spend, usage

from invest_agent.llm.filter import FilterVerdict, filter_signal


def resp(parsed, stop="end_turn"):
    return SimpleNamespace(parsed_output=parsed, stop_reason=stop, usage=usage())


V = FilterVerdict(decision="approve", reason="ok", pros=["тренд"], cons=["BTC устал"])


def test_approve_parsed(ctx, budget, journal):
    client = FakeClient(parse=resp(V))
    r = filter_signal(ctx, client, budget)
    assert (r.status, r.verdict, r.error) == ("approved", V, None)
    kw = client.calls[0][1]
    assert kw["model"] == "claude-haiku-5-5"
    assert kw["max_tokens"] == 2048
    assert kw["output_format"] is FilterVerdict
    assert journal.conn.execute("SELECT purpose FROM api_usage").fetchall() == [("filter",)]


def test_reject_parsed(ctx, budget):
    v = FilterVerdict(decision="reject", reason="нет", pros=[], cons=["слабо"])
    assert filter_signal(ctx, FakeClient(parse=resp(v)), budget).status == "rejected"


def test_refusal_becomes_review(ctx, budget):
    r = filter_signal(ctx, FakeClient(parse=resp(None, "refusal")), budget)
    assert r.status == "review" and r.verdict is None and r.error == "refusal"


def test_unparsed_becomes_review(ctx, budget):
    assert filter_signal(ctx, FakeClient(parse=resp(None)), budget).status == "review"


def test_sdk_error_becomes_review(ctx, budget):
    err = anthropic.APIConnectionError(request=httpx.Request("POST", "https://x"))
    r = filter_signal(ctx, FakeClient(parse=err), budget)
    assert r.status == "review" and r.error


def test_budget_exhausted_skips_api(ctx, budget, journal):
    spend(journal, 10.0)
    client = FakeClient(parse=resp(V))
    r = filter_signal(ctx, client, budget)
    assert (r.status, r.error) == ("review", "budget")
    assert client.calls == []
