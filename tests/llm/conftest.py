import sqlite3

import pytest
from fakes import NOW

from invest_agent.config import Settings
from invest_agent.journal import Journal, migrate
from invest_agent.llm.budget import Budget
from invest_agent.llm.filter import FilterContext
from invest_agent.models import SignalCandidate


@pytest.fixture
def journal() -> Journal:
    conn = sqlite3.connect(":memory:")
    migrate(conn)
    return Journal(conn)


@pytest.fixture
def budget(journal) -> Budget:
    return Budget(journal, Settings(claude_monthly_budget_usd=10), NOW)


@pytest.fixture
def ctx() -> FilterContext:
    sig = SignalCandidate(
        symbol="BTCUSDT", strategy="trend", created_at=NOW, entry_low=99.0, entry_high=100.0,
        stop=95.0, target=110.0, features={"rsi": 55.0},
    )  # fmt: skip
    return FilterContext(sig, {"rsi": 55.0}, "up", ["ETHUSDT"], ["target", "stop"])
