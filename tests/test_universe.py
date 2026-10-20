from datetime import UTC, datetime, timedelta

from invest_agent.models import Candle, SymbolRules
from invest_agent.universe import select_universe

NOW = datetime(2026, 10, 20, tzinfo=UTC)


def _rules(*bases: str) -> dict[str, SymbolRules]:
    return {f"{b}USDT": SymbolRules(f"{b}USDT", 0.01, 0.001, 5.0) for b in bases}


def _days(n: int, qv: float, end: datetime = NOW) -> list[Candle]:
    out = []
    for i in range(n, 0, -1):
        close = end - timedelta(days=i - 1)
        out.append(Candle(close - timedelta(days=1), close, 1, 1, 1, 1, 1, qv))
    return out


def test_always_includes_btc_eth_even_if_low_volume():
    rules = _rules("BTC", "ETH", "AAA", "BBB", "CCC")
    daily = {s: _days(400, 1e9 if s[0] in "ABC" else 1.0) for s in rules}
    out = select_universe(rules, daily, NOW, size=3)
    assert len(out) == 3
    assert {"BTCUSDT", "ETHUSDT"} <= set(out)
    assert out[0] == "AAAUSDT"


def test_btc_eth_skipped_if_not_in_rules():
    rules = _rules("AAA", "BBB")
    daily = {s: _days(400, 5.0) for s in rules}
    assert sorted(select_universe(rules, daily, NOW, size=5)) == ["AAAUSDT", "BBBUSDT"]


def test_excludes_stablecoins_wrapped_and_leveraged():
    bad = ["USDC", "FDUSD", "TUSD", "DAI", "USDP", "EUR", "AEUR", "USDE",
           "WBTC", "WBETH", "BETH", "STETH", "BTCUP", "BTCDOWN", "ETHBULL", "ETHBEAR"]
    rules = _rules("BTC", "ETH", "GOOD", "JUP", "SYRUP", *bad)
    daily = {s: _days(400, 1e9) for s in rules}
    out = select_universe(rules, daily, NOW, size=50)
    assert sorted(out) == ["BTCUSDT", "ETHUSDT", "GOODUSDT", "JUPUSDT", "SYRUPUSDT"]


def test_excludes_history_shorter_than_year():
    rules = _rules("OLD", "NEW")
    daily = {"OLDUSDT": _days(366, 1.0), "NEWUSDT": _days(300, 1e9)}
    assert select_universe(rules, daily, NOW, size=10) == ["OLDUSDT"]


def test_picks_top_by_30d_quote_volume():
    rules = _rules("BTC", "ETH", "A", "B", "C", "D")
    vols = {"A": 50.0, "B": 10.0, "C": 40.0, "D": 30.0, "BTC": 1.0, "ETH": 1.0}
    daily = {f"{b}USDT": _days(400, v) for b, v in vols.items()}
    # old days with huge volume must not count (only last 30 closed days)
    daily["BUSDT"] = _days(370, 1e9, end=NOW - timedelta(days=30)) + _days(30, 10.0)
    out = select_universe(rules, daily, NOW, size=4)
    assert out == ["AUSDT", "CUSDT", "BTCUSDT", "ETHUSDT"]


def test_uses_only_data_before_now():
    rules = _rules("AAA", "BBB")
    daily = {
        "AAAUSDT": _days(400, 10.0),
        "BBBUSDT": _days(400, 1.0) + [
            Candle(NOW, NOW + timedelta(days=1), 1, 1, 1, 1, 1, 1e12)
        ],
    }
    assert select_universe(rules, daily, NOW, size=1) == ["AAAUSDT"]


def test_volume_window_is_30_calendar_days_even_with_gaps():
    rules = _rules("AAA", "BBB")
    old = _days(370, 1e9, end=NOW - timedelta(days=60))
    daily = {
        "AAAUSDT": _days(400, 5.0),
        "BBBUSDT": old + _days(3, 20.0),  # gap: older candles are outside 30 days
    }
    assert select_universe(rules, daily, NOW, size=1) == ["AAAUSDT"]
