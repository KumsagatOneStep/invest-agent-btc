import math
from datetime import datetime
from typing import ClassVar

import pandas as pd

from invest_agent.indicators import donchian_high, ema
from invest_agent.models import SignalCandidate
from invest_agent.strategies.base import (
    build_signal,
    closed_only,
    compute_version,
    last_atr,
)


class TrendBreakout:
    name = "trend"
    DEFAULTS: ClassVar[dict[str, float]] = {
        "donchian_n": 20,
        "trend_ema": 50,
        "atr_k": 2.0,
        "rr": 2.0,
    }

    def __init__(self, params: dict[str, float] | None = None) -> None:
        self.params = {**self.DEFAULTS, **(params or {})}

    def version(self) -> str:
        return compute_version(self.name, self.params)

    def evaluate(
        self, symbol: str, h4: pd.DataFrame, d1: pd.DataFrame, now: datetime
    ) -> SignalCandidate | None:
        h4 = closed_only(h4, now)
        d1 = closed_only(d1, now)
        n = int(self.params["donchian_n"])
        ema_n = int(self.params["trend_ema"])
        if len(h4) < n + 1 or len(d1) < ema_n:
            return None
        atr_value = last_atr(h4)
        if atr_value is None:
            return None
        ema_d1 = float(ema(d1["close"], ema_n).iloc[-1])
        close_d1 = float(d1["close"].iloc[-1])
        donchian = float(donchian_high(h4, n).iloc[-1])
        close = float(h4["close"].iloc[-1])
        if math.isnan(donchian) or math.isnan(ema_d1):
            return None
        if not (close_d1 > ema_d1 and close > donchian):
            return None
        return build_signal(
            symbol=symbol,
            name=self.name,
            now=now,
            close=close,
            atr_value=atr_value,
            atr_k=self.params["atr_k"],
            rr=self.params["rr"],
            features={"ema_d1": ema_d1, "close_d1": close_d1, "donchian": donchian},
        )
