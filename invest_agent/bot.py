import asyncio
import html
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, time, timedelta

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from invest_agent.backup import backup
from invest_agent.cycle import Deps, run_window
from invest_agent.data.binance import BinanceClient
from invest_agent.data.candles import CandleStore, last_marks
from invest_agent.data.fx import usd_kzt
from invest_agent.journal import Journal, connect, migrate
from invest_agent.llm.budget import Budget
from invest_agent.llm.client import make_client
from invest_agent.messages import failure_message, weekly_message
from invest_agent.stats import book_stats, btc_hold_return, costs, golive_check

log = logging.getLogger(__name__)
Send = Callable[[str], Awaitable[None]]
MISFIRE_GRACE = 3600
CATCHUP_MAX_AGE = timedelta(hours=12)
BACKUP_AT = time(23, 0)
SEND_ATTEMPTS = 3
SEND_BACKOFF = (1, 3)


def make_sender(bot: Bot, chat_id: int, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> Send:
    """Отправка с повтором при ошибках Telegram; последний сбой пишется в лог и не бросается."""

    async def send(text: str) -> None:
        for attempt in range(SEND_ATTEMPTS):
            try:
                await bot.send_message(chat_id, text)
                return
            except TelegramAPIError as e:
                log.warning("telegram send failed (%d/%d): %s", attempt + 1, SEND_ATTEMPTS, e)
                if attempt + 1 < SEND_ATTEMPTS:
                    await sleep(SEND_BACKOFF[attempt])
        log.error("telegram message lost after %d attempts: %.80s", SEND_ATTEMPTS, text)

    return send


def _mode(deps: Deps) -> str:
    b = deps.journal.book("main")
    return "пауза" if b.paused and b.dd_state == "NORMAL" else (
        f"{b.dd_state}, пауза" if b.paused else b.dd_state
    )  # fmt: skip


def _status_text(deps: Deps) -> str:
    j, now = deps.journal, deps.now_fn()
    main = j.book("main")
    if main.stage_started_at is None:
        return "Бумажный этап ещё не начат."
    active = j.active_positions("main")
    marks = last_marks(deps.store, {p.symbol for p in active}, now)
    equity = j.equity("main", marks)
    dd = 1 - equity / main.peak if main.peak > 0 else 0.0
    lines = [
        f"<b>Режим:</b> {_mode(deps)}",
        f"Счёт: {equity:.2f} USDT, просадка от максимума {dd * 100:.1f}% (этап {main.stage})",
    ]
    if not active:
        lines.append("Открытых и ожидающих позиций нет.")
    for p in active:
        s = j.signal(p.signal_id)
        state = "открыта" if p.status == "open" else "ждёт входа"
        lines.append(f"{html.escape(p.symbol)}: {state}; стоп {s.stop:g}, цель {s.target:g}")
    return "\n".join(lines)


def _stats_text(deps: Deps) -> str:
    j, now = deps.journal, deps.now_fn()
    main, research = j.book("main"), j.book("research")
    if main.stage_started_at is None or research.stage_started_at is None:
        return "Бумажный этап ещё не начат."
    s_main = book_stats(j, "main", main.stage_started_at, now)
    s_res = book_stats(j, "research", research.stage_started_at, now)
    crit = golive_check(j, deps.store, now)
    text = weekly_message(
        s_main,
        s_res,
        crit,
        costs(j, deps.settings, main.stage_started_at, now),
        btc_hold_return(deps.store, main.stage_started_at, now),
        usd_kzt(now, deps.conn, deps.http),
    )
    return text.replace("Недельная статистика", "Статистика с начала этапа", 1)


def _budget_text(deps: Deps) -> str:
    b = Budget(deps.journal, deps.settings, deps.now_fn())
    limit = deps.settings.claude_monthly_budget_usd
    spent = b.spent_this_month()
    pct = spent / limit * 100 if limit > 0 else 0
    return f"Расход Claude API за месяц: ${spent:.2f} из ${limit:g} ({pct:.0f}%)"


def _coins_text(deps: Deps) -> str:
    row = deps.conn.execute(
        "SELECT month, symbols_json FROM coins ORDER BY month DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return "Список монет ещё не сформирован (появится после первого окна)."
    return f"Монеты на {row[0]} ({len(json.loads(row[1]))}): " + ", ".join(json.loads(row[1]))


def build_dispatcher(deps: Deps, lock: asyncio.Lock | None = None) -> Dispatcher:
    lock = lock or asyncio.Lock()
    dp = Dispatcher()
    owner = deps.settings.telegram_chat_id
    dp.message.filter(F.chat.id == owner, F.from_user.id == owner)  # только сам владелец
    j = deps.journal

    async def status(m: Message) -> None:
        async with lock:
            text = _status_text(deps)
        await m.answer(text)

    async def stats(m: Message) -> None:
        async with lock:
            text = await asyncio.to_thread(_stats_text, deps)
        await m.answer(text)

    async def pause(m: Message) -> None:
        async with lock:
            with deps.conn:
                j.update_book("main", paused=1)
        await m.answer("Пауза: новые сигналы не отправляются. Возобновить — /resume.")

    async def resume(m: Message) -> None:
        async with lock:
            main = j.book("main")
            if main.dd_state == "HALTED":
                now = deps.now_fn()
                rate = await asyncio.to_thread(usd_kzt, now, deps.conn, deps.http)
                if rate is None:
                    text = "Не удалось получить курс USD/KZT, агент остаётся остановленным. Повторите /resume позже."
                else:
                    with deps.conn:
                        j.start_new_stage("main", deps.settings.start_capital_kzt / rate, now)
                        j.update_book("main", dd_state="NORMAL", paused=0)
                    text = "Начат новый бумажный этап: счёт и максимум основного портфеля обнулены, сигналы возобновлены."
            elif main.paused:
                with deps.conn:
                    j.update_book("main", paused=0)
                text = "Пауза снята, сигналы возобновлены."
            else:
                text = "Агент не на паузе."
        await m.answer(text)

    async def budget(m: Message) -> None:
        async with lock:
            text = _budget_text(deps)
        await m.answer(text)

    async def coins(m: Message) -> None:
        async with lock:
            text = _coins_text(deps)
        await m.answer(text)

    for name, fn in (
        ("status", status), ("stats", stats), ("pause", pause), ("resume", resume),
        ("budget", budget), ("coins", coins),
    ):  # fmt: skip
        dp.message.register(fn, Command(name))
    return dp


def _window_at(windows: list[time], now: datetime) -> datetime:
    """Ближайшее окно не позже now (сдвинутый запуск попадает в своё окно)."""
    now = now.astimezone(UTC)
    cands = [
        datetime.combine(now.date() + timedelta(days=d), w, tzinfo=UTC) for d in (-1, 0) for w in windows
    ]
    return max(c for c in cands if c <= now)


async def window_job(deps: Deps, send: Send, lock: asyncio.Lock, now: datetime | None = None) -> None:
    window_at = _window_at(deps.settings.window_times_utc(), now or datetime.now(UTC))
    try:
        async with lock:
            texts = await asyncio.to_thread(run_window, window_at, deps)
    except Exception as e:
        log.exception("window job crashed")
        texts = [failure_message(window_at, f"внутренняя ошибка ({type(e).__name__}: {e})")]
    for t in texts:
        await send(t)


def catchup_window(
    windows: list[time], now: datetime, status_of: Callable[[datetime], str | None]
) -> datetime | None:
    """Последнее окно не позже now, если оно не старше 12 ч и не завершилось "ok" (иначе None)."""
    w = _window_at(windows, now)
    if now - w > CATCHUP_MAX_AGE:
        return None
    return None if status_of(w) == "ok" else w


def schedule_catchup(
    scheduler: AsyncIOScheduler, deps: Deps, send: Send, lock: asyncio.Lock, now: datetime
) -> datetime | None:
    """После рестарта: одно пропущенное или упавшее окно — тем же путём с lock."""
    w = catchup_window(deps.settings.window_times_utc(), now, deps.journal.run_status)
    if w is not None:
        scheduler.add_job(
            window_job, "date", run_date=now, args=[deps, send, lock, w], id="catchup",
            misfire_grace_time=MISFIRE_GRACE,
        )  # fmt: skip
    return w


async def backup_job(deps: Deps, lock: asyncio.Lock) -> None:
    try:
        async with lock:
            await asyncio.to_thread(backup, deps.settings.db_path)
    except Exception:
        log.exception("backup failed")


def schedule(scheduler: AsyncIOScheduler, deps: Deps, send: Send, lock: asyncio.Lock | None = None) -> None:
    lock = lock or asyncio.Lock()
    for t in deps.settings.window_times_utc():
        scheduler.add_job(
            window_job,
            CronTrigger(hour=t.hour, minute=t.minute, timezone=UTC),
            args=[deps, send, lock],
            id=f"window-{t:%H%M}",
            misfire_grace_time=MISFIRE_GRACE,
            max_instances=1,
            coalesce=True,
        )
    scheduler.add_job(
        backup_job,
        CronTrigger(hour=BACKUP_AT.hour, minute=BACKUP_AT.minute, timezone=UTC),
        args=[deps, lock],
        id="backup",
        misfire_grace_time=MISFIRE_GRACE,
        max_instances=1,
        coalesce=True,
    )


def _missing_secrets(s) -> list[str]:
    names = {"telegram_token": s.telegram_token, "telegram_chat_id": s.telegram_chat_id,
             "anthropic_api_key": s.anthropic_api_key}  # fmt: skip
    return [k.upper() for k, v in names.items() if not v]


def _init_books(deps: Deps) -> str | None:
    """Начальные этапы обоих портфелей; возвращает причину отказа или None."""
    j = deps.journal
    todo = [b for b in ("main", "research") if j.book(b).start_equity <= 0 or j.book(b).stage == 0]
    if not todo:
        return None
    now = deps.now_fn()
    rate = usd_kzt(now, deps.conn, deps.http)
    if rate is None:
        return "нет курса USD/KZT, стартовый счёт не посчитать"
    with deps.conn:
        for b in todo:
            j.start_new_stage(b, deps.settings.start_capital_kzt / rate, now)
    return None


async def run() -> None:
    from invest_agent.config import Settings

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings()
    if missing := _missing_secrets(settings):
        raise SystemExit("Не заданы переменные окружения: " + ", ".join(missing))
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(settings.db_path, check_same_thread=False)
    migrate(conn)
    http = httpx.Client(timeout=15)
    deps = Deps(
        settings, conn, Journal(conn), CandleStore(conn), BinanceClient(),
        make_client(settings), http=http,
    )  # fmt: skip
    bot = Bot(settings.telegram_token, default=DefaultBotProperties(parse_mode="HTML"))
    send = make_sender(bot, settings.telegram_chat_id)
    try:
        if reason := _init_books(deps):
            log.error("start refused: %s", reason)
            await send(f"Агент не запущен: {reason}.")
            return
        lock = asyncio.Lock()
        dp = build_dispatcher(deps, lock)
        scheduler = AsyncIOScheduler(timezone=UTC)
        schedule(scheduler, deps, send, lock)
        if missed := schedule_catchup(scheduler, deps, send, lock, datetime.now(UTC)):
            log.info("catch-up window %s", missed.isoformat())
        scheduler.start()
        windows = " и ".join(settings.windows)
        await send(f"Агент запущен, окна {windows} (UTC+{settings.tz_offset_hours}).")
        try:
            await dp.start_polling(bot)
        finally:
            scheduler.shutdown(wait=False)
    finally:
        await bot.session.close()
        http.close()
        conn.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
