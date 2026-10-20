import pandas as pd


def ema(s: pd.Series, n: int) -> pd.Series:
    """EMA with span=n, seeded with the first value."""
    return s.ewm(span=n, adjust=False).mean()


def _wilder(s: pd.Series, n: int) -> pd.Series:
    """Wilder smoothing: EMA with alpha=1/n, seeded with the first valid value.

    The first n-1 values are NaN (min_periods=n).
    """
    return s.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def atr(df: pd.DataFrame, n: int) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return _wilder(tr, n)


def rsi(s: pd.Series, n: int) -> pd.Series:
    delta = s.diff()
    avg_gain = _wilder(delta.clip(lower=0), n)
    avg_loss = _wilder((-delta).clip(lower=0), n)
    out = 100 - 100 / (1 + avg_gain / avg_loss)
    out[(avg_loss == 0) & (avg_gain > 0)] = 100.0
    out[(avg_loss == 0) & (avg_gain == 0)] = 50.0
    return out


def donchian_high(df: pd.DataFrame, n: int) -> pd.Series:
    """Max high of the n previous bars, excluding the current one."""
    return df["high"].shift(1).rolling(n).max()
