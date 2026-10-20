import json
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone

import anthropic
import httpx

from invest_agent.backtest.engine import D1_LOOKBACK, H4_LOOKBACK
from invest_agent.config import RISK_NORMAL, Settings
from invest_agent.data.binance import BinanceClient, BinanceError
from invest_agent.data.candles import CandleStore, last_marks, to_frame
from invest_agent.data.fx import usd_kzt
from invest_agent.execution import next_window, simulate
from invest_agent.indicators import ema
from invest_agent.journal import Journal
from invest_agent.llm.budget import Budget
from invest_agent.llm.explain import explain_signal
from invest_agent.llm.filter import FilterContext, filter_signal
from invest_agent.messages import (
    OpenView,
    budget_message,
    evening_summary,
    failure_message,
    halt_message,
    recovered_message,
    reduced_message,
    signal_message,
    weekly_message,
    window_header,
)
from invest_agent.models import Rejection, SignalCandidate, SymbolRules
from invest_agent.risk import risk_pct_for, round_prices, size_position, step_drawdown
from invest_agent.stats import book_stats, btc_hold_return, costs, golive_check, halt_analysis
from invest_agent.strategies.base import Strategy
from invest_agent.strategies.registry import load_approved
from invest_agent.universe import candidate_symbols, select_universe

log = logging.getLogger(__name__)
BOOKS = ("main", "research")
INTERVALS = ("1h", "4h", "1d")
RECENT = 5
UNIVERSE_SPAN = timedelta(days=400)  # дневная история для выбора монет месяца
MAX_SKIPPED = 20  # больше сбойных пар при обновлении монет — считаем, что Binance лежит
NO_STRATEGIES = "Нет одобренных стратегий — см. отчёт бэктеста."
_STATUS_RU = {"open": "открыта", "pending": "ждёт входа"}


@dataclass
class Deps:
    settings: Settings
    conn: sqlite3.Connection
    journal: Journal
    store: CandleStore
    binance: BinanceClient
    anthropic_client: anthropic.Anthropic
    now_fn: Callable[[], datetime] = lambda: datetime.now(UTC)
    http: httpx.Client = field(default_factory=lambda: httpx.Client(timeout=15))  # курс и пинг


class WindowError(Exception):
    """Ожидаемая причина, по которой окно не посчитано (текст — для пользователя)."""


@dataclass
class _Market:
    rules: dict[str, SymbolRules]
    universe: list[str]
    new_universe: bool
    notes: list[str]
    usd_kzt: float | None
    skipped: set[str] = field(default_factory=set)  # без свежих свечей в этом окне


@dataclass
class _Result:
    n_sent: int
    notes: list[str]
    body: list[str]
    budget_events: list[str]
    tail: list[str]


def run_window(window_at: datetime, deps: Deps) -> list[str]:
    """Одно окно сигналов целиком; возвращает тексты сообщений по порядку ([] — окно уже было)."""
    j = deps.journal
    if not j.begin_run(window_at):
        return []
    tz = _tz(deps.settings)
    try:
        market = _prepare(window_at, deps)
        with deps.conn:
            res = _process(window_at, deps, market, tz)
            j.mark_run_ok(window_at)  # статус в той же транзакции: повтор не задвоит окно
    except Exception as e:  # любой сбой окна -> откат и сообщение пользователю
        log.exception("window %s failed", window_at)
        if deps.conn.in_transaction:
            deps.conn.rollback()
        reason = _reason(e)
        j.finish_run(window_at, "failed", reason)
        return [failure_message(window_at, reason, tz)]
    _ping(deps)
    header = window_header(window_at.astimezone(tz), res.n_sent, res.notes)
    return [header, *res.body, *(budget_message(e) for e in res.budget_events), *res.tail]


def _tz(settings: Settings) -> timezone:
    return timezone(timedelta(hours=settings.tz_offset_hours))


def _reason(e: Exception) -> str:
    if isinstance(e, BinanceError):
        return f"Binance недоступен ({e})"
    if isinstance(e, WindowError):
        return str(e)
    return f"внутренняя ошибка ({type(e).__name__}: {e})"


def _ping(deps: Deps) -> None:
    url = deps.settings.healthcheck_url
    if not url:
        return
    try:
        deps.http.get(url, timeout=10)
    except Exception:  # пинг не влияет на окно
        log.warning("healthcheck ping failed", exc_info=True)


def _active_symbols(j: Journal) -> set[str]:
    return {p.symbol for b in BOOKS for p in j.active_positions(b)}


def _prepare(window_at: datetime, deps: Deps) -> _Market:
    """Сетевая часть до транзакции окна: правила пар, свечи, вселенная, курс."""
    j, store, binance = deps.journal, deps.store, deps.binance
    now = deps.now_fn()
    rules = binance.symbol_rules()
    notes: list[str] = []
    critical = {"BTCUSDT"} | _active_symbols(j)  # без них окно не считается

    row = deps.conn.execute(
        "SELECT symbols_json FROM coins WHERE month=?", (f"{window_at:%Y-%m}",)
    ).fetchone()
    if row is not None:
        universe, new_universe = json.loads(row[0]), False
    else:
        candidates = candidate_symbols(rules)
        failed: list[str] = []
        for s in sorted(candidates, key=lambda x: (x not in critical, x)):
            try:
                store.refresh(binance, s, "1d", now)
            except BinanceError:
                if s in critical:
                    raise
                log.warning("%s: дневные свечи не обновлены, пара пропущена", s, exc_info=True)
                failed.append(s)
                if len(failed) > MAX_SKIPPED:
                    raise WindowError(f"Binance: не обновились свечи {len(failed)} пар") from None
        since = window_at - UNIVERSE_SPAN
        daily = {s: store.get(s, "1d", since, window_at) for s in candidates if s not in failed}
        universe, new_universe = select_universe(rules, daily, window_at), True
        notes.append(_universe_note(_previous_universe(deps.conn, window_at), universe))
        if failed:
            notes.append(
                f"Монеты на месяц: пропущено пар из-за ошибок Binance: {len(failed)}"
                f" ({', '.join(failed[:5])}{', …' if len(failed) > 5 else ''})"
            )

    skipped: set[str] = set()
    for s in sorted((set(universe) | critical) & rules.keys()):
        try:
            for interval in INTERVALS:
                store.refresh(binance, s, interval, now)
        except BinanceError:
            if s in critical:
                raise
            log.warning("%s: свечи не обновлены, монета пропущена в окне", s, exc_info=True)
            skipped.add(s)
    if skipped:
        notes.append(f"Без свежих свечей, пропущены в этом окне: {', '.join(sorted(skipped))}")

    rate = usd_kzt(now, deps.conn, deps.http)
    if rate is None and any(_needs_stage(j, b) for b in BOOKS):
        raise WindowError("нет курса ₸/$ для старта бумажного этапа")
    return _Market(rules, universe, new_universe, notes, rate, skipped)


def _previous_universe(conn: sqlite3.Connection, window_at: datetime) -> list[str] | None:
    row = conn.execute(
        "SELECT symbols_json FROM coins WHERE month<? ORDER BY month DESC LIMIT 1",
        (f"{window_at:%Y-%m}",),
    ).fetchone()
    return json.loads(row[0]) if row else None


def _universe_note(prev: list[str] | None, cur: list[str]) -> str:
    if prev is None:
        return f"Монеты на месяц: {', '.join(cur)}"
    added = [s for s in cur if s not in prev]
    removed = [s for s in prev if s not in cur]
    if not added and not removed:
        return "Монеты на месяц: без изменений."
    parts = []
    if added:
        parts.append(f"добавлены {', '.join(added)}")
    if removed:
        parts.append(f"выбыли {', '.join(removed)}")
    return f"Монеты на месяц: {'; '.join(parts)}"


def _needs_stage(j: Journal, book: str) -> bool:
    b = j.book(book)
    return b.stage == 0 or b.start_equity <= 0


def _process(window_at: datetime, deps: Deps, m: _Market, tz: timezone) -> _Result:
    """Все записи окна; вызывается внутри `with conn:`."""
    j, s, store = deps.journal, deps.settings, deps.store
    windows = s.window_times_utc()
    notes = list(m.notes)
    body: list[str] = []
    tail: list[str] = []

    for book in BOOKS:
        if _needs_stage(j, book):
            assert m.usd_kzt is not None
            j.start_new_stage(book, s.start_capital_kzt / m.usd_kzt, window_at)
    if m.new_universe:
        deps.conn.execute(
            "INSERT OR REPLACE INTO coins (month, symbols_json) VALUES (?, ?)",
            (f"{window_at:%Y-%m}", json.dumps(m.universe)),
        )

    # 3. Исходы активных позиций обоих портфелей
    delisted: set[str] = set()
    for book in BOOKS:
        for p in j.active_positions(book):
            sig = j.signal(p.signal_id)
            if p.symbol not in m.rules:
                if p.symbol not in delisted:
                    delisted.add(p.symbol)
                    notes.append(
                        f"{p.symbol}: нет в списке TRADING (возможно, временная остановка),"
                        " позиция остаётся"
                        f" ({_STATUS_RU[p.status]}, стоп {sig.stop:g}) — проверьте вручную."
                    )
                continue
            candles = store.get(p.symbol, "1h", sig.created_at, window_at)
            j.apply_outcome(p.id, simulate(sig, candles, next_window(sig.created_at, windows)))

    # 4. Счёт, максимум и ступень просадки
    marks = last_marks(store, _active_symbols(j), window_at)
    equity: dict[str, float] = {}
    for book in BOOKS:
        b = j.book(book)
        equity[book] = j.equity(book, marks)
        peak = max(b.peak, equity[book])
        state, events = b.dd_state, []
        if book == "main":
            state, events = step_drawdown(b.dd_state, equity[book], peak)
        j.update_book(book, peak=peak, dd_state=state)
        for ev in events:
            if ev == "reduced":
                body.append(reduced_message())
            elif ev == "recovered":
                body.append(recovered_message())
            elif ev == "halted":
                body.append(halt_message(halt_analysis(j, "main")))

    # 5–6. Кандидаты и два портфеля
    budget = Budget(j, s, deps.now_fn())
    n_sent = 0
    strategies = load_approved()
    if not strategies:
        notes.append(NO_STRATEGIES)
    else:
        main, research = j.book("main"), j.book("research")
        blocked = "пауза" if main.paused else "остановка" if main.dd_state == "HALTED" else None
        risk_pct = risk_pct_for(main.dd_state)
        expires_at = next_window(window_at, windows)
        btc_trend = _btc_trend(store, window_at)
        for strategy, sig in _candidates(strategies, m, store, window_at):
            rules = m.rules[sig.symbol]
            sid = j.add_signal(sig, strategy.version())
            r_size = size_position(sig, equity["research"], RISK_NORMAL, j.open_risk("research"), rules)
            j.add_position(sid, "research", research.stage, r_size)
            if blocked:
                j.set_claude(sid, "skipped", blocked, None)
                continue
            sizing = size_position(sig, equity["main"], risk_pct, j.open_risk("main"), rules)
            if isinstance(sizing, Rejection):
                j.set_claude(sid, "skipped", f"лимиты: {sizing.reason}", None)
                continue
            ctx = FilterContext(
                sig, sig.features, btc_trend,
                [p.symbol for p in j.active_positions("main")], _recent(j, strategy.name),
            )  # fmt: skip
            fr = filter_signal(ctx, deps.anthropic_client, budget)
            if fr.status == "approved" and fr.verdict is not None:
                text = explain_signal(ctx, fr.verdict, deps.anthropic_client, budget)
                j.set_claude(sid, "approved", fr.verdict.reason, text)
                j.add_position(sid, "main", main.stage, sizing)
                j.mark_sent(sid)
                body.append(
                    signal_message(
                        sig, sizing, equity["main"], risk_pct, text, expires_at, m.usd_kzt, tz
                    )
                )
                n_sent += 1
            elif fr.status == "rejected" and fr.verdict is not None:
                j.set_claude(sid, "rejected", fr.verdict.reason, None)
            else:
                j.set_claude(sid, "review", fr.error, None)

    # 7. События бюджета (сообщения — после коммита)
    budget_events = budget.threshold_events()

    # 8. Вечерняя сводка и недельная статистика
    local = window_at.astimezone(tz)
    if window_at.astimezone(UTC).time().replace(second=0, microsecond=0) == max(windows):
        main = j.book("main")
        views = []
        for p in j.active_positions("main"):
            sig = j.signal(p.signal_id)
            views.append(
                OpenView(p.symbol, p.status, p.entry_price, sig.stop, sig.target, marks.get(p.symbol))
            )
        tail.append(
            evening_summary(
                local, views, equity["main"], main.dd_state, m.usd_kzt, _review_notes(j, window_at)
            )
        )
        if local.weekday() == 6:
            since, until = window_at - timedelta(days=7), window_at + timedelta(microseconds=1)
            tail.append(
                weekly_message(
                    book_stats(j, "main", since, until),
                    book_stats(j, "research", since, until),
                    golive_check(j, store, window_at),
                    costs(j, s, since, until),
                    btc_hold_return(store, since, window_at),
                    m.usd_kzt,
                )
            )
    return _Result(n_sent, notes, body, budget_events, tail)


def _candidates(
    strategies: list[Strategy], m: _Market, store: CandleStore, window_at: datetime
) -> list[tuple[Strategy, SignalCandidate]]:
    out = []
    for sym in m.universe:
        if sym not in m.rules:
            log.warning("%s: в списке монет месяца, но нет в правилах Binance — пропущена", sym)
            continue
        if sym in m.skipped:
            continue
        h4 = to_frame(store.get(sym, "4h", window_at - timedelta(hours=4 * H4_LOOKBACK), window_at))
        d1 = to_frame(store.get(sym, "1d", window_at - timedelta(days=D1_LOOKBACK), window_at))
        for strategy in strategies:
            raw = strategy.evaluate(sym, h4, d1, window_at)
            if raw is not None:
                out.append((strategy, round_prices(raw, m.rules[sym])))
    return out


def _btc_trend(store: CandleStore, window_at: datetime) -> str:
    d1 = to_frame(store.get("BTCUSDT", "1d", window_at - timedelta(days=D1_LOOKBACK), window_at))
    if len(d1) < 50:
        return "нет данных"
    close, avg = float(d1["close"].iloc[-1]), float(ema(d1["close"], 50).iloc[-1])
    direction = "восходящий" if close > avg else "нисходящий"
    return f"{direction} (дневное закрытие {close:g}, EMA50 {avg:g})"


def _recent(j: Journal, strategy: str) -> list[str]:
    rows = j.conn.execute(
        "SELECT s.symbol, p.status, p.pnl_usdt FROM positions p JOIN signals s ON s.id=p.signal_id"
        " WHERE p.book='research' AND s.strategy=? AND p.status IN ('stop','target','time')"
        " ORDER BY p.exit_time DESC, p.id DESC LIMIT ?",
        (strategy, RECENT),
    ).fetchall()
    return [f"{sym} {status} {pnl:+.2f}" for sym, status, pnl in rows]


def _review_notes(j: Journal, window_at: datetime) -> list[str]:
    since = (window_at - timedelta(days=1)).astimezone(UTC).isoformat()
    n = j.conn.execute(
        "SELECT COUNT(*) FROM signals WHERE claude_status='review' AND created_at>?", (since,)
    ).fetchone()[0]
    return [f"Сигналов REVIEW за сутки (Claude недоступен, не отправлены): {n}"] if n else []
