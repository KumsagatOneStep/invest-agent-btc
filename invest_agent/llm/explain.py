import anthropic

from invest_agent.llm.budget import Budget
from invest_agent.llm.filter import FilterContext, FilterVerdict, describe

SYSTEM = (
    "Ты объясняешь трейдеру сигнал на покупку, который уже одобрен. Напиши 2–3 строки по-русски: "
    "«Почему:» и «Против:». Не называй цены и уровни (вход, стоп, цель) и не придумывай числа."
)
FALLBACK_BETA = "server-side-fallback-2026-07-01"


def verdict_text(verdict: FilterVerdict) -> str:
    return f"Почему: {'; '.join(verdict.pros) or verdict.reason}\nПротив: {'; '.join(verdict.cons) or '—'}"


def explain_signal(
    ctx: FilterContext, verdict: FilterVerdict, client: anthropic.Anthropic, budget: Budget
) -> str:
    model = budget.model_for("explain")
    if model is None:
        return verdict_text(verdict)
    extra = {}
    if model == budget.settings.strong_model:
        extra = {"betas": [FALLBACK_BETA], "fallbacks": "default"}
    prompt = f"{describe(ctx)}\n\nВывод фильтра: {verdict.reason}\nЗа: {verdict.pros}\nПротив: {verdict.cons}"
    try:
        resp = client.beta.messages.create(
            model=model,
            max_tokens=1024,
            system=SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            **extra,
        )
    except Exception:  # noqa: BLE001 - любой сбой -> текст из вердикта
        return verdict_text(verdict)
    budget.record(model, "explain", resp.usage)
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
    if resp.stop_reason == "refusal" or not text:
        return verdict_text(verdict)
    return text
