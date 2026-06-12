"""
Volatility squeeze breakout — compression precedes expansion.

Logic: when Bollinger Band relative width on the *prior* bar is in the lowest
`squeeze_pct` percent of the last `squeeze_lookback` bars (a volatility
squeeze), and the current candle closes outside the bands, trade WITH the
expansion. Distinct from plain range-breakout: the squeeze precondition means
we only trade breaks that follow genuine energy build-up.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from strategies.base import Signal  # noqa: E402

NAME = "vol_squeeze"


def _bands(closes: list[float], n: int) -> tuple[float, float, float, float]:
    window = closes[-n:]
    mean = sum(window) / n
    var = sum((x - mean) ** 2 for x in window) / n
    std = var ** 0.5
    width = (4 * std / mean) if mean else 0.0  # relative band width (2σ each side)
    return mean, mean + 2 * std, mean - 2 * std, width


def detect(candles, params) -> Signal | None:
    bb_n = params.get("bb_period", 20)
    look = params.get("squeeze_lookback", 64)
    squeeze_pct = params.get("squeeze_pct", 25)
    if len(candles) < bb_n + look + 2:
        return None

    closes = [c.close for c in candles]

    # Band width history over the lookback, ending at the PRIOR bar
    widths = []
    for i in range(len(closes) - look - 1, len(closes) - 1):
        _, _, _, w = _bands(closes[: i + 1], bb_n)
        widths.append(w)
    prior_width = widths[-1]
    sorted_w = sorted(widths)
    threshold = sorted_w[max(0, int(len(sorted_w) * squeeze_pct / 100) - 1)]
    if prior_width > threshold:
        return None  # no squeeze before this bar

    mid, upper, lower, _ = _bands(closes[:-1], bb_n)
    c = candles[-1]

    if c.close > upper:
        return Signal(NAME, "long", c.__dict__,
                      {"bb_upper": round(upper, 2), "bb_mid": round(mid, 2),
                       "squeeze_width": round(prior_width * 100, 3)},
                      stop_hint=min(c.low, mid), key_price=round(upper, 2))

    if c.close < lower:
        return Signal(NAME, "short", c.__dict__,
                      {"bb_lower": round(lower, 2), "bb_mid": round(mid, 2),
                       "squeeze_width": round(prior_width * 100, 3)},
                      stop_hint=max(c.high, mid), key_price=round(lower, 2))
    return None
