from dataclasses import dataclass

from invest_agent.backtest.engine import BacktestResult

MIN_TRADES = 30
SUBPERIODS = 3
MIN_NONNEG_SUBPERIODS = 2


@dataclass(frozen=True)
class GateVerdict:
    passed: bool
    reasons: list[str]


def subperiod_pnls(result: BacktestResult) -> list[float]:
    """[start, end) делится на 3 равных отрезка; сделка относится к отрезку по exit_time."""
    span = (result.end - result.start) / SUBPERIODS
    sums = [0.0] * SUBPERIODS
    for t in result.trades:
        i = int((t.exit_time - result.start) / span)
        sums[min(max(i, 0), SUBPERIODS - 1)] += t.pnl_usdt
    return sums


def gate(result: BacktestResult) -> GateVerdict:
    reasons: list[str] = []
    net = result.net_pnl()
    if net <= 0:
        reasons.append(f"результат после комиссий не положительный: {net:.2f} USDT")
    if len(result.trades) < MIN_TRADES:
        reasons.append(f"сделок {len(result.trades)} < {MIN_TRADES}")
    subs = subperiod_pnls(result)
    nonneg = sum(1 for s in subs if s >= 0)
    if nonneg < MIN_NONNEG_SUBPERIODS:
        shown = ", ".join(f"{s:.2f}" for s in subs)
        reasons.append(f"неотрицательных под-периодов {nonneg} из {SUBPERIODS} ({shown})")
    return GateVerdict(not reasons, reasons)
