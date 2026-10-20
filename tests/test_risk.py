from dataclasses import replace
from datetime import UTC, datetime

import pytest

from invest_agent.models import Rejection, SignalCandidate, Sizing, SymbolRules
from invest_agent.risk import OpenRisk, risk_pct_for, round_prices, size_position, step_drawdown

RULES = SymbolRules("BTCUSDT", tick_size=0.01, step_size=0.1, min_notional=10.0)


def sig(**kw) -> SignalCandidate:
    base = {
        "symbol": "BTCUSDT", "strategy": "trend",
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "entry_low": 99.0, "entry_high": 100.0, "stop": 95.0, "target": 110.0, "features": {},
    }
    base.update(kw)
    return SignalCandidate(**base)


def test_one_percent_risk_qty():
    r = size_position(sig(), 400.0, 0.01, [], RULES)
    assert isinstance(r, Sizing)
    assert r.qty == 0.8
    assert r.notional == pytest.approx(80.0)
    assert r.risk_usdt == pytest.approx(4.0)


def test_caps_at_25_percent_equity():
    r = size_position(sig(stop=99.0, target=102.0), 400.0, 0.01, [], RULES)
    assert isinstance(r, Sizing)
    assert r.qty == 1.0
    assert r.notional == pytest.approx(100.0)
    assert r.risk_usdt == pytest.approx(1.0)


def test_rejects_non_positive_risk():
    assert size_position(sig(stop=100.0), 400.0, 0.01, [], RULES) == Rejection("stop>=entry")
    assert size_position(sig(stop=101.0), 400.0, 0.01, [], RULES) == Rejection("stop>=entry")


def test_rejects_bad_rr():
    assert size_position(sig(target=107.0), 400.0, 0.01, [], RULES) == Rejection("rr<1.5")
    assert isinstance(size_position(sig(target=107.5), 400.0, 0.01, [], RULES), Sizing)


def test_one_position_per_symbol():
    r = size_position(sig(), 400.0, 0.01, [OpenRisk("BTCUSDT", 1.0, 20.0)], RULES)
    assert r == Rejection("symbol already open")


def test_max_four_open():
    open_ = [OpenRisk(s, 1.0, 20.0) for s in ("A", "B", "C", "D")]
    assert size_position(sig(), 400.0, 0.01, open_, RULES) == Rejection("max open positions")


def test_shrinks_to_remaining_total_risk():
    # limit 16, open 15 -> remaining 1 -> qty 0.2
    r = size_position(sig(), 400.0, 0.01, [OpenRisk("A", 15.0, 20.0)], RULES)
    assert isinstance(r, Sizing)
    assert r.qty == 0.2
    assert r.risk_usdt == pytest.approx(1.0)
    assert size_position(sig(), 400.0, 0.01, [OpenRisk("A", 16.0, 20.0)], RULES) == Rejection(
        "total risk limit"
    )


def test_rejects_below_min_notional():
    r = size_position(sig(), 400.0, 0.01, [], replace(RULES, min_notional=100.0))
    assert r == Rejection("below min notional")


def test_round_prices_directions():
    r = round_prices(
        sig(entry_low=99.001, entry_high=100.019, stop=94.999, target=110.019), RULES
    )
    assert (r.entry_low, r.entry_high, r.stop, r.target) == (99.01, 100.01, 94.99, 110.01)
    exact = round_prices(sig(entry_low=0.3, entry_high=0.3, stop=0.1, target=0.7), replace(RULES, tick_size=0.1))
    assert (exact.entry_low, exact.entry_high, exact.stop, exact.target) == (0.3, 0.3, 0.1, 0.7)


def test_drawdown_ladder():
    assert step_drawdown("NORMAL", 90.0, 100.0) == ("REDUCED", ["reduced"])
    assert step_drawdown("REDUCED", 95.0, 100.0) == ("REDUCED", [])
    assert step_drawdown("REDUCED", 100.0, 100.0) == ("NORMAL", ["recovered"])
    assert step_drawdown("REDUCED", 85.0, 100.0) == ("HALTED", ["halted"])
    assert step_drawdown("NORMAL", 85.0, 100.0) == ("HALTED", ["halted"])
    assert step_drawdown("HALTED", 99.0, 100.0) == ("HALTED", [])
    assert step_drawdown("NORMAL", 99.0, 100.0) == ("NORMAL", [])


def test_risk_pct_for():
    assert risk_pct_for("NORMAL") == 0.01
    assert risk_pct_for("REDUCED") == 0.005
