"""Technical indicators implemented with pandas/numpy only (no TA-Lib)."""
from __future__ import annotations

import re

import numpy as np
import pandas as pd


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False, min_periods=span).mean()


def sma(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window, min_periods=window).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.fillna(50.0)


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    line = ema(series, fast) - ema(series, slow)
    sig = line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return line, sig, line - sig


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["c"].shift(1)
    return pd.concat([
        df["h"] - df["l"],
        (df["h"] - prev_close).abs(),
        (df["l"] - prev_close).abs(),
    ], axis=1).max(axis=1)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    return true_range(df).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    up = df["h"].diff()
    down = -df["l"].diff()
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    tr = true_range(df).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(
        alpha=1 / period, adjust=False, min_periods=period).mean() / tr.replace(0, np.nan)
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(
        alpha=1 / period, adjust=False, min_periods=period).mean() / tr.replace(0, np.nan)
    dx = (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan) * 100
    return dx.ewm(alpha=1 / period, adjust=False, min_periods=period).mean().fillna(0)


def vwap(df: pd.DataFrame) -> pd.Series:
    """Session-anchored VWAP for intraday bars."""
    typical = (df["h"] + df["l"] + df["c"]) / 3
    if not isinstance(df.index, pd.DatetimeIndex):
        return (typical * df["v"]).cumsum() / df["v"].replace(0, np.nan).cumsum()
    day = df.index.tz_convert("Asia/Kolkata").date if df.index.tz is not None else df.index.date
    pv = (typical * df["v"]).groupby(day).cumsum()
    vol = df["v"].groupby(day).cumsum().replace(0, np.nan)
    return pv / vol


def anchored_vwap(df: pd.DataFrame, window: int = 20) -> pd.Series:
    """Rolling volume-weighted average price for daily bars."""
    typical = (df["h"] + df["l"] + df["c"]) / 3
    return (typical * df["v"]).rolling(window, min_periods=5).sum() / \
        df["v"].rolling(window, min_periods=5).sum().replace(0, np.nan)


def bollinger(series: pd.Series, window: int = 20, mult: float = 2.0):
    mid = sma(series, window)
    sd = series.rolling(window, min_periods=window).std()
    return mid, mid + mult * sd, mid - mult * sd


def enrich(df: pd.DataFrame, intraday: bool = False) -> pd.DataFrame:
    """Add every indicator the scanner needs. Never mutates the input."""
    if df is None or df.empty or len(df) < 5:
        return df
    out = df.copy()
    c = out["c"]

    for span in (9, 20, 50, 200):
        out[f"ema{span}"] = ema(c, span)
    out["rsi14"] = rsi(c, 14)
    line, sig, hist = macd(c)
    out["macd"] = line
    out["macd_signal"] = sig
    out["macd_hist"] = hist
    out["atr"] = atr(out, 14)
    out["atr_pct"] = out["atr"] / c * 100
    out["adx"] = adx(out, 14)
    out["vol_sma20"] = sma(out["v"], 20)
    out["rel_vol"] = out["v"] / out["vol_sma20"].replace(0, np.nan)
    out["roc5"] = c.pct_change(5, fill_method=None) * 100
    out["roc20"] = c.pct_change(20, fill_method=None) * 100

    if intraday:
        out["vwap"] = vwap(out)
    else:
        out["vwap"] = anchored_vwap(out, 20)

    mid, upper, lower = bollinger(c, 20)
    out["bb_mid"] = mid
    out["bb_upper"] = upper
    out["bb_lower"] = lower
    out["bb_width"] = (upper - lower) / mid.replace(0, np.nan) * 100

    roll_high = out["h"].rolling(min(len(out), 252), min_periods=20).max()
    roll_low = out["l"].rolling(min(len(out), 252), min_periods=20).min()
    out["high_52w"] = roll_high
    out["low_52w"] = roll_low
    out["pos_52w"] = (c - roll_low) / (roll_high - roll_low).replace(0, np.nan) * 100

    out["ret1"] = c.pct_change(1, fill_method=None) * 100
    out["ret5"] = c.pct_change(5, fill_method=None) * 100
    out["ret20"] = c.pct_change(20, fill_method=None) * 100
    return out


def _pandas_rule(rule: str) -> str:
    """Translate V4 shorthand: 5m/15m mean minutes, not pandas month-end."""
    text = str(rule or "15min").strip().lower()
    match = re.fullmatch(r"(\d+)\s*m", text)
    if match:
        return f"{int(match.group(1))}min"
    if text in ("m", "mo", "1mo", "month", "1month"):
        return "ME"
    aliases = {"d": "D", "1d": "D", "day": "D", "w": "W", "1w": "W", "week": "W"}
    return aliases.get(text, text)


def resample(frame: pd.DataFrame | None, rule: str = "15min") -> pd.DataFrame | None:
    """Build a higher-timeframe frame (e.g. 15m from 5m) for V4 confirmation."""
    if frame is None or frame.empty or not isinstance(frame.index, pd.DatetimeIndex):
        return None
    rule = _pandas_rule(rule)
    funcs = {"o": "first", "h": "max", "l": "min", "c": "last", "v": "sum"}
    cols = {key: funcs[key] for key in ("o", "h", "l", "c", "v") if key in frame}
    if len(cols) < 5:
        return None
    out = frame.resample(rule).agg(cols).dropna(subset=["o", "h", "l", "c"])
    if len(out) < 5:
        return None
    # Keep short histories too: indicators.last() substitutes safe defaults for
    # not-yet-available EMAs instead of discarding the whole frame.
    return enrich(out, intraday=True)


def last(df: pd.DataFrame | None, column: str, default: float = 0.0) -> float:
    """Safe last value of a column."""
    if df is None or df.empty or column not in df.columns:
        return default
    value = df[column].iloc[-1]
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    return default if not np.isfinite(value) else value
