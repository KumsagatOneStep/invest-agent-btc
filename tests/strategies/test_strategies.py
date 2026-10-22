import json
import logging
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from invest_agent.config import MIN_RR
from invest_agent.strategies import base
from invest_agent.strategies.base import closed_only
from invest_agent.strategies.meanrev import MeanReversion
from invest_agent.strategies.registry import load_approved
from invest_agent.strategies.trend import TrendBreakout

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def frame(closes: list[float], step: timedelta, end: datetime, spread: float = 0.5) -> pd.DataFrame:
    n = len(closes)
    idx = [end - step * (n - 1 - i) for i in range(n)]
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + spread for c in closes],
            "low": [c - spread for c in closes],
            "close": closes,
            "volume": [1.0] * n,
        },
        index=pd.DatetimeIndex(idx, name="close_time"),
    )


H4 = timedelta(hours=4)
D1 = timedelta(days=1)


def d1_trend(up: bool) -> pd.DataFrame:
    closes = [100 + i for i in range(80)]
    return frame(closes if up else closes[::-1], D1, NOW)


def h4_breakout() -> pd.DataFrame:
    closes = [100 + math.sin(i) for i in range(100)] + [106.0]
    return frame(closes, H4, NOW)


def h4_oversold() -> pd.DataFrame:
    closes = [100 + math.sin(i) for i in range(80)] + [100 - 1.5 * i for i in range(1, 21)]
    return frame(closes, H4, NOW)


def test_trend_fires_on_breakout_in_uptrend():
    sig = TrendBreakout().evaluate("BTCUSDT", h4_breakout(), d1_trend(True), NOW)
    assert sig is not None
    assert sig.strategy == "trend" and sig.symbol == "BTCUSDT" and sig.created_at == NOW
    assert sig.entry_high == 106.0
    assert sig.stop < sig.entry_low < sig.entry_high < sig.target
    assert {"atr", "close", "donchian", "ema_d1"} <= sig.features.keys()


def test_trend_silent_in_downtrend():
    assert TrendBreakout().evaluate("BTCUSDT", h4_breakout(), d1_trend(False), NOW) is None


def test_meanrev_fires_on_oversold_in_uptrend():
    sig = MeanReversion().evaluate("ETHUSDT", h4_oversold(), d1_trend(True), NOW)
    assert sig is not None
    assert sig.strategy == "meanrev"
    assert sig.features["rsi"] < 30
    assert MeanReversion().evaluate("ETHUSDT", h4_oversold(), d1_trend(False), NOW) is None


@pytest.mark.parametrize("cls", [TrendBreakout, MeanReversion])
def test_strategy_returns_none_on_flat_market(cls):
    h4 = frame([100.0] * 120, H4, NOW, spread=0.0)
    assert cls().evaluate("BTCUSDT", h4, d1_trend(True), NOW) is None


def test_zero_atr_returns_none(monkeypatch):
    from invest_agent.strategies import base

    monkeypatch.setattr(base, "atr", lambda df, n: pd.Series(0.0, index=df.index))
    assert TrendBreakout().evaluate("BTCUSDT", h4_breakout(), d1_trend(True), NOW) is None


@pytest.mark.parametrize(
    ("strategy", "h4"),
    [(TrendBreakout(), h4_breakout()), (MeanReversion(), h4_oversold())],
)
def test_no_lookahead(strategy, h4):
    d1 = d1_trend(True)
    base = strategy.evaluate("X", h4, d1, NOW)
    assert base is not None
    fut4 = frame([500.0, 1.0, 900.0], H4, NOW + H4 * 3)
    fut1 = frame([1.0, 2.0], D1, NOW + D1 * 2)
    again = strategy.evaluate("X", pd.concat([h4, fut4]), pd.concat([d1, fut1]), NOW)
    assert again == base


def test_closed_only_filters_future():
    h4 = h4_breakout()
    assert len(closed_only(h4, NOW - H4)) == len(h4) - 1


@pytest.mark.parametrize("rr", [1.0, 1.5, 2.0, 3.0])
def test_signal_respects_min_rr(rr):
    sig = TrendBreakout({"rr": rr}).evaluate("X", h4_breakout(), d1_trend(True), NOW)
    assert sig is not None
    assert sig.target - sig.entry_high >= MIN_RR * (sig.entry_high - sig.stop)
    mr = MeanReversion().evaluate("X", h4_oversold(), d1_trend(True), NOW)
    assert mr is not None
    assert mr.target - mr.entry_high >= MIN_RR * (mr.entry_high - mr.stop)


def test_version_changes_with_params():
    a, b = TrendBreakout(), TrendBreakout({"donchian_n": 30})
    assert a.version() != b.version()
    assert a.version() == TrendBreakout().version()
    assert a.version() != MeanReversion().version()


def test_load_approved(tmp_path: Path, caplog):
    assert load_approved(tmp_path / "missing.json") == []
    good = TrendBreakout({"atr_k": 2.5})
    entries = [
        {"name": "trend", "params": good.params, "version": good.version()},
        {"name": "meanrev", "params": MeanReversion().params, "version": "stale"},
        {"name": "ghost", "params": {}, "version": "x"},
    ]
    path = tmp_path / "approved.json"
    path.write_text(json.dumps(entries))
    with caplog.at_level(logging.WARNING):
        loaded = load_approved(path)
    assert [s.name for s in loaded] == ["trend"]
    assert loaded[0].params == good.params
    assert len(caplog.records) == 2


def test_code_hash_covers_signal_and_backtest_modules():
    names = {p.as_posix() for p in base.tracked_sources()}
    for expected in (
        "strategies/base.py", "strategies/trend.py", "strategies/meanrev.py",
        "indicators.py", "execution.py", "risk.py", "universe.py", "backtest/engine.py",
        "config.py", "models.py", "data/candles.py", "backtest/gate.py",
    ):  # fmt: skip
        assert expected in names


def test_code_hash_changes_with_source(tmp_path: Path):
    for rel in base.tracked_sources():
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes((base.PACKAGE_ROOT / rel).read_bytes())
    same = base.code_hash(tmp_path)
    assert same == base.code_hash(base.PACKAGE_ROOT)
    (tmp_path / "execution.py").write_text("# edited\n", encoding="utf-8")
    assert base.code_hash(tmp_path) != same


def test_version_tracks_code(monkeypatch):
    before = TrendBreakout().version()
    assert before == TrendBreakout().version()
    monkeypatch.setattr(base, "code_hash", lambda root=base.PACKAGE_ROOT: "other-code")
    assert TrendBreakout().version() != before
