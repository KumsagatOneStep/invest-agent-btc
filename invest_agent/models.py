from dataclasses import dataclass
from datetime import datetime
from typing import Literal


@dataclass(frozen=True)
class Candle:
    open_time: datetime
    close_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float


@dataclass(frozen=True)
class SignalCandidate:
    symbol: str
    strategy: str
    created_at: datetime
    entry_low: float
    entry_high: float
    stop: float
    target: float
    features: dict[str, float]


@dataclass(frozen=True)
class Outcome:
    status: Literal["pending", "expired", "open", "stop", "target", "time"]
    entry_time: datetime | None
    entry_price: float | None
    exit_time: datetime | None
    exit_price: float | None


@dataclass(frozen=True)
class Sizing:
    qty: float
    notional: float
    risk_usdt: float


@dataclass(frozen=True)
class Rejection:
    reason: str


@dataclass(frozen=True)
class SymbolRules:
    symbol: str
    tick_size: float
    step_size: float
    min_notional: float
