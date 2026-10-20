from datetime import datetime, timedelta

from invest_agent.models import Candle, SymbolRules

EXCLUDED_BASES = frozenset({
    "USDC", "FDUSD", "TUSD", "DAI", "USDP", "EUR", "AEUR", "USDE",
    "WBTC", "WBETH", "BETH", "STETH",
})
LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")
ALWAYS_INCLUDED = ("BTCUSDT", "ETHUSDT")
HISTORY_DAYS = 365
VOLUME_DAYS = 30


def _base(symbol: str) -> str:
    return symbol.removesuffix("USDT")


def _excluded(symbol: str) -> bool:
    base = _base(symbol)
    return base in EXCLUDED_BASES or base.endswith(LEVERAGED_SUFFIXES)


def _closed(candles: list[Candle], now: datetime) -> list[Candle]:
    return sorted((c for c in candles if c.close_time <= now), key=lambda c: c.open_time)


def select_universe(
    rules: dict[str, SymbolRules],
    daily: dict[str, list[Candle]],
    now: datetime,
    size: int = 10,
) -> list[str]:
    volume: dict[str, float] = {}
    eligible: list[str] = []
    for symbol in rules:
        if _excluded(symbol):
            continue
        closed = _closed(daily.get(symbol, []), now)
        volume[symbol] = sum(c.quote_volume for c in closed[-VOLUME_DAYS:])
        if closed and closed[0].open_time <= now - timedelta(days=HISTORY_DAYS):
            eligible.append(symbol)

    forced = [s for s in ALWAYS_INCLUDED if s in rules]
    others = sorted((s for s in eligible if s not in forced), key=lambda s: (-volume[s], s))
    picked = forced + others[: max(size - len(forced), 0)]
    return sorted(picked, key=lambda s: (-volume[s], s))
