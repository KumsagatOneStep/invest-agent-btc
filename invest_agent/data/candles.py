import sqlite3
from datetime import UTC, datetime, timedelta

import pandas as pd

from invest_agent.data.binance import BinanceClient
from invest_agent.models import Candle

HISTORY = timedelta(days=4 * 365 + 7)  # запас, чтобы первый якорь train видел ≥ 365 дней
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
MARK_SPAN = timedelta(days=3)


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _from_ms(ms: int) -> datetime:
    return _EPOCH + timedelta(milliseconds=ms)


def to_frame(candles: list[Candle]) -> pd.DataFrame:
    df = pd.DataFrame(
        {
            "close_time": [c.close_time for c in candles],
            "open": [c.open for c in candles],
            "high": [c.high for c in candles],
            "low": [c.low for c in candles],
            "close": [c.close for c in candles],
            "volume": [c.volume for c in candles],
        }
    )
    return df.set_index("close_time")


def last_marks(store: "CandleStore", symbols: set[str], at: datetime) -> dict[str, float]:
    """Отметки: последнее закрытие 1h с close_time <= at (не старше MARK_SPAN)."""
    marks = {}
    for sym in symbols:
        candles = store.get(sym, "1h", at - MARK_SPAN, at)
        if candles:
            marks[sym] = candles[-1].close
    return marks


class CandleStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    @staticmethod
    def ensure_schema(conn: sqlite3.Connection) -> None:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS candles (
                symbol TEXT NOT NULL, interval TEXT NOT NULL,
                open_time INTEGER NOT NULL, close_time INTEGER NOT NULL,
                open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
                volume REAL NOT NULL, quote_volume REAL NOT NULL,
                PRIMARY KEY (symbol, interval, open_time))"""
        )
        conn.commit()

    def upsert(self, symbol: str, interval: str, candles: list[Candle]) -> None:
        self.conn.executemany(
            "INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    symbol, interval, _ms(c.open_time), _ms(c.close_time),
                    c.open, c.high, c.low, c.close, c.volume, c.quote_volume,
                )
                for c in candles
            ],
        )  # fmt: skip
        self.conn.commit()

    def get(self, symbol: str, interval: str, start: datetime, end: datetime) -> list[Candle]:
        rows = self.conn.execute(
            """SELECT open_time, close_time, open, high, low, close, volume, quote_volume
               FROM candles WHERE symbol=? AND interval=? AND open_time>=? AND close_time<=?
               ORDER BY open_time""",
            (symbol, interval, _ms(start), _ms(end)),
        ).fetchall()
        return [Candle(_from_ms(r[0]), _from_ms(r[1]), *r[2:]) for r in rows]

    def last_close_time(self, symbol: str, interval: str) -> datetime | None:
        row = self.conn.execute(
            "SELECT MAX(close_time) FROM candles WHERE symbol=? AND interval=?",
            (symbol, interval),
        ).fetchone()
        return _from_ms(row[0]) if row[0] is not None else None

    def refresh(self, client: BinanceClient, symbol: str, interval: str, now: datetime) -> None:
        last = self.last_close_time(symbol, interval)
        start = last + timedelta(milliseconds=1) if last else now - HISTORY
        self.upsert(symbol, interval, client.klines(symbol, interval, start, now))
