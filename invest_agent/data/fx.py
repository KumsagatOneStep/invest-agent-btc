import math
import sqlite3
from datetime import datetime, timedelta

import httpx

URL = "https://open.er-api.com/v6/latest/USD"
TTL = timedelta(days=1)


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS fx (fetched_at TEXT NOT NULL PRIMARY KEY, usd_kzt REAL NOT NULL)"
    )
    conn.commit()


def usd_kzt(now: datetime, conn: sqlite3.Connection, http: httpx.Client) -> float | None:
    row = conn.execute(
        "SELECT fetched_at, usd_kzt FROM fx ORDER BY fetched_at DESC LIMIT 1"
    ).fetchone()
    cached = (datetime.fromisoformat(row[0]), row[1]) if row else None
    if cached and now - cached[0] < TTL:
        return cached[1]
    try:
        resp = http.get(URL)
        resp.raise_for_status()
        rate = float(resp.json()["rates"]["KZT"])
        if not (math.isfinite(rate) and rate > 0):
            raise ValueError("non-positive rate")
    except (httpx.HTTPError, KeyError, ValueError, TypeError):
        return cached[1] if cached else None
    conn.execute("INSERT OR REPLACE INTO fx VALUES (?, ?)", (now.isoformat(), rate))
    conn.commit()
    return rate
