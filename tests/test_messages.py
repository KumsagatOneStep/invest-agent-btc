from datetime import UTC, datetime, timedelta, timezone

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
from invest_agent.models import SignalCandidate, Sizing
from invest_agent.stats import BookStats, Criteria

TZ = timezone(timedelta(hours=5))
T0 = datetime(2026, 10, 20, 3, 0, tzinfo=UTC)
SIG = SignalCandidate(
    symbol="ETHUSDT", strategy="trend", created_at=T0,
    entry_low=2400.5, entry_high=2410.0, stop=2300.0, target=2600.0, features={},
)  # fmt: skip
SIZING = Sizing(qty=0.4, notional=964.0, risk_usdt=44.0)
EXPIRES = T0 + timedelta(hours=12)


def msg(explanation="Почему: пробой.", usd_kzt: float | None = 500.0) -> str:
    return signal_message(SIG, SIZING, 4400.0, 0.01, explanation, EXPIRES, usd_kzt, TZ)


def test_signal_message_contains_all_fields():
    m = msg()
    for part in ("ETHUSDT", "покупка (спот)", "2400.5", "2410", "2300", "2600", "964", "482 000",
                 "44", "22 000", "1%", "Почему: пробой.", "20.10 20:00", "UTC+5", "OCO"):  # fmt: skip
        assert part in m, part


def test_signal_message_escapes_and_truncates_explanation():
    m = msg("<b>x</b> & стоп 1.0 " + "а" * 10_000)
    assert len(m) <= 4096
    assert "&lt;b&gt;x&lt;/b&gt; &amp; стоп 1.0" in m
    assert "<b>x</b>" not in m
    assert "…" in m
    assert "2300" in m and "OCO" in m  # остальное цело


def test_signal_message_truncation_never_splits_entity():
    m = msg("&" * 10_000)
    assert len(m) <= 4096
    assert "&amp;…" in m


def test_signal_message_without_fx_shows_usd_only():
    m = msg(usd_kzt=None)
    assert "964" in m and "44" in m
    assert "₸" not in m


def test_no_signals_header():
    h = window_header(T0.astimezone(TZ), 0, [])
    assert "Сигналов нет" in h
    h2 = window_header(T0.astimezone(TZ), 2, ["<тест>"])
    assert "Сигналов: 2" in h2 and "&lt;тест&gt;" in h2


def test_other_messages():
    assert "сниж" in reduced_message().lower()
    assert "максимум" in recovered_message()
    f = failure_message(T0, "нет <данных>")
    assert "Сигналы не посчитаны, причина: нет &lt;данных&gt;" in f
    assert "80%" in budget_message("budget_80")
    assert "100%" in budget_message("budget_100")


def test_evening_summary_and_halt():
    ev = evening_summary(
        T0.astimezone(TZ),
        [OpenView("ETHUSDT", "open", 2410.0, 2300.0, 2600.0, 2450.0)],
        4400.0, "NORMAL", 500.0, [],
    )  # fmt: skip
    assert "ETHUSDT" in ev and "2300" in ev
    assert "нет" in evening_summary(T0, [], 4400.0, "NORMAL", None, []).lower()
    h = halt_message({
        "by_strategy": {"trend": -50.0}, "by_symbol": {"ETHUSDT": -50.0},
        "worst": [{"symbol": "ETHUSDT", "strategy": "trend", "pnl_usdt": -50.0}],
        "open": [{"symbol": "BTCUSDT", "stop": 90.0}],
    })  # fmt: skip
    assert "trend" in h and "BTCUSDT" in h and "/resume" in h


def test_weekly_message():
    s = BookStats(10, 8, 2, 0.5, 20.0, -10.0, 2.0, 30.0, 0.05)
    c = Criteria(True, False, True, False, True, {})
    m = weekly_message(s, s, c, 3.0, btc_return=0.02, usd_kzt=500.0)
    assert "27" in m and "не выполнены" in m
