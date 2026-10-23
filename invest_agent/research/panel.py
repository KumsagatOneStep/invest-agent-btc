"""Дневные свечи из базы в таблицы дата × монета и состав вселенной на каждый день."""

from dataclasses import dataclass
from datetime import UTC, datetime

import pandas as pd

from invest_agent.backtest.engine import universe_anchors
from invest_agent.data.candles import CandleStore
from invest_agent.models import Candle, SymbolRules
from invest_agent.universe import candidate_symbols, select_universe

EPOCH = datetime(2000, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class Panel:
    closes: pd.DataFrame
    volumes: pd.DataFrame  # объём в USDT
    member: pd.DataFrame  # монета во вселенной (пересчёт 1-го числа месяца, как в бэктесте)


def _day(c: Candle) -> pd.Timestamp:
    return pd.Timestamp(c.open_time).normalize()


def load_panel(
    store: CandleStore, rules: dict[str, SymbolRules], start: datetime, end: datetime, size: int
) -> Panel:
    daily = {s: store.get(s, "1d", EPOCH, end) for s in candidate_symbols(rules)}
    members: dict[datetime, list[str]] = {
        a: select_universe(rules, daily, a, size) for a in universe_anchors(start, end)
    }
    symbols = sorted({s for picked in members.values() for s in picked})
    days = pd.date_range(pd.Timestamp(start).normalize(), pd.Timestamp(end).normalize(), freq="D")

    def table(field: str) -> pd.DataFrame:
        cols = {
            s: pd.Series({_day(c): getattr(c, field) for c in daily[s]}, dtype=float)
            for s in symbols
        }
        return pd.DataFrame(cols).reindex(days)

    member = pd.DataFrame(False, index=days, columns=symbols)
    anchors = sorted(members)
    for a, nxt in zip(anchors, [*anchors[1:], None], strict=True):
        rows = (days >= pd.Timestamp(a).normalize()) & (
            days < pd.Timestamp(nxt).normalize() if nxt else True
        )
        member.loc[rows, members[a]] = True
    closes = table("close")
    return Panel(closes, table("quote_volume"), member & closes.notna())
