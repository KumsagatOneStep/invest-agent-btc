import json
import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import ClassVar

import anthropic
import httpx
import pytest
import respx

from invest_agent import cycle
from invest_agent.config import Settings
from invest_agent.cycle import Deps, run_window
from invest_agent.data.binance import BinanceClient
from invest_agent.data.candles import CandleStore
from invest_agent.journal import Journal, migrate
from invest_agent.llm.filter import FilterVerdict
from invest_agent.models import Outcome, SignalCandidate, Sizing

BASE = "https://data-api.binance.vision"
FX_URL = "https://open.er-api.com/v6/latest/USD"
PING = "https://hc.example/ping"
MORNING = datetime(2026, 10, 20, 3, 0, tzinfo=UTC)  # вторник 08:00 UTC+5
SUNDAY_EVENING = datetime(2026, 10, 25, 15, 0, tzinfo=UTC)  # воскресенье 20:00 UTC+5
STEP = {"1h": 3_600_000, "4h": 4 * 3_600_000, "1d": 86_400_000}
SPAN = {"1h": timedelta(days=30), "4h": timedelta(days=100), "1d": timedelta(days=400)}
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
APPROVE = FilterVerdict(decision="approve", reason="тренд", pros=["тренд вверх"], cons=["BTC"])
REJECT = FilterVerdict(decision="reject", reason="слабый объём", pros=[], cons=["объём"])
EXPLANATION = "Почему: пробой. Против: рынок устал, цель 999999."


def usage():
    return SimpleNamespace(input_tokens=1000, output_tokens=500)


class FakeClaude:
    """Подставной anthropic-клиент: messages.parse (фильтр) и beta.messages.create (объяснение)."""

    def __init__(self, verdict=APPROVE):
        self.verdict = verdict
        self.calls: list[str] = []
        self.messages = SimpleNamespace(parse=self._parse)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _parse(self, **kw):
        self.calls.append("parse")
        if isinstance(self.verdict, Exception):
            raise self.verdict
        return SimpleNamespace(parsed_output=self.verdict, stop_reason="end_turn", usage=usage())

    def _create(self, **kw):
        self.calls.append("create")
        block = SimpleNamespace(type="text", text=EXPLANATION)
        return SimpleNamespace(content=[block], stop_reason="end_turn", usage=usage())


class FakeStrategy:
    name = "trend"
    params: ClassVar[dict[str, float]] = {}

    def __init__(self, symbols=("SOLUSDT",)):
        self.symbols = symbols

    def version(self) -> str:
        return "v-test"

    def evaluate(self, symbol, h4, d1, now):
        assert not h4.empty and not d1.empty and h4.index.max() <= now
        if symbol not in self.symbols:
            return None
        return SignalCandidate(symbol, self.name, now, 99.0, 100.0, 95.0, 110.0, {"rsi": 40.0})


FILTERS = [
    {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
    {"filterType": "LOT_SIZE", "stepSize": "0.001"},
    {"filterType": "NOTIONAL", "minNotional": "5"},
]


def exchange_info(symbols=SYMBOLS):
    return {
        "symbols": [
            {
                "symbol": s,
                "status": "TRADING",
                "quoteAsset": "USDT",
                "isSpotTradingAllowed": True,
                "filters": FILTERS,
            }
            for s in symbols
        ]
    }


def klines_handler(window_at: datetime, fail: dict[str, int] | None = None, trend: bool = False):
    """Синтетические свечи: цена ~100, ряд от window_at − SPAN до запрошенного endTime.

    trend=True: цена равномерно растёт (+0.5 в сутки) — каждая 4h свеча пробивает канал.
    """
    fail = fail or {}
    base = int((window_at - timedelta(days=400)).timestamp() * 1000)

    def price(ms: int) -> str:
        return f"{100 + (ms - base) / 86_400_000 * 0.5:.6f}"

    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.params
        symbol, interval = p["symbol"], p["interval"]
        if symbol in fail:
            return httpx.Response(fail[symbol])
        step = STEP[interval]
        first = int((window_at - SPAN[interval]).timestamp() * 1000) // step * step
        start = max(int(p["startTime"]), first)
        start = -(-start // step) * step
        end = int(p["endTime"])
        rows, t = [], start
        while t + step - 1 <= end and len(rows) < 1000:
            # дневные закрытия колеблются ±10%, иначе монета похожа на стейблкоин
            close = ("90" if (t // step) % 2 else "110") if interval == "1d" else "100"
            high, low = ("111", "89") if interval == "1d" else ("101", "99.5")
            open_ = "100"
            if trend:
                open_, close = price(t), price(t + step)
                high, low = close, open_
            rows.append([t, open_, high, low, close, "10", t + step - 1, "1000", 1, "0", "0", "0"])
            t += step
        return httpx.Response(200, json=rows)

    return handler


@pytest.fixture
def env(monkeypatch):
    conn = sqlite3.connect(":memory:")
    migrate(conn)
    journal = Journal(conn)
    settings = Settings(healthcheck_url=PING, claude_monthly_budget_usd=10)
    state = {"strategies": [FakeStrategy()]}
    monkeypatch.setattr(cycle, "load_approved", lambda: state["strategies"])

    def make(window_at=MORNING, claude=None):
        return Deps(
            settings=settings,
            conn=conn,
            journal=journal,
            store=CandleStore(conn),
            binance=BinanceClient(http=httpx.Client(), sleep=lambda s: None),
            anthropic_client=claude or FakeClaude(),
            now_fn=lambda: window_at + timedelta(seconds=30),
            http=httpx.Client(),
        )

    return SimpleNamespace(conn=conn, journal=journal, make=make, state=state)


def mock_network(window_at=MORNING, fail=None, symbols=SYMBOLS, trend=False):
    respx.get(f"{BASE}/api/v3/exchangeInfo").mock(
        return_value=httpx.Response(200, json=exchange_info(symbols))
    )
    klines = respx.get(f"{BASE}/api/v3/klines").mock(side_effect=klines_handler(window_at, fail, trend))
    respx.get(FX_URL).mock(return_value=httpx.Response(200, json={"rates": {"KZT": 500.0}}))
    ping = respx.get(PING).mock(return_value=httpx.Response(200))
    return SimpleNamespace(klines=klines, ping=ping)


def count(conn, table: str, where: str = "1") -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0]


def positions(conn) -> list[tuple[str, str]]:
    return conn.execute("SELECT book, status FROM positions ORDER BY book").fetchall()


def run_status(conn, window_at=MORNING):
    return conn.execute(
        "SELECT status, error FROM runs WHERE window_at=?", (window_at.isoformat(),)
    ).fetchone()


def seed_stage(journal: Journal) -> None:
    for book in ("main", "research"):
        journal.start_new_stage(book, 400.0, MORNING - timedelta(days=10))
    journal.conn.commit()


def seed_position(journal, symbol, book, outcome: Outcome | None, created_days_ago=3):
    created = MORNING - timedelta(days=created_days_ago)
    sid = journal.add_signal(
        SignalCandidate(symbol, "trend", created, 99.0, 100.0, 95.0, 110.0, {}), "v-test"
    )
    pid = journal.add_position(sid, book, journal.book(book).stage, Sizing(0.8, 80.0, 4.0))
    if outcome is not None:
        journal.apply_outcome(pid, outcome)
    journal.conn.commit()
    return pid


@respx.mock
def test_window_sends_approved_signal_and_records_both_books(env):
    net = mock_network()
    claude = FakeClaude(APPROVE)
    msgs = run_window(MORNING, env.make(claude=claude))

    assert len(msgs) == 2
    header, signal = msgs
    assert header.startswith("<b>Окно 20.10 08:00 (UTC+5)</b>")
    assert "Сигналов: 1." in header
    assert "Монеты" in header and "SOLUSDT" in header
    assert "<b>SOLUSDT</b> — покупка (спот)" in signal
    assert "Стоп: 95\n" in signal and "Цель: 110\n" in signal
    assert "Сумма: $80.00 (40 000 ₸)" in signal  # 400 USDT счёт, риск 1% = 4, qty 0.8
    assert EXPLANATION in signal
    assert "21.10 08:00" not in signal and "20.10 20:00 (UTC+5)" in signal
    assert claude.calls == ["parse", "create"]

    c = env.conn
    assert c.execute("SELECT claude_status, sent FROM signals").fetchall() == [("approved", 1)]
    assert positions(c) == [("main", "pending"), ("research", "pending")]
    for book in ("main", "research"):
        b = env.journal.book(book)
        assert (b.stage, b.start_equity, b.peak) == (1, 400.0, 400.0)
    month = json.loads(c.execute("SELECT symbols_json FROM coins WHERE month='2026-10'").fetchone()[0])
    assert set(month) == set(SYMBOLS)
    assert count(c, "api_usage") == 2
    assert run_status(c) == ("ok", None)
    assert net.ping.call_count == 1


@respx.mock
def test_rejected_signal_only_in_research(env):
    mock_network()
    claude = FakeClaude(REJECT)
    msgs = run_window(MORNING, env.make(claude=claude))

    assert len(msgs) == 1 and "Сигналов нет." in msgs[0]
    assert claude.calls == ["parse"]
    row = env.conn.execute("SELECT claude_status, claude_reason, sent FROM signals").fetchone()
    assert row == ("rejected", "слабый объём", 0)
    assert positions(env.conn) == [("research", "pending")]


@respx.mock
def test_review_not_sent(env):
    mock_network()
    err = anthropic.APIConnectionError(request=httpx.Request("POST", "https://x"))
    claude = FakeClaude(err)
    msgs = run_window(MORNING, env.make(claude=claude))

    assert len(msgs) == 1 and "Сигналов нет." in msgs[0]
    assert claude.calls == ["parse"]
    assert env.conn.execute("SELECT claude_status, sent FROM signals").fetchall() == [("review", 0)]
    assert positions(env.conn) == [("research", "pending")]
    assert run_status(env.conn)[0] == "ok"


@respx.mock
def test_window_is_idempotent(env):
    net = mock_network()
    claude = FakeClaude(APPROVE)
    first = run_window(MORNING, env.make(claude=claude))
    tables = ("signals", "positions", "coins", "api_usage", "runs")
    before = {t: count(env.conn, t) for t in tables}
    klines_calls = net.klines.call_count

    assert run_window(MORNING, env.make(claude=claude)) == []
    assert len(first) == 2
    assert {t: count(env.conn, t) for t in tables} == before
    assert claude.calls == ["parse", "create"]
    assert net.klines.call_count == klines_calls


@respx.mock
@pytest.mark.parametrize("status", [429, 451, 503])
def test_window_binance_down_reports_failure_and_writes_nothing(env, status):
    mock_network(fail={"BTCUSDT": status})  # без BTC окно не считается
    claude = FakeClaude(APPROVE)
    msgs = run_window(MORNING, env.make(claude=claude))

    assert len(msgs) == 1
    assert "Сигналы не посчитаны, причина:" in msgs[0] and f"HTTP {status}" in msgs[0]
    for table in ("signals", "positions", "coins", "api_usage", "budget_events"):
        assert count(env.conn, table) == 0, table
    assert env.journal.book("main").stage == 0 and env.journal.book("research").stage == 0
    assert run_status(env.conn)[0] == "failed"
    assert claude.calls == []

    respx.reset()
    mock_network()  # Binance вернулся — окно можно повторить
    retry = run_window(MORNING, env.make(claude=claude))
    assert len(retry) == 2 and run_status(env.conn) == ("ok", None)


@respx.mock
def test_halt_stops_signals_but_research_continues(env):
    mock_network()
    seed_stage(env.journal)
    loss_day = MORNING - timedelta(days=2)
    seed_position(
        env.journal, "ETHUSDT", "main",
        Outcome("stop", loss_day, 100.0, loss_day + timedelta(hours=5), 30.0),
    )  # fmt: skip
    seed_position(  # вместе ≈ −64 USDT из 400 = −16% → HALTED
        env.journal, "BTCUSDT", "main",
        Outcome("stop", loss_day, 100.0, loss_day + timedelta(hours=6), 90.0),
    )  # fmt: skip
    claude = FakeClaude(APPROVE)
    msgs = run_window(MORNING, env.make(claude=claude))

    assert env.journal.book("main").dd_state == "HALTED"
    assert any("Остановка: просадка достигла −15%" in m for m in msgs)
    assert not any("покупка (спот)" in m for m in msgs)
    assert claude.calls == []
    sol = "SELECT p.book, p.status FROM positions p JOIN signals s ON s.id=p.signal_id"
    assert env.conn.execute(sol + " WHERE s.symbol='SOLUSDT'").fetchall() == [
        ("research", "pending")
    ]
    assert env.conn.execute(
        "SELECT claude_status FROM signals WHERE symbol='SOLUSDT'"
    ).fetchall() == [("skipped",)]


@respx.mock
def test_paused_skips_claude_calls(env):
    mock_network()
    seed_stage(env.journal)
    env.journal.update_book("main", paused=1)
    env.conn.commit()
    claude = FakeClaude(APPROVE)
    msgs = run_window(MORNING, env.make(claude=claude))

    assert claude.calls == []
    assert len(msgs) == 1 and "Сигналов нет." in msgs[0]
    assert env.conn.execute("SELECT claude_status, sent FROM signals").fetchall() == [("skipped", 0)]
    assert positions(env.conn) == [("research", "pending")]


@respx.mock
def test_no_approved_strategies_note(env):
    mock_network()
    env.state["strategies"] = []
    msgs = run_window(MORNING, env.make())

    assert len(msgs) == 1
    assert "Нет одобренных стратегий — см. отчёт бэктеста" in msgs[0]
    assert count(env.conn, "signals") == 0
    assert run_status(env.conn)[0] == "ok"


@respx.mock
def test_delisted_symbol_with_open_position_warns(env):
    net = mock_network()
    seed_stage(env.journal)
    entry = MORNING - timedelta(days=2)
    pid = seed_position(env.journal, "OLDUSDT", "main", Outcome("open", entry, 100.0, None, None))
    msgs = run_window(MORNING, env.make())

    status = env.conn.execute("SELECT status FROM positions WHERE id=?", (pid,)).fetchone()[0]
    assert status == "open"
    assert "OLDUSDT: нет в списке TRADING (возможно, временная остановка)" in msgs[0]
    assert not any(c.request.url.params["symbol"] == "OLDUSDT" for c in net.klines.calls)
    assert run_status(env.conn)[0] == "ok"


@respx.mock
def test_sunday_evening_has_weekly_stats(env):
    mock_network(SUNDAY_EVENING)
    msgs = run_window(SUNDAY_EVENING, env.make(SUNDAY_EVENING))

    assert msgs[0].startswith("<b>Окно 25.10 20:00 (UTC+5)</b>")
    assert any(m.startswith("<b>Вечерняя сводка 25.10 20:00 (UTC+5)</b>") for m in msgs)
    weekly = [m for m in msgs if m.startswith("<b>Недельная статистика</b>")]
    assert len(weekly) == 1 and "Критерии перехода на деньги" in weekly[0]
    assert "По стратегиям:\ntrend: сигналов 1" in weekly[0]  # сигнал этого окна, спец §12
    assert msgs.index(weekly[0]) == len(msgs) - 1


@respx.mock
def test_morning_window_has_no_summary(env):
    mock_network()
    msgs = run_window(MORNING, env.make())
    assert not any("Вечерняя сводка" in m or "Недельная статистика" in m for m in msgs)


@respx.mock
def test_open_position_is_simulated_to_target(env):
    mock_network()
    seed_stage(env.journal)
    pid = seed_position(env.journal, "ETHUSDT", "research", None)
    env.conn.execute("UPDATE signals SET target=100.5 WHERE symbol='ETHUSDT'")
    env.conn.commit()
    run_window(MORNING, env.make())
    row = env.conn.execute("SELECT status, pnl_usdt FROM positions WHERE id=?", (pid,)).fetchone()
    assert row[0] == "target" and row[1] is not None


@respx.mock
def test_healthcheck_failure_does_not_matter(env):
    net = mock_network()
    net.ping.mock(side_effect=httpx.ConnectError("down"))
    msgs = run_window(MORNING, env.make())
    assert len(msgs) == 2 and run_status(env.conn)[0] == "ok"


@respx.mock
def test_budget_threshold_message_after_signals(env):
    mock_network()
    env.conn.execute(
        "INSERT INTO api_usage VALUES (?, 'claude-haiku-5-5', 'filter', 0, 0, 8.5)",
        (MORNING.isoformat(),),
    )
    env.conn.commit()
    msgs = run_window(MORNING, env.make())
    assert "80% месячного лимита" in msgs[-1] and "покупка (спот)" in msgs[1]
    assert env.conn.execute("SELECT event FROM budget_events").fetchall() == [("budget_80",)]


@respx.mock
def test_first_window_without_fx_fails(env):
    mock_network()
    respx.get(FX_URL).mock(return_value=httpx.Response(500))
    msgs = run_window(MORNING, env.make())
    assert len(msgs) == 1 and "нет курса" in msgs[0]
    assert env.journal.book("main").stage == 0 and run_status(env.conn)[0] == "failed"


@respx.mock
def test_unexpected_error_rolls_back_window(env, monkeypatch):
    mock_network()

    def boom(*a, **kw):
        raise RuntimeError("сломалось")

    monkeypatch.setattr(cycle, "signal_message", boom)  # после записей сигнала, позиций и api_usage
    msgs = run_window(MORNING, env.make())
    assert len(msgs) == 1 and "Сигналы не посчитаны, причина:" in msgs[0] and "сломалось" in msgs[0]
    for table in ("signals", "positions", "coins", "api_usage", "budget_events"):
        assert count(env.conn, table) == 0, table
    assert env.journal.book("main").stage == 0
    assert run_status(env.conn)[0] == "failed"


@respx.mock
def test_monthly_refresh_skips_failing_pair(env):
    mock_network(fail={"SOLUSDT": 400})
    msgs = run_window(MORNING, env.make())

    assert run_status(env.conn) == ("ok", None)
    assert "Монеты на месяц: пропущено пар из-за ошибок Binance: 1 (SOLUSDT)" in msgs[0]
    month = json.loads(env.conn.execute("SELECT symbols_json FROM coins").fetchone()[0])
    assert set(month) == {"BTCUSDT", "ETHUSDT"}
    assert count(env.conn, "signals") == 0


@respx.mock
def test_too_many_failing_pairs_fail_window(env, monkeypatch):
    monkeypatch.setattr(cycle, "MAX_SKIPPED", 0)
    mock_network(fail={"SOLUSDT": 400})
    msgs = run_window(MORNING, env.make())
    assert "Сигналы не посчитаны" in msgs[0] and run_status(env.conn)[0] == "failed"


@respx.mock
def test_failing_symbol_with_active_position_fails_window(env):
    mock_network(fail={"SOLUSDT": 400})
    seed_stage(env.journal)
    seed_position(env.journal, "SOLUSDT", "research", None)
    msgs = run_window(MORNING, env.make())
    assert "Сигналы не посчитаны" in msgs[0] and "HTTP 400" in msgs[0]
    assert run_status(env.conn)[0] == "failed"


@respx.mock
def test_universe_member_without_fresh_candles_skipped_this_window(env):
    env.conn.execute(
        "INSERT INTO coins VALUES ('2026-10', ?)", (json.dumps(["BTCUSDT", "SOLUSDT"]),)
    )
    env.conn.commit()
    mock_network(fail={"SOLUSDT": 503})
    msgs = run_window(MORNING, env.make())

    assert run_status(env.conn) == ("ok", None)
    assert "SOLUSDT" in msgs[0] and "пропущены в этом окне" in msgs[0]
    assert count(env.conn, "signals") == 0  # без свежих свечей стратегия не смотрит монету


@respx.mock
def test_monthly_refresh_reads_only_recent_daily_history(env):
    mock_network()
    deps = env.make()
    starts = []
    orig = deps.store.get

    def spy(symbol, interval, start, end):
        if interval == "1d":
            starts.append(start)
        return orig(symbol, interval, start, end)

    deps.store.get = spy
    run_window(MORNING, deps)
    assert starts and min(starts) >= MORNING - timedelta(days=400)


@respx.mock
def test_ok_status_committed_with_window_writes(env):
    mock_network()

    def crash(*a, **kw):  # процесс упал бы между коммитом окна и отдельной записью статуса
        raise SystemExit("killed")

    env.journal.finish_run = crash
    run_window(MORNING, env.make())
    assert run_status(env.conn)[0] == "ok"
    del env.journal.finish_run
    assert run_window(MORNING, env.make()) == []  # повтор не задвоит сигналы
    assert count(env.conn, "signals") == 1


@respx.mock
def test_real_trend_strategy_through_window(env):
    from invest_agent.strategies.trend import TrendBreakout

    mock_network(trend=True)
    strategy = TrendBreakout()
    env.state["strategies"] = [strategy]
    claude = FakeClaude(APPROVE)
    msgs = run_window(MORNING, env.make(claude=claude))

    assert run_status(env.conn) == ("ok", None)
    rows = env.conn.execute(
        "SELECT symbol, strategy, strategy_version, entry_high, claude_status FROM signals"
    ).fetchall()
    assert {r[0] for r in rows} == set(SYMBOLS)
    assert all(r[1:3] == ("trend", strategy.version()) and r[4] == "approved" for r in rows)
    # вход по закрытию последней закрытой 4h свечи (00:00 UTC), не по незакрытой
    last_closed = 100 + (timedelta(days=400) - timedelta(hours=3)).total_seconds() / 86400 * 0.5
    assert all(abs(r[3] - last_closed) < 0.011 for r in rows)
    assert sum("покупка (спот)" in m for m in msgs) == 3
