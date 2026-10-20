import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from invest_agent.config import PRICES_PER_MTOK, Settings

if TYPE_CHECKING:
    from invest_agent.journal import Journal

Purpose = Literal["filter", "explain", "summary"]
_SONNET = "claude-sonnet-5-5"
_EVENTS = (("budget_80", 0.8), ("budget_100", 1.0))


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS budget_events ("
        "month TEXT NOT NULL, event TEXT NOT NULL, PRIMARY KEY (month, event))"
    )
    conn.commit()


def _month_bounds(now: datetime) -> tuple[str, str]:
    n = now.astimezone(UTC)
    start = datetime(n.year, n.month, 1, tzinfo=UTC)
    end = datetime(n.year + n.month // 12, n.month % 12 + 1, 1, tzinfo=UTC)
    return start.isoformat(), end.isoformat()


class Budget:
    """Месячный лимит Claude API (календарный месяц UTC).

    record() и threshold_events() пишут в БД, но не коммитят: транзакцию окна ведёт вызывающий.
    """

    def __init__(self, journal: "Journal", settings: Settings, now: datetime) -> None:
        if now.tzinfo is None:
            raise ValueError("naive datetime")
        self.journal, self.settings, self.now = journal, settings, now.astimezone(UTC)

    def spent_this_month(self) -> float:
        start, end = _month_bounds(self.now)
        return self.journal.conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM api_usage WHERE ts >= ? AND ts < ?",
            (start, end),
        ).fetchone()[0]

    def _ratio(self) -> float:
        limit = self.settings.claude_monthly_budget_usd
        return float("inf") if limit <= 0 else self.spent_this_month() / limit

    def model_for(self, purpose: Purpose) -> str | None:
        ratio = self._ratio()
        if ratio >= 1.0:
            return None
        if purpose == "explain" and ratio < 0.8:
            return self.settings.strong_model
        return self.settings.cheap_model

    def record(self, model: str, purpose: str, usage) -> float:
        p_in, p_out = PRICES_PER_MTOK.get(model, PRICES_PER_MTOK[_SONNET])
        cost = usage.input_tokens * p_in / 1e6 + usage.output_tokens * p_out / 1e6
        self.journal.conn.execute(
            "INSERT INTO api_usage (ts, model, purpose, input_tokens, output_tokens, cost_usd)"
            " VALUES (?,?,?,?,?,?)",
            (self.now.isoformat(), model, purpose, usage.input_tokens, usage.output_tokens, cost),
        )
        return cost

    def threshold_events(self) -> list[str]:
        """Пороги, достигнутые в этом месяце и ещё не объявленные (помечает их объявленными)."""
        ratio = self._ratio()
        month = f"{self.now:%Y-%m}"
        fired = []
        for event, level in _EVENTS:
            if ratio < level:
                continue
            cur = self.journal.conn.execute(
                "INSERT OR IGNORE INTO budget_events (month, event) VALUES (?, ?)", (month, event)
            )
            if cur.rowcount:
                fired.append(event)
        return fired
