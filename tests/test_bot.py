import asyncio
import sqlite3
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import SendMessage
from aiogram.types import Chat, Message, Update, User
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from invest_agent import bot as botmod
from invest_agent.bot import build_dispatcher, make_sender, schedule
from invest_agent.config import Settings
from invest_agent.journal import Journal, migrate
from invest_agent.models import SignalCandidate, Sizing

CHAT = 111
NOW = datetime(2026, 10, 20, 4, 0, tzinfo=UTC)


class FakeSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.sent: list[SendMessage] = []

    async def make_request(self, bot, method, timeout=None):
        self.sent.append(method)
        return Message(message_id=1, date=NOW, chat=Chat(id=method.chat_id, type="private"), text=method.text)

    async def stream_content(self, *a, **k):  # pragma: no cover
        yield b""

    async def close(self) -> None:
        pass


@pytest.fixture
def env():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    migrate(conn)
    journal = Journal(conn)
    settings = Settings(telegram_chat_id=CHAT, start_capital_kzt=200_000)
    conn.execute("INSERT INTO fx VALUES (?, ?)", (NOW.isoformat(), 500.0))
    conn.commit()
    deps = SimpleNamespace(
        settings=settings, conn=conn, journal=journal, now_fn=lambda: NOW, http=None, store=None
    )
    from invest_agent.data.candles import CandleStore

    deps.store = CandleStore(conn)
    session = FakeSession()
    bot = Bot("123:abc", session=session)
    dp = build_dispatcher(deps)
    n = [0]

    async def say(text: str, chat: int = CHAT):
        n[0] += 1
        msg = Message(
            message_id=n[0], date=NOW, chat=Chat(id=chat, type="private"),
            from_user=User(id=chat, is_bot=False, first_name="x"), text=text,
            entities=[{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}],
        )  # fmt: skip
        await dp.feed_update(bot, Update(update_id=n[0], message=msg))
        return [m.text for m in session.sent]

    return SimpleNamespace(conn=conn, journal=journal, deps=deps, say=say, session=session, dp=dp)


def start_books(journal, conn):
    with conn:
        for b in ("main", "research"):
            journal.start_new_stage(b, 400.0, NOW)


async def test_foreign_chat_ignored(env):
    start_books(env.journal, env.conn)
    sent = await env.say("/pause", chat=999)
    assert sent == []
    assert env.journal.book("main").paused is False
    await env.say("/pause")
    assert env.journal.book("main").paused is True


async def test_resume_from_pause_keeps_stage(env):
    start_books(env.journal, env.conn)
    with env.conn:
        env.journal.update_book("main", paused=1)
    stage = env.journal.book("main").stage
    sent = await env.say("/resume")
    b = env.journal.book("main")
    assert b.paused is False and b.stage == stage
    assert "новый бумажный этап" not in sent[-1]


async def test_resume_from_halt_starts_new_stage(env):
    start_books(env.journal, env.conn)
    with env.conn:
        env.journal.update_book("main", dd_state="HALTED", peak=400.0, paused=1)
    stage = env.journal.book("main").stage
    sent = await env.say("/resume")
    b = env.journal.book("main")
    assert b.stage == stage + 1
    assert b.dd_state == "NORMAL" and b.paused is False
    assert b.start_equity == pytest.approx(200_000 / 500.0) and b.peak == b.start_equity
    assert "новый бумажный этап" in sent[-1]
    assert env.journal.book("research").stage == stage


def _down(*a, **k):
    raise httpx.ConnectError("down")


async def test_resume_halt_without_rate_stays_halted(env):
    start_books(env.journal, env.conn)
    with env.conn:
        env.journal.update_book("main", dd_state="HALTED")
        env.conn.execute("DELETE FROM fx")
    env.deps.http = SimpleNamespace(get=_down)
    sent = await env.say("/resume")
    assert env.journal.book("main").dd_state == "HALTED"
    assert "курс" in sent[-1]


async def test_status_shows_mode_and_positions(env):
    start_books(env.journal, env.conn)
    j = env.journal
    with env.conn:
        sig = SignalCandidate("SOLUSDT", "trend", NOW, 100.0, 101.0, 95.0, 115.0, {})
        sid = j.add_signal(sig, "v1")
        j.add_position(sid, "main", 1, Sizing(qty=1.0, risk_usdt=4.0, notional=100.0))
        j.update_book("main", dd_state="REDUCED")
    sent = await env.say("/status")
    text = sent[-1]
    assert "REDUCED" in text
    assert "SOLUSDT" in text and "95" in text and "115" in text
    assert "ждёт входа" in text


async def test_budget_and_coins_and_stats(env):
    start_books(env.journal, env.conn)
    env.conn.execute("INSERT INTO coins VALUES ('2026-10', '[\"BTCUSDT\", \"ETHUSDT\"]')")
    env.conn.commit()
    sent = await env.say("/coins")
    assert "BTCUSDT" in sent[-1] and "ETHUSDT" in sent[-1]
    sent = await env.say("/budget")
    assert "$0.00" in sent[-1] and "10" in sent[-1]
    sent = await env.say("/stats")
    assert "Критерии" in sent[-1]


def test_schedule_registers_utc_windows(env):
    sched = AsyncIOScheduler(timezone=UTC)

    async def send(text): ...

    schedule(sched, env.deps, send)
    jobs = {j.id: j for j in sched.get_jobs()}
    hours = sorted(
        (str(j.trigger.fields[5]), str(j.trigger.fields[6])) for i, j in jobs.items() if i.startswith("window")
    )
    assert hours == [("15", "0"), ("3", "0")]
    assert all(j.misfire_grace_time == 3600 and j.max_instances == 1 for i, j in jobs.items() if i.startswith("window"))
    b = jobs["backup"]
    assert (str(b.trigger.fields[5]), str(b.trigger.fields[6])) == ("23", "0")


async def test_window_job_runs_in_thread_and_sends(env, monkeypatch):
    calls = []

    def fake_run(window_at, deps):
        calls.append(window_at)
        return ["a", "b"]

    monkeypatch.setattr(botmod, "run_window", fake_run)
    out = []

    async def send(t):
        out.append(t)

    await botmod.window_job(env.deps, send, asyncio.Lock(), now=datetime(2026, 10, 20, 3, 0, 40, tzinfo=UTC))
    assert calls == [datetime(2026, 10, 20, 3, 0, tzinfo=UTC)]
    assert out == ["a", "b"]


async def test_window_job_crash_sends_failure(env, monkeypatch):
    def boom(window_at, deps):
        raise RuntimeError("x")

    monkeypatch.setattr(botmod, "run_window", boom)
    out = []

    async def send(t):
        out.append(t)

    await botmod.window_job(env.deps, send, asyncio.Lock(), now=datetime(2026, 10, 20, 15, 5, tzinfo=UTC))
    assert len(out) == 1 and "RuntimeError" in out[0]


async def test_sender_retries_then_gives_up():
    from aiogram.exceptions import TelegramNetworkError

    class Flaky:
        def __init__(self, fails):
            self.fails, self.calls = fails, 0

        async def send_message(self, chat_id, text):
            self.calls += 1
            if self.calls <= self.fails:
                raise TelegramNetworkError(method=None, message="net")

    async def nosleep(s):
        pass

    b = Flaky(2)
    await make_sender(b, 1, sleep=nosleep)("hi")
    assert b.calls == 3
    b = Flaky(99)
    await make_sender(b, 1, sleep=nosleep)("hi")  # не бросает
    assert b.calls == 3


def test_init_books_skips_started_and_needs_rate(env):
    assert botmod._init_books(env.deps) is None
    assert env.journal.book("main").start_equity == pytest.approx(400.0)
    assert env.journal.book("research").stage == 1
    before = env.journal.book("main")
    assert botmod._init_books(env.deps) is None
    assert env.journal.book("main") == before
    env.conn.execute("DELETE FROM fx")
    env.conn.execute("UPDATE books SET stage=0, start_equity=0")
    env.conn.commit()
    env.deps.http = SimpleNamespace(get=_down)
    assert "курса" in botmod._init_books(env.deps)
    assert env.journal.book("main").stage == 0
