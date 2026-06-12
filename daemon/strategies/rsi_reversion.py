"""
RSI mean-reversion strategy — fade exhaustion extremes with a reversal trigger.

Logic: when Wilder RSI on the prior bar is below `oversold` and the current
candle closes green (reversal confirmation), go long. Mirror for shorts above
`overbought`. The reversal-candle requirement avoids catching falling knives —
we need the market to show its hand first.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from strategies.base import Signal  # noqa: E402

NAME = "rsi_reversion"


def _rsi(closes: list[float], period: int) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    avg_g, avg_l = gains / period, losses / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_g = (avg_g * (period - 1) + max(d, 0)) / period
        avg_l = (avg_l * (period - 1) + max(-d, 0)) / period
    if avg_l == 0:
        return 100.0
    return 100 - 100 / (1 + avg_g / avg_l)


def detect(candles, params) -> Signal | None:
    period = params.get("rsi_period", 14)
    oversold = params.get("oversold", 30)
    overbought = params.get("overbought", 70)
    # Wilder RSI is recursive: with only 3x period of history the smoothed
    # averages haven't converged (off by several RSI points). 8x period keeps
    # truncation error < 1 RSI point (see tests/test_strategies.py).
    if len(candles) < period * 8 + 2:
        return None

    closes_prev = [c.close for c in candles[:-1]][-(period * 8):]
    rsi_prev = _rsi(closes_prev, period)
    c = candles[-1]
    recent = candles[-4:-1]

    # Oversold + green reversal candle -> long
    if rsi_prev < oversold and c.close > c.open:
        return Signal(NAME, "long", c.__dict__,
                      {"rsi_prev": round(rsi_prev, 1)},
                      stop_hint=min(min(r.low for r in recent), c.low),
                      key_price=round(c.close, 2))

    # Overbought + red reversal candle -> short
    if rsi_prev > overbought and c.close < c.open:
        return Signal(NAME, "short", c.__dict__,
                      {"rsi_prev": round(rsi_prev, 1)},
                      stop_hint=max(max(r.high for r in recent), c.high),
                      key_price=round(c.close, 2))
    return None
