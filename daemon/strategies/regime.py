"""
regime.py — market regime detection.

Returns "trending_up", "trending_down", or "ranging" based on EMA slope
normalized by ATR. Used by candle_watcher to gate strategies by regime so
mean-reversion setups (sweep) are suppressed during strong trends and
momentum setups (breakout) are suppressed during chop.
"""

from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sweep_filter import Candle  # noqa: E402


def _ema(values: list[float], period: int) -> list[float]:
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def _atr(candles: list[Candle], period: int) -> float:
    if len(candles) < 2:
        return 1e-12
    trs = []
    for i in range(1, len(candles)):
        c, p = candles[i], candles[i - 1]
        trs.append(max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close)))
    window = trs[-period:]
    return sum(window) / len(window) if window else 1e-12


def detect_regime(candles: list[Candle], params: dict) -> str:
    """
    Return 'trending_up' | 'trending_down' | 'ranging'.

    Uses the slope of the EMA over the last ema_period bars, normalized by
    the ATR, as a dimensionless trend strength indicator.

    params:
      ema_period       int   (default 50)  — EMA lookback
      atr_period       int   (default 14)  — ATR lookback for normalization
      trend_threshold  float (default 0.3) — slope magnitude to classify as trend
    """
    ema_period = int(params.get("ema_period", 50))
    atr_period = int(params.get("atr_period", 14))
    trend_threshold = float(params.get("trend_threshold", 0.3))

    if len(candles) < ema_period + atr_period:
        return "ranging"  # insufficient history → safe default

    closes = [c.close for c in candles]
    ema_vals = _ema(closes, ema_period)
    atr = _atr(candles[-(atr_period + 1):], atr_period)

    # Normalized slope: EMA change over ema_period bars expressed in ATR/bar units
    slope = (ema_vals[-1] - ema_vals[-ema_period]) / (atr * ema_period)

    if slope > trend_threshold:
        return "trending_up"
    if slope < -trend_threshold:
        return "trending_down"
    return "ranging"


# ---------------- volatility-rank regime (low / normal / high) ----------------

def _rolling_vol(closes: list[float], window: int) -> list[float | None]:
    """Rolling stddev of simple returns; index-aligned to closes.
    None until enough history. O(n) via running sums."""
    n = len(closes)
    out: list[float | None] = [None] * n
    if n < window + 1:
        return out
    rets = [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, n)]
    s = sum(rets[:window])
    sq = sum(r * r for r in rets[:window])
    for i in range(window, len(rets) + 1):
        if i > window:
            old, new = rets[i - window - 1], rets[i - 1]
            s += new - old
            sq += new * new - old * old
        m = s / window
        var = max(sq / window - m * m, 0.0)
        out[i] = var ** 0.5  # vol at close index i uses returns up to close i
    return out


def vol_regime_series(candles: list[Candle], params: dict | None = None) -> list[str]:
    """Per-candle volatility regime labels, trailing data only (no look-ahead).

    Current realized vol (stddev of returns over vol_window bars) is
    percentile-ranked against its own trailing vol_lookback distribution:
      rank < low_pct  -> 'low' | rank > high_pct -> 'high' | else 'normal'
    """
    params = params or {}
    window = int(params.get("vol_window", 24))
    lookback = int(params.get("vol_lookback", 480))
    low_pct = float(params.get("low_pct", 0.30))
    high_pct = float(params.get("high_pct", 0.70))

    closes = [c.close for c in candles]
    vols = _rolling_vol(closes, window)
    labels = []
    for i in range(len(candles)):
        v = vols[i]
        if v is None:
            labels.append("normal")
            continue
        hist = [x for x in vols[max(0, i - lookback):i + 1] if x is not None]
        if len(hist) < 60:  # need a real distribution before ranking means anything
            labels.append("normal")
            continue
        rank = sum(1 for x in hist if x <= v) / len(hist)
        labels.append("low" if rank < low_pct else "high" if rank > high_pct else "normal")
    return labels


def vol_regime(candles: list[Candle], params: dict | None = None) -> str:
    """Current volatility regime: 'low' | 'normal' | 'high'."""
    params = params or {}
    lookback = int(params.get("vol_lookback", 480))
    window = int(params.get("vol_window", 24))
    tail = candles[-(lookback + window + 2):]
    return vol_regime_series(tail, params)[-1]
