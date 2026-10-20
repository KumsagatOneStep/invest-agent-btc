import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from invest_agent.data.candles import CandleStore
from invest_agent.data.fx import ensure_schema as ensure_fx_schema
from invest_agent.execution import net_pnl
from invest_agent.llm.budget import ensure_schema as ensure_budget_schema
from invest_agent.models import Outcome, Rejection, SignalCandidate, Sizing
from invest_agent.risk import DDState, OpenRisk

ACTIVE = ("pending", "open")
FINAL = ("expired", "stop", "target", "time")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, symbol TEXT NOT NULL,
    strategy TEXT NOT NULL, strategy_version TEXT NOT NULL,
    entry_low REAL NOT NULL, entry_high REAL NOT NULL, stop REAL NOT NULL, target REAL NOT NULL,
    features_json TEXT NOT NULL, claude_status TEXT, claude_reason TEXT, explanation TEXT,
    sent INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER NOT NULL REFERENCES signals(id),
    book TEXT NOT NULL, stage INTEGER NOT NULL,
    qty REAL NOT NULL, risk_usdt REAL NOT NULL, notional REAL NOT NULL, status TEXT NOT NULL,
    entry_time TEXT, entry_price REAL, exit_time TEXT, exit_price REAL,
    pnl_usdt REAL, fees_usdt REAL, reject_reason TEXT);
CREATE INDEX IF NOT EXISTS positions_book_status ON positions(book, status);
CREATE TABLE IF NOT EXISTS books (
    book TEXT PRIMARY KEY, stage INTEGER NOT NULL, stage_started_at TEXT,
    start_equity REAL NOT NULL, peak REAL NOT NULL, dd_state TEXT NOT NULL,
    paused INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS runs (
    window_at TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
    status TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS api_usage (
    ts TEXT NOT NULL, model TEXT NOT NULL, purpose TEXT NOT NULL,
    input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL, cost_usd REAL NOT NULL);
CREATE TABLE IF NOT EXISTS coins (month TEXT PRIMARY KEY, symbols_json TEXT NOT NULL);
"""
_BOOKS = ("main", "research")


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        raise ValueError("naive datetime")
    return dt.astimezone(UTC).isoformat()


def _dt(s: str | None) -> datetime | None:
    if s is None:
        return None
    d = datetime.fromisoformat(s)
    return d.replace(tzinfo=UTC) if d.tzinfo is None else d.astimezone(UTC)


def _require_idle(conn: sqlite3.Connection, what: str) -> None:
    if conn.in_transaction:
        raise RuntimeError(f"{what} commits and must not be called inside an open transaction")


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    """Создаёт все таблицы (идемпотентно) и добавляет строки обоих портфелей, если их нет.

    Новые строки books: этап 0, start_equity 0 — реальный счёт задаёт start_new_stage.
    """
    _require_idle(conn, "migrate")
    conn.executescript(_SCHEMA)
    CandleStore.ensure_schema(conn)
    ensure_fx_schema(conn)
    ensure_budget_schema(conn)
    conn.executemany(
        "INSERT OR IGNORE INTO books (book, stage, stage_started_at, start_equity, peak, dd_state, paused)"
        " VALUES (?, 0, NULL, 0, 0, 'NORMAL', 0)",
        [(b,) for b in _BOOKS],
    )
    conn.commit()


@dataclass(frozen=True)
class PositionRow:
    id: int
    signal_id: int
    book: str
    stage: int
    symbol: str
    qty: float
    risk_usdt: float
    notional: float
    status: str
    entry_time: datetime | None
    entry_price: float | None


@dataclass(frozen=True)
class BookRow:
    book: str
    stage: int
    stage_started_at: datetime | None
    start_equity: float
    peak: float
    dd_state: DDState
    paused: bool


_BOOK_FIELDS = {"stage", "stage_started_at", "start_equity", "peak", "dd_state", "paused"}


class Journal:
    """Контракт окна: begin_run -> `with conn:` все записи окна -> finish_run.

    begin_run/finish_run коммитят сами и бросают RuntimeError внутри открытой транзакции.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def add_signal(self, sig: SignalCandidate, version: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO signals (created_at, symbol, strategy, strategy_version, entry_low,"
            " entry_high, stop, target, features_json) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                _iso(sig.created_at), sig.symbol, sig.strategy, version, sig.entry_low,
                sig.entry_high, sig.stop, sig.target, json.dumps(sig.features),
            ),
        )  # fmt: skip
        assert cur.lastrowid is not None
        return cur.lastrowid

    def signal(self, signal_id: int) -> SignalCandidate:
        r = self.conn.execute(
            "SELECT symbol, strategy, created_at, entry_low, entry_high, stop, target, features_json"
            " FROM signals WHERE id=?",
            (signal_id,),
        ).fetchone()
        if r is None:
            raise KeyError(signal_id)
        return SignalCandidate(r[0], r[1], _dt(r[2]), r[3], r[4], r[5], r[6], json.loads(r[7]))

    def set_claude(self, signal_id: int, status: str, reason: str | None, explanation: str | None) -> None:
        self.conn.execute(
            "UPDATE signals SET claude_status=?, claude_reason=?, explanation=? WHERE id=?",
            (status, reason, explanation, signal_id),
        )

    def mark_sent(self, signal_id: int) -> None:
        self.conn.execute("UPDATE signals SET sent=1 WHERE id=?", (signal_id,))

    def add_position(self, signal_id: int, book: str, stage: int, result: Sizing | Rejection) -> int:
        if isinstance(result, Rejection):
            vals = (0.0, 0.0, 0.0, "rejected", result.reason)
        else:
            vals = (result.qty, result.risk_usdt, result.notional, "pending", None)
        cur = self.conn.execute(
            "INSERT INTO positions (signal_id, book, stage, qty, risk_usdt, notional, status,"
            " reject_reason) VALUES (?,?,?,?,?,?,?,?)",
            (signal_id, book, stage, vals[0], vals[1], vals[2], vals[3], vals[4]),
        )
        assert cur.lastrowid is not None
        return cur.lastrowid

    def active_positions(self, book: str) -> list[PositionRow]:
        rows = self.conn.execute(
            "SELECT p.id, p.signal_id, p.book, p.stage, s.symbol, p.qty, p.risk_usdt, p.notional,"
            " p.status, p.entry_time, p.entry_price FROM positions p JOIN signals s ON s.id=p.signal_id"
            " WHERE p.book=? AND p.status IN ('pending','open') ORDER BY p.id",
            (book,),
        ).fetchall()
        return [PositionRow(*r[:9], _dt(r[9]), r[10]) for r in rows]

    def apply_outcome(self, position_id: int, outcome: Outcome) -> bool:
        """Применяет исход к активной позиции; финальную не трогает (False). Неизвестный id -> KeyError."""
        row = self.conn.execute(
            "SELECT qty, status FROM positions WHERE id=?", (position_id,)
        ).fetchone()
        if row is None:
            raise KeyError(position_id)
        if row[1] not in ACTIVE:
            return False
        qty = row[0]
        pnl = fees = None
        if outcome.status in ("stop", "target", "time"):
            pnl, fees = net_pnl(outcome, qty)
        elif outcome.status == "expired":
            pnl = fees = 0.0
        self.conn.execute(
            "UPDATE positions SET status=?, entry_time=?, entry_price=?, exit_time=?, exit_price=?,"
            " pnl_usdt=?, fees_usdt=? WHERE id=?",
            (
                outcome.status, _iso(outcome.entry_time), outcome.entry_price,
                _iso(outcome.exit_time), outcome.exit_price, pnl, fees, position_id,
            ),
        )  # fmt: skip
        return True

    def open_risk(self, book: str) -> list[OpenRisk]:
        return [OpenRisk(p.symbol, p.risk_usdt, p.notional) for p in self.active_positions(book)]

    def book(self, book: str) -> BookRow:
        r = self.conn.execute(
            "SELECT book, stage, stage_started_at, start_equity, peak, dd_state, paused"
            " FROM books WHERE book=?",
            (book,),
        ).fetchone()
        if r is None:
            raise KeyError(book)
        return BookRow(r[0], r[1], _dt(r[2]), r[3], r[4], r[5], bool(r[6]))

    def update_book(self, book: str, **fields: object) -> None:
        if not fields or not set(fields) <= _BOOK_FIELDS:
            raise ValueError(f"bad book fields: {sorted(fields)}")
        vals = [_iso(v) if isinstance(v, datetime) else v for v in fields.values()]
        sets = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE books SET {sets} WHERE book=?", (*vals, book))

    def start_new_stage(self, book: str, start_equity: float, now: datetime) -> None:
        """Следующий этап: счёт и максимум = start_equity, ступень NORMAL. `paused` не трогает."""
        self.update_book(
            book,
            stage=self.book(book).stage + 1,
            stage_started_at=now,
            start_equity=start_equity,
            peak=start_equity,
            dd_state="NORMAL",
        )

    def equity(self, book: str, marks: dict[str, float]) -> float:
        """start_equity + закрытый pnl этапа + нереализованный pnl открытых по `marks`.

        Открытая позиция без отметки и ожидающая вход дают 0.
        """
        b = self.book(book)
        realized = self.conn.execute(
            "SELECT COALESCE(SUM(pnl_usdt), 0) FROM positions WHERE book=? AND stage=?"
            " AND status IN ('stop','target','time','expired')",
            (book, b.stage),
        ).fetchone()[0]
        unrealized = 0.0
        for p in self.active_positions(book):
            if p.stage == b.stage and p.status == "open" and p.symbol in marks and p.entry_price:
                unrealized += p.qty * (marks[p.symbol] - p.entry_price)
        return b.start_equity + realized + unrealized

    def begin_run(self, window_at: datetime) -> bool:
        _require_idle(self.conn, "begin_run")
        w = _iso(window_at)
        row = self.conn.execute("SELECT status FROM runs WHERE window_at=?", (w,)).fetchone()
        if row is not None and row[0] == "ok":
            return False
        self.conn.execute(
            "INSERT OR REPLACE INTO runs (window_at, started_at) VALUES (?, ?)",
            (w, _iso(datetime.now(UTC))),
        )
        self.conn.commit()
        return True

    def finish_run(self, window_at: datetime, status: str, error: str | None = None) -> None:
        _require_idle(self.conn, "finish_run")
        self.conn.execute(
            "UPDATE runs SET finished_at=?, status=?, error=? WHERE window_at=?",
            (_iso(datetime.now(UTC)), status, error, _iso(window_at)),
        )
        self.conn.commit()
