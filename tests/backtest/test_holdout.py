import json
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from invest_agent.backtest import holdout
from invest_agent.backtest.engine import BacktestResult, Period, Trade
from invest_agent.backtest.gate import GateVerdict
from invest_agent.backtest.holdout import HoldoutAlreadyRun, HoldoutLog, run_holdout
from invest_agent.strategies.trend import TrendBreakout

START = datetime(2026, 1, 1, tzinfo=UTC)
END = START + timedelta(days=300)


def canned(pnl: float, n: int = 30) -> BacktestResult:
    trades = [
        Trade(
            "BTCUSDT", "trend", START, START, START + timedelta(days=i * 10 + 1), "target", pnl, 0
        )
        for i in range(n)
    ]
    return BacktestResult(trades, pd.Series([1.0], index=pd.DatetimeIndex([START])), START, END)


def run(monkeypatch, log, pnl, strategy=None):
    monkeypatch.setattr(holdout, "run_backtest", lambda *a, **k: canned(pnl))
    return run_holdout(strategy or TrendBreakout(), None, {}, Period(START, END), 100.0, log)


def test_holdout_refuses_second_run_same_version(tmp_path, monkeypatch):
    log = HoldoutLog(tmp_path / "holdout_log.json")
    run(monkeypatch, log, -1.0)
    assert log.already_run(TrendBreakout().version())
    with pytest.raises(HoldoutAlreadyRun):
        run(monkeypatch, log, 5.0)
    # другая версия (другие параметры) — можно
    verdict, _ = run(monkeypatch, log, -1.0, TrendBreakout({"atr_k": 3.0}))
    assert not verdict.passed


def test_holdout_writes_approved_only_when_passed(tmp_path, monkeypatch):
    log = HoldoutLog(tmp_path / "holdout_log.json")
    verdict, _ = run(monkeypatch, log, -1.0)
    assert not verdict.passed
    assert log.approved() == []
    assert not (tmp_path / "approved_strategies.json").exists()

    good = TrendBreakout({"atr_k": 3.0})
    verdict, _ = run(monkeypatch, log, 1.0, good)
    assert verdict.passed
    assert log.approved() == [{"name": "trend", "params": good.params, "version": good.version()}]
    assert json.loads((tmp_path / "approved_strategies.json").read_text()) == log.approved()


def test_record_keeps_reasons(tmp_path):
    log = HoldoutLog(tmp_path / "h.json")
    log.record("v1", GateVerdict(False, ["мало сделок"]), {"name": "trend", "params": {}})
    assert log.entries()["v1"]["reasons"] == ["мало сделок"]
