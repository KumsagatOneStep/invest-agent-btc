import math
from datetime import datetime
from typing import ClassVar

import pandas as pd

from invest_agent.config import MIN_RR
from invest_agent.indicators import ema, rsi
from invest_agent.models import SignalCandidate
from invest_agent.strategies.base import (
    build_signal,
    closed_only,
    compute_version,
    last_atr,
)


class MeanReversion:
    name = "meanrev"
    DEFAULTS: ClassVar[dict[str, float]] = {
        "rsi_n": 14,
        "rsi_max": 30,
        "trend_ema": 50,
        "atr_k": 1.5,
    }
    RR = MIN_RR  # fixed, not a tunable parameter

    def __init__(self, params: dict[str, float] | None = None) -> None:
        self.params = {**self.DEFAULTS, **(params or {})}

    def version(self) -> str:
        return compute_version(self.name, self.params)

    def evaluate(
        self, symbol: str, h4: pd.DataFrame, d1: pd.DataFrame, now: datetime
    ) -> SignalCandidate | None:
        h4 = closed_only(h4, now)
        d1 = closed_only(d1, now)
        rsi_n = int(self.params["rsi_n"])
        ema_n = int(self.params["trend_ema"])
        if len(h4) < rsi_n + 1 or len(d1) < ema_n:
            return None
        atr_value = last_atr(h4)
        if atr_value is None:
            return None
        rsi_value = float(rsi(h4["close"], rsi_n).iloc[-1])
        ema_d1 = float(ema(d1["close"], ema_n).iloc[-1])
        close_d1 = float(d1["close"].iloc[-1])
        if math.isnan(rsi_value) or math.isnan(ema_d1):
            return None
        if not (close_d1 > ema_d1 and rsi_value < self.params["rsi_max"]):
            return None
        return build_signal(
            symbol=symbol,
            name=self.name,
            now=now,
            close=float(h4["close"].iloc[-1]),
            atr_value=atr_value,
            atr_k=self.params["atr_k"],
            rr=self.RR,
            features={"rsi": rsi_value, "ema_d1": ema_d1, "close_d1": close_d1},
        )
