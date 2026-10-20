import sqlite3
from datetime import UTC, datetime, timedelta

import pandas as pd

from invest_agent.backtest.engine import (
    BacktestResult,
    Period,
    run_backtest,
    split_periods,
    summarize,
)
from invest_agent.config import FEE, SLIPPAGE
from invest_agent.data.candles import CandleStore
from invest_agent.models import Candle, SignalCandidate, SymbolRules

H1 = timedelta(hours=1)
D1 = timedelta(days=1)
MS = timedelta(milliseconds=1)


def dt(*a: int) -> datetime:
    return datetime(*a, tzinfo=UTC)


def make_store() -> CandleStore:
    conn = sqlite3.connect(":memory:")
    CandleStore.ensure_schema(conn)
    return CandleStore(conn)


def flat(
    start: datetime, end: datetime, step: timedelta, price: float = 100.0, low: float | None = None
):
    out, t = [], start
    while t + step <= end:
        out.append(Candle(t, t + step - MS, price, price, low or price, price, 1.0, 1e6))
        t += step
    return out


def rules_for(*symbols: str) -> dict[str, SymbolRules]:
    return {s: SymbolRules(s, 0.01, 0.001, 5.0) for s in symbols}


class Fixed:
    """Стратегия-заглушка: всегда один и тот же сигнал по выбранным монетам."""

    name = "fake"

    def __init__(self, only: set[str] | None = None) -> None:
        self.only = only
        self.params: dict[str, float] = {}

    def version(self) -> str:
        return "fake-v1"

    def evaluate(self, symbol, h4, d1, now):
        if self.only is not None and symbol not in self.only:
            return None
        return SignalCandidate(symbol, self.name, now, 99.0, 100.0, 98.0, 104.0, {})


def test_engine_uses_same_execution_and_limits():
    start, end = dt(2026, 3, 1), dt(2026, 3, 4)
    symbols = ["BTCUSDT", "ETHUSDT", "AAAUSDT", "BBBUSDT", "CCCUSDT"]
    store = make_store()
    for s in symbols:
        store.upsert(s, "1d", flat(start - timedelta(days=400), end, D1))
        store.upsert(s, "1h", flat(start, end, H1))
    result = run_backtest([Fixed()], store, rules_for(*symbols), start, end, 10_000.0)

    assert len(result.trades) == 4  # пятый сигнал отклонён лимитом открытых позиций
    assert {t.status for t in result.trades} == {"end"}
    # qty = 25% счёта / 100 = 25; вход 100 * (1+slip), выход 100 * (1-slip), комиссия с обеих сторон
    entry, exit_ = 100 * (1 + SLIPPAGE), 100 * (1 - SLIPPAGE)
    expected = 25 * (exit_ - entry) - FEE * 25 * (entry + exit_)
    assert all(abs(t.pnl_usdt - expected) < 1e-9 for t in result.trades)
    assert len(result.equity) == 7  # 6 окон + конец периода
    assert abs(result.equity.iloc[-1] - (10_000 + 4 * expected)) < 1e-9


def test_engine_no_lookahead_universe():
    start, end = dt(2026, 1, 10), dt(2026, 3, 20)
    store = make_store()
    for s in ("BTCUSDT", "ETHUSDT", "OLDUSDT"):
        store.upsert(s, "1d", flat(dt(2024, 6, 1), end, D1))
    # NEW получает 365 дней истории только к 1 марта 2026
    store.upsert("NEWUSDT", "1d", flat(dt(2025, 2, 20), end, D1))
    store.upsert("NEWUSDT", "1h", flat(start, end, H1))
    rules = rules_for("BTCUSDT", "ETHUSDT", "OLDUSDT", "NEWUSDT")

    result = run_backtest([Fixed({"NEWUSDT"})], store, rules, start, end, 10_000.0)

    assert result.trades
    assert min(t.created_at for t in result.trades) >= dt(2026, 3, 1)


def test_engine_stops_opening_after_halt():
    start, end = dt(2026, 3, 1), dt(2026, 3, 31)
    symbols = ["BTCUSDT", "ETHUSDT", "AAAUSDT", "BBBUSDT"]
    store = make_store()
    for s in symbols:
        store.upsert(s, "1d", flat(start - timedelta(days=400), end, D1))
        # каждая свеча касается цены входа и стопа -> каждая сделка закрывается по стопу
        store.upsert(s, "1h", flat(start, end, H1, low=97.0))
    result = run_backtest([Fixed()], store, rules_for(*symbols), start, end, 10_000.0)

    assert result.max_drawdown() >= 0.15
    assert result.trades[-1].created_at < end - timedelta(days=15)
    assert {t.status for t in result.trades} == {"stop"}


def test_split_last_365_days_is_holdout():
    start, end = dt(2022, 1, 1), dt(2026, 1, 1)
    train, val, hold = split_periods(start, end)
    assert hold == Period(end - timedelta(days=365), end)
    assert val == Period(end - timedelta(days=730), end - timedelta(days=365))
    assert train == Period(start, end - timedelta(days=730))


def test_summarize_and_max_drawdown():
    idx = pd.DatetimeIndex([dt(2026, 1, 1), dt(2026, 1, 2), dt(2026, 1, 3)])
    eq = pd.Series([100.0, 120.0, 90.0], index=idx)
    r = BacktestResult([], eq, dt(2026, 1, 1), dt(2026, 1, 4))
    assert abs(r.max_drawdown() - 0.25) < 1e-12
    assert summarize(r)["trades"] == 0
