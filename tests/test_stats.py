import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from invest_agent.config import Settings
from invest_agent.data.candles import CandleStore
from invest_agent.journal import Journal, migrate
from invest_agent.models import Candle, Outcome, SignalCandidate, Sizing
from invest_agent.stats import book_stats, btc_hold_return, costs, golive_check, halt_analysis

T0 = datetime(2026, 1, 1, 3, 0, tzinfo=UTC)
END = T0 + timedelta(days=30)


@pytest.fixture
def j() -> Journal:
    conn = sqlite3.connect(":memory:")
    migrate(conn)
    jr = Journal(conn)
    jr.start_new_stage("main", 1000.0, T0)
    jr.start_new_stage("research", 1000.0, T0)
    conn.commit()
    return jr


def add(j, pnl_target: float | None, *, symbol="BTCUSDT", strategy="trend", day=1,
        book="main", status=None):  # fmt: skip
    """Закрытая сделка с заданным ~pnl: qty=1, вход 100, выход 100+pnl (комиссии малы, считаем net_pnl)."""
    s = SignalCandidate(symbol, strategy, T0 + timedelta(days=day), 99.0, 100.0, 90.0, 120.0, {})
    sid = j.add_signal(s, "v1")
    pid = j.add_position(sid, book, j.book(book).stage, Sizing(1.0, 100.0, 10.0))
    if status == "expired":
        j.apply_outcome(pid, Outcome("expired", None, None, T0 + timedelta(days=day), None))
    elif pnl_target is not None:
        st = "target" if pnl_target > 0 else "stop"
        out = Outcome(st, T0 + timedelta(days=day), 100.0, T0 + timedelta(days=day + 1),
                      100.0 + pnl_target)  # fmt: skip
        j.apply_outcome(pid, out)
    j.conn.commit()
    return pid


def pnl_of(j, pid) -> float:
    return j.conn.execute("SELECT pnl_usdt FROM positions WHERE id=?", (pid,)).fetchone()[0]


def test_profit_factor_and_win_rate(j):
    a = add(j, 20.0, day=1)
    b = add(j, 10.0, day=3)
    c = add(j, -15.0, day=5)
    add(j, None, day=7, status="expired")
    wins = [pnl_of(j, a), pnl_of(j, b)]
    loss = pnl_of(j, c)
    s = book_stats(j, "main", T0, END)
    assert s.n_signals == 4 and s.n_entered == 3 and s.n_expired == 1
    assert s.win_rate == pytest.approx(2 / 3)
    assert s.avg_win == pytest.approx(sum(wins) / 2)
    assert s.avg_loss == pytest.approx(loss)
    assert s.profit_factor == pytest.approx(sum(wins) / -loss)
    assert s.net_usdt == pytest.approx(sum(wins) + loss)
    assert s.max_dd == pytest.approx(-loss / (1000 + sum(wins)))


def test_stats_empty_and_only_wins(j):
    s = book_stats(j, "main", T0, END)
    assert (s.n_signals, s.win_rate, s.profit_factor, s.net_usdt, s.max_dd) == (
        0,
        0.0,
        0.0,
        0.0,
        0.0,
    )
    add(j, 5.0)
    assert book_stats(j, "main", T0, END).profit_factor == float("inf")


def test_costs_include_server_and_api(j):
    j.conn.execute(
        "INSERT INTO api_usage VALUES (?, 'm', 'filter', 1, 1, ?)",
        ((T0 + timedelta(days=2)).isoformat(), 0.75),
    )
    j.conn.execute(
        "INSERT INTO api_usage VALUES (?, 'm', 'filter', 1, 1, ?)",
        ((END + timedelta(days=1)).isoformat(), 9.0),
    )
    j.conn.commit()
    st = Settings(server_cost_usd_month=6.0)
    assert costs(j, st, T0, T0 + timedelta(days=15)) == pytest.approx(6.0 * 15 / 30 + 0.75)


def test_btc_hold_return():
    conn = sqlite3.connect(":memory:")
    CandleStore.ensure_schema(conn)
    store = CandleStore(conn)
    h = timedelta(hours=1)
    store.upsert("BTCUSDT", "1h", [
        Candle(T0, T0 + h - timedelta(milliseconds=1), 100, 120, 90, 110, 1, 1),
        Candle(T0 + h, T0 + 2 * h - timedelta(milliseconds=1), 110, 125, 100, 120, 1, 1),
    ])  # fmt: skip
    assert btc_hold_return(store, T0, T0 + 2 * h) == pytest.approx(0.2)
    assert btc_hold_return(store, T0 + 5 * h, T0 + 6 * h) is None


def _store(btc_end: float) -> CandleStore:
    conn = sqlite3.connect(":memory:")
    CandleStore.ensure_schema(conn)
    store = CandleStore(conn)
    ms = timedelta(milliseconds=1)
    h = timedelta(hours=1)
    store.upsert("BTCUSDT", "1h", [
        Candle(T0, T0 + h - ms, 100, 100, 100, 100, 1, 1),
        Candle(T0 + 69 * timedelta(days=1), T0 + 69 * timedelta(days=1) + h - ms,
               btc_end, btc_end, btc_end, btc_end, 1, 1),
    ])  # fmt: skip
    return store


def _fill(j, n: int, pnl: float):
    for i in range(n):
        add(j, pnl, day=i % 60 + 1)


def test_golive_requires_all_five(j):
    now = T0 + timedelta(days=70)
    store = _store(100.5)  # BTC +0.5%
    _fill(j, 100, 1.0)
    c = golive_check(j, store, now)
    assert (c.days_ok, c.trades_ok, c.profit_ok, c.beats_btc_ok, c.dd_ok) == (True,) * 5
    assert c.all_ok

    # каждое условие по очереди ломается
    assert not golive_check(j, store, T0 + timedelta(days=59)).days_ok
    few = golive_check(Journal(_copy(j, drop_last=1)), store, now)
    assert not few.trades_ok and not few.all_ok
    assert not golive_check(j, _store(130.0), now).beats_btc_ok  # BTC +30% > стратегия
    big_loss = _copy(j, extra_loss=-400.0)
    c2 = golive_check(Journal(big_loss), store, now)
    assert not c2.dd_ok and not c2.all_ok
    loss = _copy(j, extra_loss=-1000.0)
    assert not golive_check(Journal(loss), store, now).profit_ok


def _copy(j: Journal, drop_last: int = 0, extra_loss: float = 0.0) -> sqlite3.Connection:
    dst = sqlite3.connect(":memory:")
    j.conn.backup(dst)
    jr = Journal(dst)
    if drop_last:
        dst.execute("DELETE FROM positions WHERE id IN (SELECT id FROM positions ORDER BY id DESC LIMIT ?)",
                    (drop_last,))  # fmt: skip
    if extra_loss:
        add(jr, extra_loss, day=2)
    dst.commit()
    return dst


def test_golive_ignores_previous_stages(j):
    _fill(j, 100, 1.0)
    j.start_new_stage("main", 1000.0, T0 + timedelta(days=65))
    j.conn.commit()
    c = golive_check(j, _store(100.5), T0 + timedelta(days=70))
    assert not c.days_ok and not c.trades_ok


def test_halt_analysis_groups_losses(j):
    add(j, -10.0, symbol="ETHUSDT", strategy="trend", day=1)
    add(j, -30.0, symbol="ETHUSDT", strategy="meanrev", day=2)
    add(j, -5.0, symbol="SOLUSDT", strategy="trend", day=3)
    add(j, 50.0, symbol="BTCUSDT", strategy="trend", day=4)
    for d in range(5, 9):
        add(j, -1.0, symbol="ADAUSDT", strategy="meanrev", day=d)
    pend = add(j, None, symbol="XRPUSDT", day=9)
    a = halt_analysis(j, "main")
    assert a["by_strategy"]["meanrev"] < a["by_strategy"]["trend"] < 0
    assert a["by_symbol"]["ETHUSDT"] < a["by_symbol"]["SOLUSDT"] < 0
    assert "BTCUSDT" not in a["by_symbol"]
    assert len(a["worst"]) == 5
    assert (
        a["worst"][0]["symbol"] == "ETHUSDT"
        and a["worst"][0]["pnl_usdt"] < a["worst"][1]["pnl_usdt"]
    )
    assert a["open"] == [{"symbol": "XRPUSDT", "stop": 90.0, "status": "pending"}]
    assert pend


def test_golive_missing_btc_data_is_not_ok(j):
    _fill(j, 100, 1.0)
    empty = CandleStore(sqlite3.connect(":memory:"))
    CandleStore.ensure_schema(empty.conn)
    c = golive_check(j, empty, T0 + timedelta(days=70))
    assert c.profit_ok and not c.beats_btc_ok and not c.all_ok
    assert c.details["btc_missing"] is True and c.details["btc_return"] is None
