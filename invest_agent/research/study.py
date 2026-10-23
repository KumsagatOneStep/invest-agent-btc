"""Событийный анализ: доходность после события против средней по вселенной в те же дни."""

import math
from dataclasses import dataclass

import pandas as pd

from invest_agent.config import FEE, SLIPPAGE

ROUND_TRIP_COST = 2 * (FEE + SLIPPAGE)


@dataclass(frozen=True)
class StudyResult:
    n_dates: int
    n_events: int
    mean_event: float  # средняя доходность вперёд по событиям
    mean_excess: float  # событие минус среднее по вселенной, усреднено по датам
    t_excess: float  # t-статистика избытка (даты независимы при step >= horizon)
    mean_vs_bench: float  # событие минус бенчмарк (BTC), усреднено по датам
    mean_net: float  # mean_event минус круг комиссий и проскальзывания
    by_year: dict[int, tuple[int, float]]  # год -> (дат, средний избыток)
    by_symbol: pd.DataFrame  # events, mean_fwd, contrib; по убыванию |вклада|


def forward_returns(closes: pd.DataFrame, horizon: int) -> pd.DataFrame:
    return closes.shift(-horizon) / closes - 1


def _t(x: pd.Series) -> float:
    if len(x) < 2 or x.std(ddof=1) == 0:
        return math.nan
    return float(x.mean() / (x.std(ddof=1) / math.sqrt(len(x))))


def event_study(
    events: pd.DataFrame,
    closes: pd.DataFrame,
    member: pd.DataFrame,
    horizon: int,
    step: int,
    bench: str,
) -> StudyResult:
    fwd = forward_returns(closes, horizon)
    dates = closes.index[::step]
    fwd, ev, mem = fwd.loc[dates], events.loc[dates], member.loc[dates]
    valid = mem & fwd.notna()
    hit = ev & valid
    keep = hit.any(axis=1)
    fwd, hit, valid = fwd[keep], hit[keep], valid[keep]

    event_mean = fwd.where(hit).mean(axis=1)
    excess = event_mean - fwd.where(valid).mean(axis=1)
    vs_bench = (event_mean - fwd[bench]).dropna() if bench in fwd else pd.Series(dtype=float)
    per_event = fwd.where(hit).stack()
    mean_event = float(per_event.mean()) if len(per_event) else math.nan

    by_year = {
        int(y): (len(g), float(g.mean())) for y, g in excess.groupby(excess.index.year)
    }
    counts = hit.sum()
    by_symbol = pd.DataFrame({"events": counts, "mean_fwd": fwd.where(hit).mean()})
    by_symbol = by_symbol[by_symbol["events"] > 0]
    by_symbol = by_symbol.assign(contrib=by_symbol["events"] * by_symbol["mean_fwd"])
    by_symbol = by_symbol.sort_values("contrib", key=abs, ascending=False)
    return StudyResult(
        n_dates=int(keep.sum()),
        n_events=int(hit.to_numpy().sum()),
        mean_event=mean_event,
        mean_excess=float(excess.mean()),
        t_excess=_t(excess),
        mean_vs_bench=float(vs_bench.mean()) if len(vs_bench) else math.nan,
        mean_net=mean_event - ROUND_TRIP_COST,
        by_year=by_year,
        by_symbol=by_symbol,
    )
