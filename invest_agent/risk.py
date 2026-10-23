from dataclasses import dataclass, replace
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Literal

from invest_agent.config import (
    DD_HALT,
    DD_REDUCED,
    MAX_OPEN,
    MAX_POSITION_FRAC,
    MAX_TOTAL_RISK,
    MIN_RR,
    RISK_NORMAL,
    RISK_REDUCED,
)
from invest_agent.models import Rejection, SignalCandidate, Sizing, SymbolRules

DDState = Literal["NORMAL", "REDUCED", "HALTED"]
_EPS = Decimal("1e-9")


@dataclass(frozen=True)
class OpenRisk:
    symbol: str
    risk_usdt: float
    notional: float


def _round_step(x: float, step: float, mode: str) -> float:
    ratio = Decimal(str(x)) / Decimal(str(step))
    ratio = ratio + _EPS if mode == ROUND_FLOOR else ratio - _EPS
    return float(ratio.to_integral_value(mode) * Decimal(str(step)))


def floor_step(x: float, step: float) -> float:
    return _round_step(x, step, ROUND_FLOOR)


def ceil_step(x: float, step: float) -> float:
    return _round_step(x, step, ROUND_CEILING)


def size_position(
    sig: SignalCandidate,
    equity: float,
    risk_pct: float,
    open_: list[OpenRisk],
    rules: SymbolRules,
) -> Sizing | Rejection:
    unit_risk = sig.entry_high - sig.stop
    if unit_risk <= 0:
        return Rejection("stop>=entry")
    if sig.stop <= 0 or sig.entry_low > sig.entry_high or sig.entry_low <= sig.stop:
        # округление до шага цены может вывернуть зону (0,25×ATR меньше шага)
        return Rejection("entry zone invalid after rounding")
    if (sig.target - sig.entry_high) < MIN_RR * unit_risk * (1 - 1e-12):
        return Rejection("rr<1.5")
    if any(o.symbol == sig.symbol for o in open_):
        return Rejection("symbol already open")
    if len(open_) >= MAX_OPEN:
        return Rejection("max open positions")
    remaining = MAX_TOTAL_RISK * equity - sum(o.risk_usdt for o in open_)
    if remaining <= 0:
        return Rejection("total risk limit")
    risk = min(risk_pct * equity, remaining)
    qty = risk / unit_risk
    qty = min(qty, MAX_POSITION_FRAC * equity / sig.entry_high)
    qty = floor_step(qty, rules.step_size)
    notional = qty * sig.entry_high
    if qty <= 0 or notional < rules.min_notional:
        return Rejection("below min notional")
    return Sizing(qty=qty, notional=notional, risk_usdt=qty * unit_risk)


def round_prices(sig: SignalCandidate, rules: SymbolRules) -> SignalCandidate:
    t = rules.tick_size
    entry_high = floor_step(sig.entry_high, t)
    stop = floor_step(sig.stop, t)
    target = floor_step(sig.target, t)
    # цель ровно на MIN_RR после округления стопа/цели вниз дала бы rr чуть меньше 1,5
    if sig.target - sig.entry_high >= MIN_RR * (sig.entry_high - sig.stop) * (1 - 1e-12):
        target = max(target, ceil_step(entry_high + MIN_RR * (entry_high - stop), t))
    return replace(
        sig,
        entry_low=ceil_step(sig.entry_low, t),
        entry_high=entry_high,
        stop=stop,
        target=target,
    )


def step_drawdown(state: DDState, equity: float, peak: float) -> tuple[DDState, list[str]]:
    if state == "HALTED":
        return state, []
    dd = 1 - equity / peak + 1e-12
    if dd >= DD_HALT:
        return "HALTED", ["halted"]
    if state == "NORMAL":
        return ("REDUCED", ["reduced"]) if dd >= DD_REDUCED else ("NORMAL", [])
    if equity >= peak:
        return "NORMAL", ["recovered"]
    return "REDUCED", []


def risk_pct_for(state: DDState) -> float:
    return RISK_REDUCED if state == "REDUCED" else RISK_NORMAL
