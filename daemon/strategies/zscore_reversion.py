"""
Z-score mean-reversion — fade statistically extreme deviations from the
rolling mean, with a reversal-candle trigger.

Logic: compute the z-score of the current close against the mean/std of the
prior `lookback` closes. If price is more than `z_entry` standard deviations
below the mean AND the candle closes green, go long back toward the mean.
Mirror for shorts. Pure statistics, no levels — diversifies the library away
from price-action pattern detectors.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from strategies.base import Signal  # noqa: E402

NAME = "zscore_reversion"


def detect(candles, params) -> Signal | None:
    look = params.get("lookback", 96)
    z_entry = params.get("z_entry", 2.0)
    if len(candles) < look + 2:
        return None

    closes = [c.close for c in candles[-(look + 1):-1]]
    mean = sum(closes) / len(closes)
    var = sum((x - mean) ** 2 for x in closes) / len(closes)
    std = var ** 0.5
    if std <= 0:
        return None

    c = candles[-1]
    z = (c.close - mean) / std

    # Stretched far below the mean + green reversal candle -> long
    if z < -z_entry and c.close > c.open:
        return Signal(NAME, "long", c.__dict__,
                      {"zscore": round(z, 2), "mean": round(mean, 2)},
                      stop_hint=c.low, key_price=round(mean, 2))

    # Stretched far above the mean + red reversal candle -> short
    if z > z_entry and c.close < c.open:
        return Signal(NAME, "short", c.__dict__,
                      {"zscore": round(z, 2), "mean": round(mean, 2)},
                      stop_hint=c.high, key_price=round(mean, 2))
    return None
