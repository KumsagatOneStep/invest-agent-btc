import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from invest_agent.execution import net_pnl, unrealized_pnl
from invest_agent.journal import Journal, connect, migrate
from invest_agent.models import Outcome, Rejection, SignalCandidate, Sizing

T0 = datetime(2026, 1, 1, 3, 0, tzinfo=UTC)


def sig(symbol="BTCUSDT") -> SignalCandidate:
    return SignalCandidate(
        symbol=symbol, strategy="trend", created_at=T0,
        entry_low=99.0, entry_high=100.0, stop=95.0, target=110.0, features={"rsi": 55.0},
    )  # fmt: skip


@pytest.fixture
def j() -> Journal:
    conn = sqlite3.connect(":memory:")
    migrate(conn)
    jr = Journal(conn)
    jr.start_new_stage("main", 500.0, T0)
    jr.start_new_stage("research", 500.0, T0)
    conn.commit()
    return jr


def new_pos(j: Journal, book: str, symbol="BTCUSDT", stage=None) -> int:
    sid = j.add_signal(sig(symbol), "v1")
    st = j.book(book).stage if stage is None else stage
    return j.add_position(sid, book, st, Sizing(qty=1.0, notional=100.0, risk_usdt=5.0))


def closed(status="target", entry=100.0, exit_=110.0) -> Outcome:
    return Outcome(status, T0, entry, T0 + timedelta(days=1), exit_)


def test_books_independent(j):
    new_pos(j, "research")
    assert len(j.active_positions("research")) == 1
    assert j.active_positions("main") == []
    assert j.open_risk("main") == []
    assert [o.symbol for o in j.open_risk("research")] == ["BTCUSDT"]


def test_equity_counts_only_current_stage(j):
    old = new_pos(j, "main", stage=1)
    j.apply_outcome(old, closed("target", 100.0, 110.0))
    j.start_new_stage("main", 500.0, T0 + timedelta(days=1))
    assert j.equity("main", {}) == 500.0
    cur = new_pos(j, "main", "ETHUSDT")
    j.apply_outcome(cur, closed("stop", 100.0, 95.0))
    pnl = net_pnl(closed("stop", 100.0, 95.0), 1.0)[0]
    assert j.equity("main", {}) == pytest.approx(500.0 + pnl)


def test_equity_unrealized_and_missing_mark(j):
    pid = new_pos(j, "main")
    assert j.equity("main", {"BTCUSDT": 120.0}) == 500.0  # pending
    j.apply_outcome(pid, Outcome("open", T0, 100.0, None, None))
    # отметка с учётом проскальзывания и комиссий выхода — как в бэктесте
    assert j.equity("main", {"BTCUSDT": 110.0}) == pytest.approx(500.0 + unrealized_pnl(1.0, 100.0, 110.0))
    assert j.equity("main", {}) == 500.0


def test_new_stage_resets_peak_and_equity(j):
    j.update_book("main", peak=650.0, dd_state="HALTED")
    j.start_new_stage("main", 300.0, T0 + timedelta(days=2))
    b = j.book("main")
    assert (b.stage, b.start_equity, b.peak, b.dd_state) == (2, 300.0, 300.0, "NORMAL")
    assert b.stage_started_at == T0 + timedelta(days=2)
    assert b.stage_started_at.tzinfo is not None
    assert j.equity("main", {}) == 300.0


def test_apply_outcome_records_net_pnl(j):
    pid = new_pos(j, "main")
    out = closed("target", 100.0, 110.0)
    j.apply_outcome(pid, out)
    pnl, fees = net_pnl(out, 1.0)
    row = j.conn.execute(
        "SELECT status, pnl_usdt, fees_usdt, exit_price FROM positions WHERE id=?", (pid,)
    ).fetchone()
    assert row == ("target", pytest.approx(pnl), pytest.approx(fees), 110.0)
    assert j.active_positions("main") == []


def test_expired_has_zero_pnl(j):
    pid = new_pos(j, "main")
    j.apply_outcome(pid, Outcome("expired", None, None, None, None))
    assert j.conn.execute("SELECT pnl_usdt FROM positions").fetchone()[0] == 0.0
    assert j.active_positions("main") == []


def test_begin_run_twice_returns_false(j):
    assert j.begin_run(T0) is True
    assert j.begin_run(T0) is True  # crashed run may be re-begun
    j.finish_run(T0, "ok")
    assert j.begin_run(T0) is False


def test_begin_run_retry_after_error(j):
    assert j.begin_run(T0) is True
    j.finish_run(T0, "error", "binance down")
    assert j.begin_run(T0) is True
    row = j.conn.execute("SELECT status, error, finished_at FROM runs").fetchone()
    assert row == (None, None, None)


def test_run_calls_raise_inside_transaction(j):
    with j.conn:
        j.add_signal(sig(), "v1")
        with pytest.raises(RuntimeError):
            j.begin_run(T0)
        with pytest.raises(RuntimeError):
            j.finish_run(T0, "ok")
        with pytest.raises(RuntimeError):
            migrate(j.conn)


def test_apply_outcome_noop_on_final_position(j):
    pid = new_pos(j, "main")
    assert j.apply_outcome(pid, closed("target", 100.0, 110.0)) is True
    before = j.conn.execute("SELECT * FROM positions WHERE id=?", (pid,)).fetchone()
    assert j.apply_outcome(pid, Outcome("open", T0, 100.0, None, None)) is False
    assert j.apply_outcome(pid, closed("stop", 100.0, 95.0)) is False
    assert j.conn.execute("SELECT * FROM positions WHERE id=?", (pid,)).fetchone() == before


def test_apply_outcome_unknown_id(j):
    with pytest.raises(KeyError):
        j.apply_outcome(999, closed())


def test_foreign_keys_enforced(tmp_path):
    conn = connect(tmp_path / "fk.db")
    migrate(conn)
    with pytest.raises(sqlite3.IntegrityError):
        Journal(conn).add_position(12345, "main", 1, Sizing(1.0, 100.0, 5.0))


def test_rejection_is_recorded_with_reason(j):
    sid = j.add_signal(sig(), "v1")
    pid = j.add_position(sid, "main", 1, Rejection("below min notional"))
    row = j.conn.execute("SELECT status, reject_reason FROM positions WHERE id=?", (pid,)).fetchone()
    assert row == ("rejected", "below min notional")
    assert j.active_positions("main") == []


def test_signal_roundtrip_and_claude(j):
    sid = j.add_signal(sig(), "v1")
    j.set_claude(sid, "approved", "ok", "text")
    row = j.conn.execute("SELECT claude_status, explanation, features_json FROM signals").fetchone()
    assert row[0] == "approved" and row[1] == "text" and "rsi" in row[2]


def test_migrate_idempotent_keeps_books():
    conn = sqlite3.connect(":memory:")
    migrate(conn)
    Journal(conn).start_new_stage("main", 500.0, T0)
    conn.commit()
    migrate(conn)
    assert Journal(conn).book("main").start_equity == 500.0
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"signals", "positions", "books", "runs", "api_usage", "coins", "fx", "candles"} <= tables


def test_connect_wal_and_fk(tmp_path):
    conn = connect(tmp_path / "j.db")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_signal_roundtrip():
    conn = sqlite3.connect(":memory:")
    migrate(conn)
    j = Journal(conn)
    assert j.signal(j.add_signal(sig("ETHUSDT"), "v1")) == sig("ETHUSDT")
    with pytest.raises(KeyError):
        j.signal(999)


def test_connect_sets_busy_timeout(tmp_path):
    conn = connect(tmp_path / "j.sqlite")
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 60_000
