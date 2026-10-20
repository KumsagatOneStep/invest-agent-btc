from datetime import UTC, datetime, time, timedelta

import pytest

from invest_agent.config import FEE, SLIPPAGE
from invest_agent.execution import net_pnl, next_window, simulate, unrealized_pnl
from invest_agent.models import Candle, Outcome, SignalCandidate

T0 = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
H = timedelta(hours=1)


def candle(i: int, o: float, h: float, l: float, c: float) -> Candle:
    t = T0 + i * H
    return Candle(t, t + H - timedelta(milliseconds=1), o, h, l, c, 1.0, 1.0)


def sig() -> SignalCandidate:
    return SignalCandidate("BTCUSDT", "trend", T0, 99.0, 100.0, 95.0, 110.0, {})


EXP = T0 + 24 * H
ENTRY = 100.0 * (1 + SLIPPAGE)


def test_pending_before_expiry():
    out = simulate(sig(), [candle(0, 105, 106, 104, 105)], EXP)
    assert out.status == "pending" and out.entry_price is None


def test_expired_when_zone_never_touched():
    cs = [candle(i, 105, 106, 104, 105) for i in range(25)]
    assert simulate(sig(), cs, EXP).status == "expired"


def test_expiry_covered_by_candle_ending_at_expiry():
    cs = [candle(i, 105, 106, 104, 105) for i in range(24)]
    assert cs[-1].close_time < EXP
    assert simulate(sig(), cs, EXP).status == "expired"


def test_entry_at_worst_price_plus_slippage():
    out = simulate(sig(), [candle(0, 101, 102, 99.5, 101)], EXP)
    assert out.status == "open"
    assert out.entry_price == pytest.approx(ENTRY)
    assert out.entry_time == T0
    assert out.exit_time is None and out.exit_price is None


def test_stop_in_entry_candle():
    out = simulate(sig(), [candle(0, 101, 102, 94, 96)], EXP)
    assert out.status == "stop"
    assert out.exit_price == pytest.approx(95 * (1 - SLIPPAGE))
    assert out.exit_time == T0 + H - timedelta(milliseconds=1)


def test_target_ignored_in_entry_candle():
    out = simulate(sig(), [candle(0, 101, 112, 99.5, 111)], EXP)
    assert out.status == "open"


def test_both_hit_counts_as_stop():
    cs = [candle(0, 101, 102, 99.5, 101), candle(1, 101, 112, 94, 100)]
    assert simulate(sig(), cs, EXP).status == "stop"


def test_target_after_entry_candle():
    cs = [candle(0, 101, 102, 99.5, 101), candle(1, 101, 112, 100, 111)]
    out = simulate(sig(), cs, EXP)
    assert out.status == "target"
    assert out.exit_price == pytest.approx(110 * (1 - SLIPPAGE))


def test_gap_below_stop_fills_at_open():
    cs = [candle(0, 101, 102, 99.5, 101), candle(1, 90, 91, 88, 89)]
    out = simulate(sig(), cs, EXP)
    assert out.status == "stop"
    assert out.exit_price == pytest.approx(90 * (1 - SLIPPAGE))


def test_time_exit_after_10_days():
    cs = [candle(i, 101, 102, 99.5 if i == 0 else 100, 101.5) for i in range(241)]
    out = simulate(sig(), cs, EXP)
    assert out.status == "time"
    assert out.exit_time == cs[239].close_time
    assert out.exit_price == pytest.approx(101.5 * (1 - SLIPPAGE))


def test_gap_in_candles_keeps_position_open():
    cs = [candle(0, 101, 102, 99.5, 101), candle(50, 101, 102, 100, 101)]
    assert simulate(sig(), cs, EXP).status == "open"


def test_unsorted_and_earlier_candles_ignored():
    cs = [candle(1, 101, 112, 100, 111), candle(0, 101, 102, 99.5, 101), candle(-1, 90, 91, 80, 85)]
    assert simulate(sig(), cs, EXP).status == "target"


def test_net_pnl_includes_fees_both_sides():
    out = Outcome("target", T0, 100.0, T0, 110.0)
    pnl, fees = net_pnl(out, 1.0)
    assert fees == pytest.approx(FEE * 210)
    assert pnl == pytest.approx(10 - 0.21)


WINDOWS = [time(3, 0), time(15, 0)]


def test_next_window_same_day():
    assert next_window(datetime(2026, 1, 1, 5, 0, tzinfo=UTC), WINDOWS) == datetime(2026, 1, 1, 15, 0, tzinfo=UTC)


def test_next_window_rolls_over_midnight():
    assert next_window(datetime(2026, 1, 1, 16, 0, tzinfo=UTC), WINDOWS) == datetime(2026, 1, 2, 3, 0, tzinfo=UTC)


def test_next_window_strictly_after():
    assert next_window(datetime(2026, 1, 1, 3, 0, tzinfo=UTC), WINDOWS) == datetime(2026, 1, 1, 15, 0, tzinfo=UTC)


def test_unrealized_pnl_includes_exit_slippage_and_fees():
    exit_ = 110 * (1 - SLIPPAGE)
    expected = 2 * (exit_ - 100.0) - FEE * 2 * (100.0 + exit_)
    assert unrealized_pnl(2.0, 100.0, 110.0) == pytest.approx(expected)
    # то же, что закрыть позицию по отметке
    assert unrealized_pnl(2.0, 100.0, 110.0) == pytest.approx(
        net_pnl(Outcome("time", T0, 100.0, T0 + H, exit_), 2.0)[0]
    )
