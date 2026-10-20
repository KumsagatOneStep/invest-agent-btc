from dataclasses import dataclass
from typing import Literal

import anthropic
from pydantic import BaseModel

from invest_agent.llm.budget import Budget
from invest_agent.models import SignalCandidate

SYSTEM = (
    "Ты скептичный риск-аналитик. Тебе дан сигнал на покупку криптовалюты на споте, "
    "рассчитанный кодом. Сначала разбери доводы «за» и «против», затем реши: approve или reject. "
    "Отклоняй, если против весомее. Числа сигнала (вход, стоп, цель) не предлагай и не меняй. "
    "Отвечай по-русски, кратко."
)


@dataclass(frozen=True)
class FilterContext:
    signal: SignalCandidate
    indicators: dict[str, float]
    btc_trend: str
    open_positions: list[str]
    recent_results: list[str]


class FilterVerdict(BaseModel):
    decision: Literal["approve", "reject"]
    reason: str
    pros: list[str]
    cons: list[str]


@dataclass(frozen=True)
class FilterResult:
    status: Literal["approved", "rejected", "review"]
    verdict: FilterVerdict | None
    error: str | None


def describe(ctx: FilterContext) -> str:
    s = ctx.signal
    return (
        f"Монета: {s.symbol}, стратегия: {s.strategy}\n"
        f"Зона входа: {s.entry_low}–{s.entry_high}, стоп: {s.stop}, цель: {s.target}\n"
        f"Индикаторы: {ctx.indicators}\n"
        f"Тренд BTC: {ctx.btc_trend}\n"
        f"Открытые позиции: {', '.join(ctx.open_positions) or 'нет'}\n"
        f"Недавние итоги стратегии: {', '.join(ctx.recent_results) or 'нет'}"
    )


def filter_signal(ctx: FilterContext, client: anthropic.Anthropic, budget: Budget) -> FilterResult:
    model = budget.model_for("filter")
    if model is None:
        return FilterResult("review", None, "budget")
    try:
        resp = client.messages.parse(
            model=model,
            max_tokens=2048,
            output_format=FilterVerdict,
            system=SYSTEM,
            messages=[{"role": "user", "content": describe(ctx)}],
        )
    except Exception as e:  # noqa: BLE001 - любой сбой SDK/разбора -> REVIEW
        return FilterResult("review", None, f"{type(e).__name__}: {e}")
    budget.record(model, "filter", resp.usage)
    if resp.stop_reason == "refusal":
        return FilterResult("review", None, "refusal")
    verdict = resp.parsed_output
    if verdict is None:
        return FilterResult("review", None, "unparsed")
    status = "approved" if verdict.decision == "approve" else "rejected"
    return FilterResult(status, verdict, None)
