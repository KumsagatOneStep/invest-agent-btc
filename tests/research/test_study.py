import pandas as pd
import pytest

from invest_agent.research.events import breakout, momentum_top
from invest_agent.research.study import ROUND_TRIP_COST, event_study, forward_returns

DAYS = pd.date_range("2024-01-01", periods=10, freq="D", tz="UTC")


def frame(data: dict[str, list[float]]) -> pd.DataFrame:
    return pd.DataFrame(data, index=DAYS[: len(next(iter(data.values())))])


def test_forward_returns_shift_by_horizon():
    closes = frame({"A": [100, 110, 121, 133.1]})
    fwd = forward_returns(closes, 1)
    assert fwd["A"].iloc[0] == pytest.approx(0.10)
    assert pd.isna(fwd["A"].iloc[-1])


def test_momentum_top_picks_strongest_past_return():
    closes = frame({"A": [100, 120, 130], "B": [100, 90, 95], "C": [100, 105, 104]})
    member = closes.notna()
    ev = momentum_top(closes, member, lookback=1, top=1)
    assert ev.loc[DAYS[1]].to_dict() == {"A": True, "B": False, "C": False}
    assert not ev.loc[DAYS[0]].any()  # нет прошлого — нет события


def test_momentum_ignores_non_members():
    closes = frame({"A": [100, 200], "B": [100, 110]})
    member = frame({"A": [True, False], "B": [True, True]})
    assert momentum_top(closes, member, lookback=1, top=1).loc[DAYS[1]].to_dict() == {
        "A": False, "B": True,
    }


def test_breakout_uses_previous_highs_only():
    closes = frame({"A": [10, 11, 12, 11, 13]})
    ev = breakout(closes, closes.notna(), n=2)
    assert ev["A"].tolist() == [False, False, True, False, True]


def test_event_study_excess_vs_members_and_net_of_cost():
    # A всегда +10% на следующий день, B 0% — событие на A даёт избыток 5% к среднему по вселенной
    closes = frame({"A": [100 * 1.1**i for i in range(6)], "B": [100.0] * 6})
    member = closes.notna()
    events = pd.DataFrame(False, index=closes.index, columns=closes.columns)
    events["A"] = True
    r = event_study(events, closes, member, horizon=1, step=1, bench="B")
    assert r.n_dates == 5 and r.n_events == 5
    assert r.mean_event == pytest.approx(0.10)
    assert r.mean_excess == pytest.approx(0.05)
    assert r.mean_vs_bench == pytest.approx(0.10)
    assert r.mean_net == pytest.approx(0.10 - ROUND_TRIP_COST)
    assert set(r.by_year) == {2024}
    assert r.by_symbol.loc["A", "events"] == 5


def test_event_study_step_samples_non_overlapping_dates():
    closes = frame({"A": [100 * 1.1**i for i in range(10)], "B": [100.0] * 10})
    events = closes.notna()
    r = event_study(events, closes, closes.notna(), horizon=3, step=3, bench="B")
    assert r.n_dates == 3  # дни 0, 3, 6; с дня 9 нет доходности вперёд


def test_momentum_bottom_picks_weakest():
    closes = frame({"A": [100, 120], "B": [100, 90], "C": [100, 105]})
    ev = momentum_top(closes, closes.notna(), lookback=1, top=1, bottom=True)
    assert ev.loc[DAYS[1]].to_dict() == {"A": False, "B": True, "C": False}


def test_by_symbol_sorted_by_absolute_contribution():
    closes = frame({"A": [100, 101, 102], "B": [100, 50, 25]})
    r = event_study(closes.notna(), closes, closes.notna(), horizon=1, step=1, bench="A")
    assert r.by_symbol.index[0] == "B"
