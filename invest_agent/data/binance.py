import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Literal

import httpx

from invest_agent.models import Candle, SymbolRules

PAGE_LIMIT = 1000
RETRY_PAUSES = (1, 2, 4)


class BinanceError(Exception):
    pass


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _from_ms(ms: int) -> datetime:
    return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=ms)


class BinanceClient:
    def __init__(
        self,
        base_url: str = "https://data-api.binance.vision",
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = base_url
        self.http = http or httpx.Client(timeout=15)
        self.sleep = sleep

    def _get(self, path: str, params: dict | None = None):
        for attempt in range(len(RETRY_PAUSES) + 1):
            try:
                resp = self.http.get(self.base_url + path, params=params)
            except httpx.HTTPError as e:
                raise BinanceError(f"network error on {path}: {e}") from e
            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError as e:
                    raise BinanceError(f"{path}: invalid JSON body") from e
            retryable = resp.status_code == 429 or resp.status_code >= 500
            if not retryable or attempt == len(RETRY_PAUSES):
                raise BinanceError(f"{path}: HTTP {resp.status_code}")
            self.sleep(RETRY_PAUSES[attempt])
        raise AssertionError("unreachable")

    def klines(
        self, symbol: str, interval: Literal["1h", "4h", "1d"], start: datetime, end: datetime
    ) -> list[Candle]:
        out: list[Candle] = []
        cursor, end_ms = _ms(start), _ms(end)
        while cursor <= end_ms:
            rows = self._get(
                "/api/v3/klines",
                {
                    "symbol": symbol,
                    "interval": interval,
                    "startTime": cursor,
                    "endTime": end_ms,
                    "limit": PAGE_LIMIT,
                },
            )
            if not isinstance(rows, list):
                raise BinanceError(f"{symbol}: klines response is not a list")
            try:
                for r in rows:
                    if r[6] > end_ms:
                        continue
                    out.append(
                        Candle(
                            _from_ms(r[0]), _from_ms(r[6]),
                            float(r[1]), float(r[2]), float(r[3]), float(r[4]),
                            float(r[5]), float(r[7]),
                        )
                    )  # fmt: skip
            except (KeyError, IndexError, TypeError, ValueError, OverflowError) as e:
                raise BinanceError(f"{symbol}: malformed klines response") from e
            if len(rows) < PAGE_LIMIT:
                break
            cursor = rows[-1][6] + 1
        return out

    def symbol_rules(self) -> dict[str, SymbolRules]:
        data = self._get("/api/v3/exchangeInfo")
        rules: dict[str, SymbolRules] = {}
        try:
            for s in data["symbols"]:
                if (
                    s.get("status") != "TRADING"
                    or s.get("quoteAsset") != "USDT"
                    or not s.get("isSpotTradingAllowed")
                ):
                    continue
                f = {x["filterType"]: x for x in s["filters"]}
                notional = f.get("NOTIONAL") or f.get("MIN_NOTIONAL")
                if "PRICE_FILTER" not in f or "LOT_SIZE" not in f or notional is None:
                    continue
                rules[s["symbol"]] = SymbolRules(
                    s["symbol"],
                    float(f["PRICE_FILTER"]["tickSize"]),
                    float(f["LOT_SIZE"]["stepSize"]),
                    float(notional["minNotional"]),
                )
        except (KeyError, IndexError, TypeError, ValueError, OverflowError, AttributeError) as e:
            raise BinanceError("malformed exchangeInfo response") from e
        return rules
