from datetime import datetime, time, timedelta

from invest_agent.config import FEE, MAX_HOLD, SLIPPAGE
from invest_agent.models import Candle, Outcome, SignalCandidate


def _stop_exit(c: Candle, sig: SignalCandidate) -> float:
    return min(c.open, sig.stop) * (1 - SLIPPAGE)


def simulate(sig: SignalCandidate, candles_1h: list[Candle], expires_at: datetime) -> Outcome:
    candles = sorted((c for c in candles_1h if c.open_time >= sig.created_at), key=lambda c: c.open_time)

    entry_idx = next(
        (
            i
            for i, c in enumerate(candles)
            if c.open_time < expires_at and c.low <= sig.entry_high and c.high >= sig.entry_low
        ),
        None,
    )
    if entry_idx is None:
        covered = bool(candles) and candles[-1].close_time >= expires_at
        return Outcome("expired" if covered else "pending", None, None, None, None)

    entry_time = candles[entry_idx].open_time
    entry_price = sig.entry_high * (1 + SLIPPAGE)

    for i in range(entry_idx, len(candles)):
        c = candles[i]
        if c.low <= sig.stop:
            return Outcome("stop", entry_time, entry_price, c.close_time, _stop_exit(c, sig))
        if i > entry_idx and c.high >= sig.target:
            return Outcome("target", entry_time, entry_price, c.close_time, sig.target * (1 - SLIPPAGE))
        if c.close_time >= entry_time + MAX_HOLD:
            return Outcome("time", entry_time, entry_price, c.close_time, c.close * (1 - SLIPPAGE))
    return Outcome("open", entry_time, entry_price, None, None)


def net_pnl(outcome: Outcome, qty: float) -> tuple[float, float]:
    assert outcome.entry_price is not None and outcome.exit_price is not None
    fees = FEE * qty * (outcome.entry_price + outcome.exit_price)
    return qty * (outcome.exit_price - outcome.entry_price) - fees, fees


def next_window(after: datetime, windows_utc: list[time]) -> datetime:
    """Первое окно строго позже `after` (UTC), может перейти на следующий день."""
    for day in (0, 1):
        d = after.date() + timedelta(days=day)
        cands = sorted(datetime.combine(d, t, tzinfo=after.tzinfo) for t in windows_utc)
        for w in cands:
            if w > after:
                return w
    raise ValueError("windows_utc is empty")
