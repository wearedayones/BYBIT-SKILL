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
