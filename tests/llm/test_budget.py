from datetime import UTC, datetime

import pytest
from fakes import NOW, spend, usage

from invest_agent.config import Settings
from invest_agent.llm.budget import Budget


def test_cost_calculation_haiku(budget, journal):
    budget.record("claude-haiku-5-5", "filter", usage(1_000_000, 1_000_000))
    assert budget.spent_this_month() == pytest.approx(0.60)
    row = journal.conn.execute("SELECT model, purpose, cost_usd FROM api_usage").fetchone()
    assert row == ("claude-haiku-5-5", "filter", pytest.approx(0.60))


def test_unknown_model_priced_as_sonnet(budget):
    budget.record("claude-mystery", "explain", usage(1_000_000, 1_000_000))
    assert budget.spent_this_month() == pytest.approx(12.0)


def test_record_does_not_commit(budget, journal):
    budget.record("claude-haiku-5-5", "filter", usage())
    assert journal.conn.in_transaction
    journal.conn.rollback()
    assert budget.spent_this_month() == 0


def test_spent_counts_only_current_utc_month(budget, journal):
    spend(journal, 1.0)
    spend(journal, 5.0, datetime(2026, 9, 30, 23, 59, tzinfo=UTC))
    spend(journal, 7.0, datetime(2026, 11, 1, 0, 0, tzinfo=UTC))
    assert budget.spent_this_month() == pytest.approx(1.0)


def test_model_for_tiers(journal):
    b = Budget(journal, Settings(claude_monthly_budget_usd=10), NOW)
    assert b.model_for("filter") == "claude-haiku-5-5"
    assert b.model_for("summary") == "claude-haiku-5-5"
    assert b.model_for("explain") == "claude-sonnet-5-5"
    spend(journal, 8.0)
    assert b.model_for("explain") == "claude-haiku-5-5"
    assert b.model_for("filter") == "claude-haiku-5-5"
    spend(journal, 2.0)
    assert all(b.model_for(p) is None for p in ("filter", "explain", "summary"))


def test_threshold_event_once_per_month(journal):
    b = Budget(journal, Settings(claude_monthly_budget_usd=10), NOW)
    assert b.threshold_events() == []
    spend(journal, 8.0)
    assert b.threshold_events() == ["budget_80"]
    assert b.threshold_events() == []
    spend(journal, 2.5)
    assert b.threshold_events() == ["budget_100"]
    assert b.threshold_events() == []
    # перезапуск бота: новый объект, то же состояние в БД
    assert Budget(journal, Settings(claude_monthly_budget_usd=10), NOW).threshold_events() == []
    # новый месяц — снова можно
    nxt = datetime(2026, 11, 2, tzinfo=UTC)
    spend(journal, 9.0, nxt)
    assert Budget(journal, Settings(claude_monthly_budget_usd=10), nxt).threshold_events() == [
        "budget_80"
    ]
