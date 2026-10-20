from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from invest_agent.backtest.engine import Period, btc_hold_pct
from invest_agent.config import Settings
from invest_agent.data.candles import CandleStore
from invest_agent.journal import Journal

GOLIVE_DAYS = 60
GOLIVE_TRADES = 100
GOLIVE_MAX_DD = 0.15
_CLOSED = "('stop','target','time')"


@dataclass(frozen=True)
class BookStats:
    """n_signals/n_entered/n_expired — по времени сигнала, без отклонённых лимитами;
    остальное — по закрытым сделкам с выходом в периоде. avg_loss отрицательный;
    profit_factor = inf без убытков; max_dd — доля от пика по закрытым сделкам.
    """

    n_signals: int
    n_entered: int
    n_expired: int
    win_rate: float
    avg_win: float
    avg_loss: float
    profit_factor: float
    net_usdt: float
    max_dd: float


@dataclass(frozen=True)
class Criteria:
    days_ok: bool
    trades_ok: bool
    profit_ok: bool
    beats_btc_ok: bool
    dd_ok: bool
    details: dict

    @property
    def all_ok(self) -> bool:
        return (
            self.days_ok and self.trades_ok and self.profit_ok and self.beats_btc_ok and self.dd_ok
        )


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("naive datetime")
    return dt.astimezone(UTC).isoformat()


def _book_stats(
    journal: Journal, book: str, since: datetime, until: datetime, stage: int | None
) -> BookStats:
    conn = journal.conn
    st = "" if stage is None else f" AND p.stage={int(stage)}"
    n_signals, n_entered, n_expired = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(p.entry_time IS NOT NULL), 0),"
        " COALESCE(SUM(p.status='expired'), 0)"
        " FROM positions p JOIN signals s ON s.id=p.signal_id"
        f" WHERE p.book=? AND p.status!='rejected' AND s.created_at>=? AND s.created_at<?{st}",
        (book, _iso(since), _iso(until)),
    ).fetchone()
    pnls = [
        r[0]
        for r in conn.execute(
            f"SELECT p.pnl_usdt FROM positions p WHERE p.book=? AND p.status IN {_CLOSED}"
            f" AND p.exit_time>=? AND p.exit_time<?{st} ORDER BY p.exit_time, p.id",
            (book, _iso(since), _iso(until)),
        )
    ]
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x < 0]
    gross_loss = -sum(losses)
    if gross_loss > 0:
        pf = sum(wins) / gross_loss
    else:
        pf = float("inf") if wins else 0.0
    base = journal.book(book).start_equity
    equity = peak = base
    max_dd = 0.0
    for x in pnls:
        equity += x
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak)
    return BookStats(
        n_signals=n_signals,
        n_entered=n_entered,
        n_expired=n_expired,
        win_rate=len(wins) / len(pnls) if pnls else 0.0,
        avg_win=sum(wins) / len(wins) if wins else 0.0,
        avg_loss=sum(losses) / len(losses) if losses else 0.0,
        profit_factor=pf,
        net_usdt=sum(pnls),
        max_dd=max_dd,
    )


def book_stats(journal: Journal, book: str, since: datetime, until: datetime) -> BookStats:
    return _book_stats(journal, book, since, until, None)


def costs(journal: Journal, settings: Settings, since: datetime, until: datetime) -> float:
    """Сервер (пропорционально дням периода, месяц = 30 дней) + расход на API за [since, until), $."""
    days = (until - since).total_seconds() / 86400
    server = settings.server_cost_usd_month * days / 30
    api = journal.conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) FROM api_usage WHERE ts>=? AND ts<?",
        (_iso(since), _iso(until)),
    ).fetchone()[0]
    return server + api


def btc_hold_return(store: CandleStore, since: datetime, until: datetime) -> float | None:
    """Доходность «купить и держать BTC» за период, доля (0.1 = 10%); None без свечей."""
    if not store.get("BTCUSDT", "1h", since, until):
        return None
    return btc_hold_pct(store, Period(since, until)) / 100


def golive_check(journal: Journal, store: CandleStore, now: datetime) -> Criteria:
    """Критерии перехода по основному портфелю, текущему этапу."""
    b = journal.book("main")
    if b.stage_started_at is None or b.start_equity <= 0:
        return Criteria(False, False, False, False, False, {"stage_started": False})
    until = now + timedelta(microseconds=1)
    s = _book_stats(journal, "main", b.stage_started_at, until, b.stage)
    n_closed = journal.conn.execute(
        f"SELECT COUNT(*) FROM positions WHERE book='main' AND stage=? AND status IN {_CLOSED}",
        (b.stage,),
    ).fetchone()[0]
    days = (now - b.stage_started_at).total_seconds() / 86400
    ret = s.net_usdt / b.start_equity
    btc = btc_hold_return(store, b.stage_started_at, now)
    details = {"btc_missing": btc is None, "days": days, "closed": n_closed, "net_usdt": s.net_usdt, "return": ret,
               "btc_return": btc, "max_dd": s.max_dd}  # fmt: skip
    return Criteria(
        days_ok=days >= GOLIVE_DAYS,
        trades_ok=n_closed >= GOLIVE_TRADES,
        profit_ok=s.net_usdt > 0,
        beats_btc_ok=btc is not None and ret > btc,
        dd_ok=s.max_dd <= GOLIVE_MAX_DD,
        details=details,
    )


def halt_analysis(journal: Journal, book: str) -> dict:
    """Разбор убытков текущего этапа: по стратегиям, по монетам, 5 худших сделок, открытые позиции."""
    stage = journal.book(book).stage
    rows = journal.conn.execute(
        "SELECT s.symbol, s.strategy, p.pnl_usdt, p.exit_time FROM positions p"
        f" JOIN signals s ON s.id=p.signal_id WHERE p.book=? AND p.stage=? AND p.status IN {_CLOSED}"
        " AND p.pnl_usdt<0 ORDER BY p.pnl_usdt, p.id",
        (book, stage),
    ).fetchall()
    by_strategy: dict[str, float] = {}
    by_symbol: dict[str, float] = {}
    for symbol, strategy, pnl, _ in rows:
        by_strategy[strategy] = by_strategy.get(strategy, 0.0) + pnl
        by_symbol[symbol] = by_symbol.get(symbol, 0.0) + pnl
    worst = [
        {"symbol": r[0], "strategy": r[1], "pnl_usdt": r[2], "exit_time": r[3]} for r in rows[:5]
    ]
    open_rows = journal.conn.execute(
        "SELECT s.symbol, s.stop, p.status FROM positions p JOIN signals s ON s.id=p.signal_id"
        " WHERE p.book=? AND p.status IN ('pending','open') ORDER BY p.id",
        (book,),
    ).fetchall()
    return {
        "by_strategy": dict(sorted(by_strategy.items(), key=lambda kv: kv[1])),
        "by_symbol": dict(sorted(by_symbol.items(), key=lambda kv: kv[1])),
        "worst": worst,
        "open": [{"symbol": r[0], "stop": r[1], "status": r[2]} for r in open_rows],
    }
