from datetime import datetime, timedelta

from invest_agent.models import Candle, SymbolRules

EXCLUDED_BASES = frozenset({
    # стейблкоины (USD, EUR)
    "USDC", "FDUSD", "TUSD", "DAI", "USDP", "EUR", "AEUR", "USDE", "USD1", "EURI", "XUSD",
    "PYUSD", "BFUSD", "RLUSD", "USDS", "BUSD", "SUSD", "UST",
    # обёрнутые и стейкинговые токены
    "WBTC", "WBETH", "BETH", "STETH", "WSTETH", "BNSOL",
})
LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")
ALWAYS_INCLUDED = ("BTCUSDT", "ETHUSDT")
HISTORY_DAYS = 365
VOLUME_DAYS = 30
PEG_DAYS = 90
PEG_BAND = 0.02


def _base(symbol: str) -> str:
    return symbol.removesuffix("USDT")


def _excluded(symbol: str, rules: dict[str, SymbolRules]) -> bool:
    base = _base(symbol)
    if base in EXCLUDED_BASES:
        return True
    return any(
        base.endswith(suf) and base[: -len(suf)] + "USDT" in rules for suf in LEVERAGED_SUFFIXES
    )


def _closed(candles: list[Candle], now: datetime) -> list[Candle]:
    return sorted((c for c in candles if c.close_time <= now), key=lambda c: c.open_time)


def _pegged(closed: list[Candle], now: datetime) -> bool:
    """Все дневные закрытия за PEG_DAYS в пределах ±PEG_BAND от среднего — новый стейблкоин."""
    since = now - timedelta(days=PEG_DAYS)
    closes = [c.close for c in closed if c.close_time > since]
    if not closes:
        return False
    mean = sum(closes) / len(closes)
    return mean > 0 and all(abs(x / mean - 1) <= PEG_BAND for x in closes)


def select_universe(
    rules: dict[str, SymbolRules],
    daily: dict[str, list[Candle]],
    now: datetime,
    size: int = 10,
) -> list[str]:
    vol_start = now - timedelta(days=VOLUME_DAYS)
    volume: dict[str, float] = {}
    eligible: list[str] = []
    for symbol in rules:
        if _excluded(symbol, rules):
            continue
        closed = _closed(daily.get(symbol, []), now)
        volume[symbol] = sum(c.quote_volume for c in closed if c.close_time > vol_start)
        long_enough = closed and closed[0].open_time <= now - timedelta(days=HISTORY_DAYS)
        if long_enough and not _pegged(closed, now):
            eligible.append(symbol)

    # BTC/ETH bypass the history and volume filters (only need to be in rules)
    forced = [s for s in ALWAYS_INCLUDED if s in rules]
    others = sorted((s for s in eligible if s not in forced), key=lambda s: (-volume[s], s))
    picked = forced + others[: max(size - len(forced), 0)]
    return sorted(picked, key=lambda s: (-volume[s], s))


def candidate_symbols(rules: dict[str, SymbolRules]) -> list[str]:
    """Пары, прошедшие фильтры состава (без фильтра объёма и истории)."""
    return sorted(s for s in rules if not _excluded(s, rules))
