import html
from dataclasses import dataclass
from datetime import datetime, tzinfo

from invest_agent.models import SignalCandidate, Sizing
from invest_agent.stats import BookStats, Criteria

LIMIT = 4096
_BUDGET = {
    "budget_80": "Расход на Claude API достиг 80% месячного лимита. Объяснения сигналов теперь пишет дешёвая модель.",
    "budget_100": "Месячный лимит Claude API исчерпан (100%). Claude не вызывается, новые сигналы получают статус REVIEW и не отправляются.",
}  # fmt: skip


@dataclass(frozen=True)
class OpenView:
    symbol: str
    status: str  # "pending" | "open"
    entry_price: float | None
    stop: float
    target: float
    mark: float | None


def _e(s: object) -> str:
    return html.escape(str(s))


def _p(x: float) -> str:
    if x >= 1:
        return f"{x:.4f}".rstrip("0").rstrip(".")
    return f"{x:.8f}".rstrip("0").rstrip(".")


def _money(x: float) -> str:
    return f"{x:,.0f}".replace(",", " ")


def _usd_kzt(usd: float, usd_kzt: float | None) -> str:
    s = f"${usd:,.2f}".replace(",", " ")
    return s if usd_kzt is None else f"{s} ({_money(usd * usd_kzt)} ₸)"


def _utc_label(dt: datetime) -> str:
    off = dt.utcoffset()
    h = 0.0 if off is None else off.total_seconds() / 3600
    return f"UTC{h:+g}" if h else "UTC"


def _local(dt: datetime, tz: tzinfo | None) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("naive datetime")
    return dt.astimezone(tz) if tz is not None else dt


def _when(dt: datetime) -> str:
    return f"{dt:%d.%m %H:%M} ({_utc_label(dt)})"


def _yn(ok: bool) -> str:
    return "да" if ok else "нет"


def _pct(x: float) -> str:
    return f"{x * 100:+.1f}%"


def signal_message(
    sig: SignalCandidate,
    sizing: Sizing,
    equity: float,
    risk_pct: float,
    explanation: str,
    expires_at: datetime,
    usd_kzt: float | None,
    tz: tzinfo,
) -> str:
    """risk_pct — доля счёта (0.01 = 1%). Урезается только объяснение; итог ≤ 4096 символов."""
    risk = _usd_kzt(sizing.risk_usdt, usd_kzt)
    head = (
        f"<b>{_e(sig.symbol)}</b> — покупка (спот)\n"
        f"Зона входа: {_p(sig.entry_low)} – {_p(sig.entry_high)}\n"
        f"Стоп: {_p(sig.stop)}\n"
        f"Цель: {_p(sig.target)}\n"
        f"Сумма: {_usd_kzt(sizing.notional, usd_kzt)}\n"
        f"Риск: {risk} ({risk_pct * 100:g}% счёта)\n\n"
    )
    tail = (
        f"\n\nДействует до {_when(_local(expires_at, tz))}. "
        "Если цена не вошла в зону — сигнал отменяется.\n"
        "Не забудьте поставить OCO-ордер (стоп + цель) на Binance."
    )
    room = LIMIT - len(head) - len(tail)
    out, used, cut = [], 0, False
    for ch in explanation:
        esc = html.escape(ch)
        if used + len(esc) > room:
            cut = True
            break
        out.append(esc)
        used += len(esc)
    if cut:
        while out and used + 1 > room:
            used -= len(out.pop())
        out.append("…")
    return head + "".join(out) + tail


def window_header(now: datetime, n_signals: int, notes: list[str]) -> str:
    title = f"<b>Окно {_when(now)}</b>\n"
    body = "Сигналов нет." if n_signals == 0 else f"Сигналов: {n_signals}."
    return title + body + "".join(f"\n{_e(n)}" for n in notes)


def evening_summary(
    now: datetime,
    open_positions: list[OpenView],
    equity: float,
    dd_state: str,
    usd_kzt: float | None,
    notes: list[str],
) -> str:
    lines = [f"<b>Вечерняя сводка {_when(now)}</b>"]
    if not open_positions:
        lines.append("Открытых и ожидающих сделок нет.")
    for p in open_positions:
        if p.status == "open" and p.entry_price:
            pnl = f", сейчас {_pct(p.mark / p.entry_price - 1)}" if p.mark else ""
            state = f"открыта по {_p(p.entry_price)}{pnl}"
        else:
            state = "ждёт входа"
        lines.append(f"{_e(p.symbol)}: {state}; стоп {_p(p.stop)}, цель {_p(p.target)}")
    lines.append(f"Счёт: {_usd_kzt(equity, usd_kzt)}, режим {_e(dd_state)}")
    lines += [_e(n) for n in notes]
    return "\n".join(lines)


def _book_block(title: str, s: BookStats, usd_kzt: float | None) -> str:
    pf = "∞" if s.profit_factor == float("inf") else f"{s.profit_factor:.2f}"
    return (
        f"<b>{title}</b>\n"
        f"Сигналов {s.n_signals}, вошли {s.n_entered}, отменены {s.n_expired}\n"
        f"Прибыльных {s.win_rate * 100:.0f}%, средняя прибыль {s.avg_win:.2f}, "
        f"средний убыток {s.avg_loss:.2f}, profit factor {pf}\n"
        f"Итого {_usd_kzt(s.net_usdt, usd_kzt)}, макс. просадка {s.max_dd * 100:.1f}%"
    )


def weekly_message(
    stats_main: BookStats,
    stats_research: BookStats,
    criteria: Criteria,
    costs: float,
    btc_return: float | None = None,
    usd_kzt: float | None = None,
) -> str:
    """costs — расходы на сервер и API за неделю, $."""
    lines = [
        "<b>Недельная статистика</b>",
        _book_block("Основной портфель (одобрено Claude)", stats_main, usd_kzt),
        _book_block("Исследовательский (все сигналы кода)", stats_research, usd_kzt),
        f"Расходы на сервер и API: {_usd_kzt(costs, usd_kzt)}",
        f"Результат основного после расходов: {_usd_kzt(stats_main.net_usdt - costs, usd_kzt)}",
    ]
    if btc_return is not None:
        lines.append(f"Купить и держать BTC за период: {_pct(btc_return)}")
    mark = _yn
    d = criteria.details
    lines.append(
        "<b>Критерии перехода на деньги</b>\n"
        f"≥ 60 дней: {mark(criteria.days_ok)}; ≥ 100 сделок: {mark(criteria.trades_ok)}; "
        f"прибыль: {mark(criteria.profit_ok)}; лучше BTC: {mark(criteria.beats_btc_ok)}; "
        f"просадка ≤ 15%: {mark(criteria.dd_ok)}\n"
        + ("Критерии выполнены." if criteria.all_ok else "Критерии не выполнены.")
        + (f" (сделок {d['closed']}, дней {d['days']:.0f})" if "closed" in d else "")
    )
    return "\n".join(lines)


def halt_message(analysis: dict) -> str:
    lines = [
        "<b>Остановка: просадка достигла −15%</b>",
        "Новых сигналов не будет. Возобновление — только командой /resume (начнётся новый бумажный этап).",
    ]
    if analysis.get("by_strategy"):
        lines.append("Убытки по стратегиям: " + ", ".join(
            f"{_e(k)} {v:.2f}" for k, v in analysis["by_strategy"].items()))  # fmt: skip
    if analysis.get("by_symbol"):
        lines.append("Убытки по монетам: " + ", ".join(
            f"{_e(k)} {v:.2f}" for k, v in analysis["by_symbol"].items()))  # fmt: skip
    if analysis.get("worst"):
        lines.append("Худшие сделки:")
        lines += [
            f"{_e(w['symbol'])} ({_e(w['strategy'])}): {w['pnl_usdt']:.2f}"
            for w in analysis["worst"]
        ]
    if analysis.get("open"):
        lines.append("Открытые позиции не закрыты, стопы:")
        lines += [f"{_e(o['symbol'])}: стоп {_p(o['stop'])}" for o in analysis["open"]]
    return "\n".join(lines)


def reduced_message() -> str:
    return (
        "<b>Просадка −10%</b>: риск на сделку снижен до 0,5% счёта. "
        "Вернётся к 1%, когда счёт достигнет прежнего максимума."
    )


def recovered_message() -> str:
    return "Счёт вернулся к прежнему максимуму: риск на сделку снова 1%."


def failure_message(window_at: datetime, reason: str, tz: tzinfo | None = None) -> str:
    return (
        f"<b>Окно {_when(_local(window_at, tz))}</b>\nСигналы не посчитаны, причина: {_e(reason)}"
    )


def budget_message(event: str) -> str:
    return _e(_BUDGET.get(event, f"Событие бюджета: {event}"))
