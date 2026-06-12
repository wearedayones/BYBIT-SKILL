"""
Trend pullback strategy (trend-following) — buy weakness in an uptrend,
sell strength in a downtrend.

Logic: when fast EMA > slow EMA (established uptrend) and price pulls back to
touch the fast EMA then closes back above it with a green candle, go long.
Mirror for shorts. This is the classic "buy the dip in trend" archetype —
profitable in trending regimes where mean-reversion strategies bleed.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from strategies.base import Signal  # noqa: E402

NAME = "trend_pullback"


def _ema(values: list[float], period: int) -> float:
    # SMA-seeded EMA (standard convention). A raw first-value seed over a
    # short window leaves ~2% seed weight and can flip touch decisions;
    # SMA seed + 3x period window keeps error < 0.1% vs full history
    # (see tests/test_strategies.py).
    if len(values) < period:
        return sum(values) / len(values)
    e = sum(values[:period]) / period
    k = 2 / (period + 1)
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def detect(candles, params) -> Signal | None:
    fast_n = params.get("ema_fast", 21)
    slow_n = params.get("ema_slow", 55)
    if len(candles) < slow_n * 3 + 2:
        return None

    closes = [c.close for c in candles]
    ema_fast = _ema(closes[-(slow_n * 3):], fast_n)
    ema_slow = _ema(closes[-(slow_n * 3):], slow_n)
    c = candles[-1]

    # Uptrend: pullback touches fast EMA, candle closes back above it, green
    if ema_fast > ema_slow and c.low <= ema_fast and c.close > ema_fast and c.close > c.open:
        return Signal(NAME, "long", c.__dict__,
                      {"ema_fast": round(ema_fast, 2), "ema_slow": round(ema_slow, 2)},
                      stop_hint=c.low, key_price=round(ema_fast, 2))

    # Downtrend: pullback touches fast EMA from below, closes back under, red
    if ema_fast < ema_slow and c.high >= ema_fast and c.close < ema_fast and c.close < c.open:
        return Signal(NAME, "short", c.__dict__,
                      {"ema_fast": round(ema_fast, 2), "ema_slow": round(ema_slow, 2)},
                      stop_hint=c.high, key_price=round(ema_fast, 2))
    return None
