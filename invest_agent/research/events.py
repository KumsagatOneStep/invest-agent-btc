"""События для событийного анализа: булевы таблицы дата × монета (True — событие на закрытии дня)."""

import pandas as pd


def momentum_top(
    closes: pd.DataFrame, member: pd.DataFrame, lookback: int, top: int, bottom: bool = False
) -> pd.DataFrame:
    """Монеты вселенной с наибольшей (bottom=True — наименьшей) доходностью за `lookback` дней."""
    past = (closes / closes.shift(lookback) - 1).where(member)
    rank = past.rank(axis=1, ascending=bottom, method="first")
    return (rank <= top).fillna(False).astype(bool)


def breakout(closes: pd.DataFrame, member: pd.DataFrame, n: int) -> pd.DataFrame:
    """Закрытие выше максимума закрытий `n` предыдущих дней."""
    prev_high = closes.shift(1).rolling(n).max()
    return ((closes > prev_high) & member).fillna(False).astype(bool)


def crash_volume(
    closes: pd.DataFrame,
    volumes: pd.DataFrame,
    member: pd.DataFrame,
    drop: float,
    vol_k: float,
    vol_n: int = 20,
) -> pd.DataFrame:
    """Падение за день сильнее `drop` при объёме больше `vol_k` × среднего за `vol_n` прошлых дней."""
    ret = closes / closes.shift(1) - 1
    avg_vol = volumes.shift(1).rolling(vol_n).mean()
    return ((ret <= -drop) & (volumes >= vol_k * avg_vol) & member).fillna(False).astype(bool)
