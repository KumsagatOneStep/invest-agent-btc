import pandas as pd
import pytest

from invest_agent.indicators import atr, donchian_high, ema, rsi


def test_ema_matches_manual():
    s = pd.Series([1.0, 2.0, 3.0, 4.0], index=[10, 11, 12, 13])
    # span=3 -> alpha=0.5, seeded with the first value
    out = ema(s, 3)
    assert list(out.index) == [10, 11, 12, 13]
    assert out.tolist() == pytest.approx([1.0, 1.5, 2.25, 3.125])


def test_atr_constant_range_equals_range():
    n = 20
    close = pd.Series([100.0] * n)
    df = pd.DataFrame({"high": close + 1, "low": close - 1, "close": close})
    out = atr(df, 5)
    assert out.iloc[:4].isna().all()
    assert out.iloc[4:].tolist() == pytest.approx([2.0] * (n - 4))


def test_atr_uses_gap_and_wilder_seed():
    df = pd.DataFrame(
        {"high": [10.0, 15.0], "low": [8.0, 14.0], "close": [9.0, 14.5]}
    )
    # TR0 = 2 (no prev close), TR1 = max(1, 6, 5) = 6; alpha=0.5 -> 4
    assert atr(df, 2).iloc[1] == pytest.approx(4.0)


def test_rsi_all_up_is_100():
    s = pd.Series(range(1, 21), dtype=float)
    out = rsi(s, 14)
    assert out.iloc[14:].tolist() == pytest.approx([100.0] * 6)


def test_rsi_all_down_is_0():
    s = pd.Series(range(20, 0, -1), dtype=float)
    assert rsi(s, 14).iloc[-1] == pytest.approx(0.0)


def test_rsi_manual():
    s = pd.Series([10.0, 12.0, 11.0])
    # n=2, alpha=0.5, seeded with first delta: gains [2,0] -> 1.0, losses [0,1] -> 0.5, RS=2
    assert rsi(s, 2).iloc[2] == pytest.approx(100 - 100 / 3)


def test_donchian_excludes_current_bar():
    df = pd.DataFrame({"high": [1.0, 5.0, 3.0, 9.0, 2.0]})
    out = donchian_high(df, 2)
    assert out.isna().tolist()[:2] == [True, True]
    assert out.iloc[2:].tolist() == [5.0, 5.0, 9.0]
