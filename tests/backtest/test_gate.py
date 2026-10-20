from datetime import UTC, datetime, timedelta

import pandas as pd

from invest_agent.backtest.engine import BacktestResult, Trade
from invest_agent.backtest.gate import gate

START = datetime(2026, 1, 1, tzinfo=UTC)
END = START + timedelta(days=300)


def result(pnls_by_span: list[list[float]]) -> BacktestResult:
    trades = []
    for span, pnls in enumerate(pnls_by_span):
        for k, pnl in enumerate(pnls):
            exit_ = START + timedelta(days=100 * span + 1 + k * 0.001)
            trades.append(Trade("BTCUSDT", "fake", exit_, exit_, exit_, "target", pnl, 0.0))
    eq = pd.Series([100.0], index=pd.DatetimeIndex([START]))
    return BacktestResult(trades, eq, START, END)


def test_gate_passes_good_result():
    v = gate(result([[1.0] * 10, [1.0] * 10, [1.0] * 10]))
    assert v.passed and v.reasons == []


def test_gate_requires_30_trades():
    v = gate(result([[1.0] * 9, [1.0] * 10, [1.0] * 10]))
    assert not v.passed
    assert any("29" in r for r in v.reasons)


def test_gate_requires_two_of_three_subperiods():
    bad = gate(result([[5.0] * 10, [-1.0] * 10, [-1.0] * 10]))  # net>0, 30 сделок, но 1 из 3
    assert not bad.passed
    assert any("под-периодов" in r for r in bad.reasons)
    ok = gate(result([[5.0] * 10, [-1.0] * 10, [1.0] * 10]))  # 2 из 3 неотрицательны
    assert ok.passed


def test_gate_requires_positive_net():
    v = gate(result([[1.0] * 10, [-1.0] * 10, [0.0] * 10]))  # net == 0
    assert not v.passed
