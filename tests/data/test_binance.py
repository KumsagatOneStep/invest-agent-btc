from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from invest_agent.data.binance import BinanceClient, BinanceError

BASE = "https://data-api.binance.vision"
T0 = datetime(2026, 1, 1, tzinfo=UTC)
HOUR_MS = 3_600_000


def kline(i: int) -> list:
    open_ms = int(T0.timestamp() * 1000) + i * HOUR_MS
    return [open_ms, "1", "2", "0.5", "1.5", "10", open_ms + HOUR_MS - 1, "15", 5, "0", "0", "0"]


def make_client() -> tuple[BinanceClient, list[float]]:
    sleeps: list[float] = []
    return BinanceClient(http=httpx.Client(), sleep=sleeps.append), sleeps


@respx.mock
def test_klines_paginates_and_drops_unclosed():
    route = respx.get(f"{BASE}/api/v3/klines").mock(
        side_effect=[
            httpx.Response(200, json=[kline(i) for i in range(1000)]),
            httpx.Response(200, json=[kline(i) for i in range(1000, 1003)]),
        ]
    )
    client, _ = make_client()
    end = T0 + timedelta(hours=1002)  # closes of candle 1002 are > end (…59.999 of hour 1003)
    candles = client.klines("BTCUSDT", "1h", T0, end)
    assert len(candles) == 1002
    assert route.call_count == 2
    assert candles[0].open_time == T0
    assert candles[0].close_time == T0 + timedelta(hours=1) - timedelta(milliseconds=1)
    assert candles[0].close_time.tzinfo is UTC
    assert candles[0].quote_volume == 15.0
    assert all(c.close_time <= end for c in candles)
    second = route.calls[1].request.url.params
    assert int(second["startTime"]) == candles[999].close_time.timestamp() * 1000 + 1


@respx.mock
def test_klines_retries_429_then_raises():
    route = respx.get(f"{BASE}/api/v3/klines").mock(return_value=httpx.Response(429))
    client, sleeps = make_client()
    with pytest.raises(BinanceError):
        client.klines("BTCUSDT", "1h", T0, T0 + timedelta(hours=5))
    assert route.call_count == 4
    assert sleeps == [1, 2, 4]


@respx.mock
def test_klines_recovers_after_5xx():
    respx.get(f"{BASE}/api/v3/klines").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json=[kline(0)])]
    )
    client, sleeps = make_client()
    assert len(client.klines("BTCUSDT", "1h", T0, T0 + timedelta(hours=5))) == 1
    assert sleeps == [1]


@respx.mock
def test_451_raises_immediately():
    route = respx.get(f"{BASE}/api/v3/klines").mock(return_value=httpx.Response(451))
    client, sleeps = make_client()
    with pytest.raises(BinanceError):
        client.klines("BTCUSDT", "1h", T0, T0 + timedelta(hours=5))
    assert route.call_count == 1
    assert sleeps == []


@respx.mock
def test_network_error_raises_binance_error():
    respx.get(f"{BASE}/api/v3/klines").mock(side_effect=httpx.ConnectError("boom"))
    client, _ = make_client()
    with pytest.raises(BinanceError):
        client.klines("BTCUSDT", "1h", T0, T0 + timedelta(hours=5))


def symbol(name, quote="USDT", status="TRADING", spot=True, notional_filter="NOTIONAL"):
    return {
        "symbol": name,
        "status": status,
        "quoteAsset": quote,
        "isSpotTradingAllowed": spot,
        "filters": [
            {"filterType": "PRICE_FILTER", "tickSize": "0.01000000"},
            {"filterType": "LOT_SIZE", "stepSize": "0.00100000"},
            {"filterType": notional_filter, "minNotional": "5.00000000"},
        ],
    }


@respx.mock
def test_symbol_rules_filters_non_usdt_and_halted():
    respx.get(f"{BASE}/api/v3/exchangeInfo").mock(
        return_value=httpx.Response(
            200,
            json={
                "symbols": [
                    symbol("BTCUSDT"),
                    symbol("ETHBTC", quote="BTC"),
                    symbol("OLDUSDT", status="BREAK"),
                    symbol("MARGINUSDT", spot=False),
                    symbol("SOLUSDT", notional_filter="MIN_NOTIONAL"),
                ]
            },
        )
    )
    client, _ = make_client()
    rules = client.symbol_rules()
    assert set(rules) == {"BTCUSDT", "SOLUSDT"}
    r = rules["BTCUSDT"]
    assert (r.symbol, r.tick_size, r.step_size, r.min_notional) == ("BTCUSDT", 0.01, 0.001, 5.0)
    assert rules["SOLUSDT"].min_notional == 5.0


@respx.mock
def test_non_json_200_body_raises_binance_error():
    respx.get(f"{BASE}/api/v3/klines").mock(return_value=httpx.Response(200, text="<html>"))
    client, _ = make_client()
    with pytest.raises(BinanceError):
        client.klines("BTCUSDT", "1h", T0, T0 + timedelta(hours=5))


@respx.mock
def test_malformed_kline_row_raises_binance_error():
    respx.get(f"{BASE}/api/v3/klines").mock(return_value=httpx.Response(200, json=[[1, "x"]]))
    client, _ = make_client()
    with pytest.raises(BinanceError):
        client.klines("BTCUSDT", "1h", T0, T0 + timedelta(hours=5))


@respx.mock
def test_exchange_info_without_symbols_raises_binance_error():
    respx.get(f"{BASE}/api/v3/exchangeInfo").mock(return_value=httpx.Response(200, json={}))
    client, _ = make_client()
    with pytest.raises(BinanceError):
        client.symbol_rules()
