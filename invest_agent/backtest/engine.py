import bisect
import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime, time, timedelta

import pandas as pd

from invest_agent.config import DEFAULT_WINDOWS_UTC, MAX_HOLD, SLIPPAGE
from invest_agent.data.candles import CandleStore, to_frame
from invest_agent.execution import net_pnl, next_window, simulate, unrealized_pnl
from invest_agent.models import Candle, Outcome, Rejection, SignalCandidate, SymbolRules
from invest_agent.risk import (
    DDState,
    OpenRisk,
    risk_pct_for,
    round_prices,
    size_position,
    step_drawdown,
)
from invest_agent.strategies.base import Strategy
from invest_agent.universe import select_universe

log = logging.getLogger(__name__)
EPOCH = datetime(2000, 1, 1, tzinfo=UTC)
H4_LOOKBACK = 300
D1_LOOKBACK = 300
SIM_SPAN = MAX_HOLD + timedelta(days=2)
HOLDOUT_DAYS = 365


@dataclass(frozen=True)
class Period:
    start: datetime
    end: datetime


@dataclass(frozen=True)
class Trade:
    symbol: str
    strategy: str
    created_at: datetime
    entry_time: datetime
    exit_time: datetime
    status: str
    pnl_usdt: float
    fees_usdt: float


@dataclass
class BacktestResult:
    trades: list[Trade]
    equity: pd.Series
    start: datetime
    end: datetime

    def net_pnl(self) -> float:
        return sum(t.pnl_usdt for t in self.trades)

    def max_drawdown(self) -> float:
        """Максимальная просадка от пика, доля (0.1 = 10%)."""
        if self.equity.empty:
            return 0.0
        peak = self.equity.cummax()
        return float(((peak - self.equity) / peak).max())


def split_periods(start: datetime, end: datetime) -> tuple[Period, Period, Period]:
    """(train, validation, holdout): holdout = последние 365 дней, validation = 365 до него."""
    hold_start = max(start, end - timedelta(days=HOLDOUT_DAYS))
    val_start = max(start, hold_start - timedelta(days=HOLDOUT_DAYS))
    return Period(start, val_start), Period(val_start, hold_start), Period(hold_start, end)


def btc_hold_pct(store: CandleStore, period: Period) -> float:
    candles = store.get("BTCUSDT", "1h", period.start, period.end)
    return 100 * (candles[-1].close / candles[0].open - 1) if candles else 0.0


def summarize(result: BacktestResult) -> dict[str, float]:
    n = len(result.trades)
    wins = sum(1 for t in result.trades if t.pnl_usdt > 0)
    return {
        "trades": n,
        "win_pct": 100 * wins / n if n else 0.0,
        "net_pnl": result.net_pnl(),
        "max_dd": result.max_drawdown(),
    }


@dataclass
class _Position:
    sig: SignalCandidate
    qty: float
    risk_usdt: float
    notional: float
    ends_at: datetime
    entry_time: datetime | None = None
    entry_price: float = 0.0
    trade: Trade | None = None


class _SymbolData:
    def __init__(self, store: CandleStore, symbol: str, start: datetime, end: datetime) -> None:
        self.h1: list[Candle] = store.get(symbol, "1h", start, end)
        self.h1_open = [c.open_time for c in self.h1]
        self.h1_close = [c.close_time for c in self.h1]
        self.h4 = to_frame(store.get(symbol, "4h", EPOCH, end))
        self.d1 = to_frame(store.get(symbol, "1d", EPOCH, end))
        if not self.h1 or self.h4.empty:
            log.warning("%s: во вселенной, но нет свечей 1h/4h — монета не торгуется", symbol)

    @staticmethod
    def _upto(df: pd.DataFrame, now: datetime, n: int) -> pd.DataFrame:
        i = int(df.index.searchsorted(now, side="right"))
        return df.iloc[max(0, i - n) : i]

    def frames(self, now: datetime) -> tuple[pd.DataFrame, pd.DataFrame]:
        return self._upto(self.h4, now, H4_LOOKBACK), self._upto(self.d1, now, D1_LOOKBACK)

    def h1_from(self, created: datetime) -> list[Candle]:
        lo = bisect.bisect_left(self.h1_open, created)
        hi = bisect.bisect_right(self.h1_open, created + SIM_SPAN)
        return self.h1[lo:hi]

    def mark(self, now: datetime) -> float | None:
        i = bisect.bisect_right(self.h1_close, now) - 1
        return self.h1[i].close if i >= 0 else None


def _windows(start: datetime, end: datetime, windows_utc: list[time]) -> list[datetime]:
    out: list[datetime] = []
    day = start.date()
    while day <= end.date():
        out += [datetime.combine(day, w, tzinfo=UTC) for w in sorted(windows_utc)]
        day += timedelta(days=1)
    return [t for t in out if start <= t < end]


def universe_anchors(start: datetime, end: datetime) -> list[datetime]:
    """Моменты пересчёта вселенной: start и 1-е число каждого месяца внутри периода."""
    out = [start]
    y, m = start.year, start.month
    while True:
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        first = datetime(y, m, 1, tzinfo=UTC)
        if first >= end:
            return out
        out.append(first)


def _close_at_end(outcome: Outcome, sim: list[Candle]) -> Outcome:
    last = sim[-1]
    return replace(
        outcome, status="end", exit_time=last.close_time, exit_price=last.close * (1 - SLIPPAGE)
    )


def run_backtest(
    strategies: list[Strategy],
    store: CandleStore,
    rules: dict[str, SymbolRules],
    start: datetime,
    end: datetime,
    start_equity: float,
    windows_utc: list[time] = DEFAULT_WINDOWS_UTC,
) -> BacktestResult:
    daily = {s: store.get(s, "1d", EPOCH, end) for s in rules}
    data: dict[str, _SymbolData] = {}
    anchors = universe_anchors(start, end)
    universes: dict[datetime, list[str]] = {}

    def universe_at(t: datetime) -> list[str]:
        anchor = anchors[bisect.bisect_right(anchors, t) - 1]
        if anchor not in universes:
            universes[anchor] = select_universe(rules, daily, anchor)
        return universes[anchor]

    trades: list[Trade] = []
    active: list[_Position] = []
    realized = 0.0
    peak = start_equity
    state: DDState = "NORMAL"
    times: list[datetime] = []
    values: list[float] = []

    for t in _windows(start, end, windows_utc):
        for p in active:
            if p.ends_at <= t and p.trade is not None:
                realized += p.trade.pnl_usdt
        active = [p for p in active if p.ends_at > t]

        equity = start_equity + realized
        for p in active:
            if p.trade is not None and p.entry_time is not None and p.entry_time < t:
                mark = data[p.sig.symbol].mark(t)
                mark_price = mark if mark is not None else p.entry_price
                equity += unrealized_pnl(p.qty, p.entry_price, mark_price)
        times.append(t)
        values.append(equity)

        peak = max(peak, equity)
        state, _ = step_drawdown(state, equity, peak)
        if state == "HALTED":
            continue

        expires = next_window(t, windows_utc)
        for symbol in universe_at(t):
            if symbol not in data:
                data[symbol] = _SymbolData(store, symbol, start, end)
            d = data[symbol]
            h4, d1 = d.frames(t)
            for strategy in strategies:
                raw = strategy.evaluate(symbol, h4, d1, t)
                if raw is None:
                    continue
                sig = round_prices(raw, rules[symbol])
                open_ = [OpenRisk(p.sig.symbol, p.risk_usdt, p.notional) for p in active]
                sizing = size_position(sig, equity, risk_pct_for(state), open_, rules[symbol])
                if isinstance(sizing, Rejection):
                    continue
                pos = _Position(sig, sizing.qty, sizing.risk_usdt, sizing.notional, expires)
                sim = d.h1_from(t)
                outcome = simulate(sig, sim, expires)
                if outcome.status == "open":
                    outcome = _close_at_end(outcome, sim)
                if outcome.entry_time is not None and outcome.exit_time is not None:
                    pnl, fees = net_pnl(outcome, sizing.qty)
                    pos.entry_time = outcome.entry_time
                    pos.entry_price = outcome.entry_price or 0.0
                    pos.ends_at = outcome.exit_time
                    pos.trade = Trade(
                        symbol, strategy.name, t, outcome.entry_time, outcome.exit_time,
                        outcome.status, pnl, fees,
                    )  # fmt: skip
                    trades.append(pos.trade)
                active.append(pos)

    trades.sort(key=lambda tr: tr.exit_time)
    if not times:
        times, values = [start], [start_equity]
    times.append(end)
    values.append(start_equity + sum(tr.pnl_usdt for tr in trades))
    equity_series = pd.Series(values, index=pd.DatetimeIndex(times, name="time"), dtype=float)
    return BacktestResult(trades, equity_series, start, end)
