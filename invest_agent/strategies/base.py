import hashlib
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Protocol

import pandas as pd

from invest_agent.config import MIN_RR
from invest_agent.indicators import atr
from invest_agent.models import SignalCandidate

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
# Модули, от которых зависят сигналы и результат бэктеста: их правка = новая версия стратегии
TRACKED = ("indicators.py", "execution.py", "risk.py", "universe.py", "backtest/engine.py")
ATR_N = 14
ENTRY_ATR_FRAC = 0.25


class Strategy(Protocol):
    name: str
    params: dict[str, float]

    def version(self) -> str: ...

    def evaluate(
        self, symbol: str, h4: pd.DataFrame, d1: pd.DataFrame, now: datetime
    ) -> SignalCandidate | None: ...


def closed_only(df: pd.DataFrame, now: datetime) -> pd.DataFrame:
    """Keep only candles already closed at `now` (index is close_time)."""
    return df[df.index <= now]


def tracked_sources(root: Path = PACKAGE_ROOT) -> list[Path]:
    """Пути (относительно пакета) всех файлов, входящих в хеш кода."""
    strategies = [p.relative_to(root) for p in (root / "strategies").glob("*.py")]
    return sorted({*strategies, *(Path(t) for t in TRACKED)})


def code_hash(root: Path = PACKAGE_ROOT) -> str:
    h = hashlib.sha1()
    for rel in tracked_sources(root):
        h.update(rel.as_posix().encode() + b"\0" + (root / rel).read_bytes() + b"\0")
    return h.hexdigest()


def compute_version(name: str, params: dict[str, float]) -> str:
    payload = name + json.dumps(params, sort_keys=True) + code_hash()
    return hashlib.sha1(payload.encode()).hexdigest()


def last_atr(h4: pd.DataFrame) -> float | None:
    """Latest ATR(14) of 4h bars, or None if unavailable or not positive."""
    if len(h4) < ATR_N + 1:
        return None
    value = float(atr(h4, ATR_N).iloc[-1])
    if math.isnan(value) or value <= 0:
        return None
    return value


def build_signal(
    *,
    symbol: str,
    name: str,
    now: datetime,
    close: float,
    atr_value: float,
    atr_k: float,
    rr: float,
    features: dict[str, float],
) -> SignalCandidate | None:
    entry_high = close
    entry_low = close - ENTRY_ATR_FRAC * atr_value
    stop = entry_high - atr_k * atr_value
    risk = entry_high - stop
    if stop <= 0 or risk <= 0 or entry_low <= stop:
        return None
    target = entry_high + max(rr, MIN_RR) * risk
    while target - entry_high < MIN_RR * risk:  # float rounding guard
        target = math.nextafter(target, math.inf)
    return SignalCandidate(
        symbol=symbol,
        strategy=name,
        created_at=now,
        entry_low=entry_low,
        entry_high=entry_high,
        stop=stop,
        target=target,
        features={**features, "atr": atr_value, "close": close},
    )
