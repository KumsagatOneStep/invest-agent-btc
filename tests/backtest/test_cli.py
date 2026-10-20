import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from invest_agent.backtest import __main__ as cli
from invest_agent.backtest.engine import Period
from invest_agent.backtest.holdout import HoldoutLog
from invest_agent.backtest.report import build_report
from invest_agent.data.candles import CandleStore
from invest_agent.models import Candle, SymbolRules
from invest_agent.strategies.trend import TrendBreakout

MS = timedelta(milliseconds=1)


def dt(*a: int) -> datetime:
    return datetime(*a, tzinfo=UTC)


def flat(start, end, step, price=100.0):
    out, t = [], start
    while t + step <= end:
        out.append(Candle(t, t + step - MS, price, price, price, price, 1.0, 1e6))
        t += step
    return out


class SpyStore(CandleStore):
    def __init__(self, conn):
        super().__init__(conn)
        self.ends: list[datetime] = []

    def get(self, symbol, interval, start, end):
        self.ends.append(end)
        return super().get(symbol, interval, start, end)


def spy_store() -> SpyStore:
    conn = sqlite3.connect(":memory:")
    CandleStore.ensure_schema(conn)
    return SpyStore(conn)


def test_param_keys_validated_against_defaults():
    s = cli.make_strategy("trend", ["atr_k=3"])
    assert s.params["atr_k"] == 3.0
    with pytest.raises(cli.CliError, match="atr_kk"):
        cli.make_strategy("trend", ["atr_kk=3"])
    with pytest.raises(cli.CliError):
        cli.make_strategy("meanrev", ["donchian_n=5"])  # ключ другой стратегии
    with pytest.raises(cli.CliError):
        cli.make_strategy("trend", ["atr_k=abc"])


def test_main_rejects_unknown_param(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["validate", "--strategy", "trend", "--param", "bogus=1"]) == 2
    assert "bogus" in capsys.readouterr().err


def test_validate_never_reads_holdout_candles(tmp_path):
    store = spy_store()
    store.upsert("BTCUSDT", "1d", flat(dt(2024, 1, 1), dt(2026, 4, 1), timedelta(days=1)))
    store.upsert("BTCUSDT", "1h", flat(dt(2025, 12, 1), dt(2026, 4, 1), timedelta(hours=1)))
    periods = (
        Period(dt(2026, 1, 1), dt(2026, 2, 1)),
        Period(dt(2026, 2, 1), dt(2026, 3, 1)),
        Period(dt(2026, 3, 1), dt(2026, 4, 1)),
    )
    rules = {"BTCUSDT": SymbolRules("BTCUSDT", 0.01, 0.001, 5.0)}
    entry = cli.validate(TrendBreakout(), store, rules, periods, tmp_path / "val.json")

    assert store.ends and max(store.ends) <= periods[2].start
    assert set(entry) >= {"train", "validation"}
    assert json.loads((tmp_path / "val.json").read_text())[TrendBreakout().version()]


def test_holdout_cmd_refuses_second_run(tmp_path, capsys):
    store = spy_store()
    store.upsert("BTCUSDT", "1d", flat(dt(2024, 1, 1), dt(2026, 4, 1), timedelta(days=1)))
    store.upsert("BTCUSDT", "1h", flat(dt(2026, 3, 1), dt(2026, 4, 1), timedelta(hours=1)))
    periods = (
        Period(dt(2026, 1, 1), dt(2026, 2, 1)),
        Period(dt(2026, 2, 1), dt(2026, 3, 1)),
        Period(dt(2026, 3, 1), dt(2026, 4, 1)),
    )
    rules = {"BTCUSDT": SymbolRules("BTCUSDT", 0.01, 0.001, 5.0)}
    log = HoldoutLog(tmp_path / "holdout_log.json")
    assert cli.holdout_cmd(TrendBreakout(), store, rules, periods, log)
    assert not cli.holdout_cmd(TrendBreakout(), store, rules, periods, log)
    assert "уже запускалась" in capsys.readouterr().out


class FakeClient:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def symbol_rules(self):
        return {
            s: SymbolRules(s, 0.01, 0.001, 5.0)
            for s in ("BTCUSDT", "ETHUSDT", "USDCUSDT", "ABCUSDT")
        }

    def klines(self, symbol, interval, start, end):
        self.calls.append((symbol, interval))
        step = {"1h": timedelta(hours=1), "4h": timedelta(hours=4), "1d": timedelta(days=1)}
        return flat(start.replace(minute=0, second=0, microsecond=0), end, step[interval])[:50]


def test_fetch_skips_excluded_and_writes_rules(tmp_path):
    conn = sqlite3.connect(":memory:")
    CandleStore.ensure_schema(conn)
    client = FakeClient()
    cli.fetch(client, CandleStore(conn), tmp_path / "rules.json", dt(2026, 4, 1))
    assert not any(s == "USDCUSDT" for s, _ in client.calls)
    assert {("BTCUSDT", i) for i in ("1d", "4h", "1h")} <= set(client.calls)
    assert set(cli.load_rules(tmp_path / "rules.json")) == {
        "BTCUSDT", "ETHUSDT", "USDCUSDT", "ABCUSDT",
    }  # fmt: skip


def test_report_states_survivorship_and_holdout_count():
    summary = {"trades": 31, "win_pct": 50.0, "net_pnl": 12.0, "max_dd": 0.1, "btc_hold_pct": 5.0}
    val = {"v1": {"name": "trend", "params": {}, "train": summary, "validation": summary}}
    hold = {"v1": {"passed": False, "reasons": ["мало"], "summary": {**summary, "name": "trend"}}}
    text = build_report(val, hold)
    assert "выживших" in text
    assert "проверено на holdout: 1" in text
    assert "BTC buy&hold" in text
