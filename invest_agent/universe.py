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


def _excluded(symbol: str, rules: dict[str, SymbolRules]) -> bool:
    base = _base(symbol)
    if base in EXCLUDED_BASES:
        return True
    return any(
        base.endswith(suf) and base[: -len(suf)] + "USDT" in rules for suf in LEVERAGED_SUFFIXES
    )


def _closed(candles: list[Candle], now: datetime) -> list[Candle]:
    return sorted((c for c in candles if c.close_time <= now), key=lambda c: c.open_time)


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
        if closed and closed[0].open_time <= now - timedelta(days=HISTORY_DAYS):
            eligible.append(symbol)

    # BTC/ETH bypass the history and volume filters (only need to be in rules)
    forced = [s for s in ALWAYS_INCLUDED if s in rules]
    others = sorted((s for s in eligible if s not in forced), key=lambda s: (-volume[s], s))
    picked = forced + others[: max(size - len(forced), 0)]
    return sorted(picked, key=lambda s: (-volume[s], s))


def candidate_symbols(rules: dict[str, SymbolRules]) -> list[str]:
    """Пары, прошедшие фильтры состава (без фильтра объёма и истории)."""
    return sorted(s for s in rules if not _excluded(s, rules))
