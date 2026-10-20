import sqlite3
from datetime import UTC, datetime, timedelta

from invest_agent.data.candles import CandleStore, to_frame
from invest_agent.models import Candle

T0 = datetime(2026, 1, 1, tzinfo=UTC)
HOUR = timedelta(hours=1)


def candle(i: int) -> Candle:
    o = T0 + i * HOUR
    return Candle(o, o + HOUR - timedelta(milliseconds=1), 1.0 + i, 2.0 + i, 0.5, 1.5 + i, 10, 15)


class FakeClient:
    def __init__(self, n: int):
        self.n = n
        self.calls: list[tuple[str, str, datetime, datetime]] = []

    def klines(self, symbol, interval, start, end):
        self.calls.append((symbol, interval, start, end))
        return [
            c for c in map(candle, range(self.n)) if c.open_time >= start and c.close_time <= end
        ]


def make_store() -> CandleStore:
    conn = sqlite3.connect(":memory:")
    CandleStore.ensure_schema(conn)
    return CandleStore(conn)


def test_upsert_get_roundtrip_and_idempotent():
    store = make_store()
    store.upsert("BTCUSDT", "1h", [candle(0), candle(1)])
    store.upsert("BTCUSDT", "1h", [candle(1), candle(2)])
    got = store.get("BTCUSDT", "1h", T0, T0 + 3 * HOUR)
    assert got == [candle(0), candle(1), candle(2)]
    assert got[0].open_time.tzinfo is UTC
    assert store.get("BTCUSDT", "4h", T0, T0 + 3 * HOUR) == []
    assert store.last_close_time("BTCUSDT", "1h") == candle(2).close_time
    assert store.last_close_time("ETHUSDT", "1h") is None


def test_store_refresh_is_incremental():
    store = make_store()
    client = FakeClient(10)
    now1 = T0 + 5 * HOUR
    store.refresh(client, "BTCUSDT", "1h", now1)
    assert client.calls[0][2] == now1 - timedelta(days=4 * 365)
    assert store.last_close_time("BTCUSDT", "1h") == candle(4).close_time

    store.refresh(client, "BTCUSDT", "1h", T0 + 8 * HOUR)
    assert client.calls[1][2] == candle(4).close_time + timedelta(milliseconds=1)
    assert client.calls[1][2] == candle(5).open_time
    assert store.last_close_time("BTCUSDT", "1h") == candle(7).close_time
    assert len(store.get("BTCUSDT", "1h", T0, T0 + 10 * HOUR)) == 8


def test_to_frame():
    df = to_frame([candle(0), candle(1)])
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df.index.name == "close_time"
    assert df.index[0] == candle(0).close_time
    assert df["close"].tolist() == [1.5, 2.5]
