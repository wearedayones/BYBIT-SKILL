"""
Range breakout strategy (momentum) — the regime complement to sweeps.

Logic: when a 15m candle is the FIRST close beyond the high/low of the last
`range_lookback` candles, with a displacement body (>= min_body_atr × ATR) and
a strong close (in the outer third of its range), trade WITH the break.

Sweeps profit when levels reject; this profits when they give way — together
they cover both regimes. Disabled by default until it passes BACKTEST.md.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from strategies.base import Signal  # noqa: E402

NAME = "breakout"


def _atr(candles, n) -> float:
    trs = []
    for i in range(len(candles) - n, len(candles)):
        c, p = candles[i], candles[i - 1]
        trs.append(max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close)))
    return sum(trs) / len(trs)


def detect(candles, params) -> Signal | None:
    look = params.get("range_lookback", 48)
    atr_n = params.get("atr_period", 14)
    min_body = params.get("min_body_atr", 1.2)
    if len(candles) < look + atr_n + 2:
        return None

    c = candles[-1]
    prev = candles[-2]
    window = candles[-(look + 1):-1]          # the range, excluding the breakout candle
    hh = max(w.high for w in window)
    ll = min(w.low for w in window)
    a = _atr(candles[:-1], atr_n)
    body = abs(c.close - c.open)
    rng = max(c.high - c.low, 1e-12)
    if a <= 0 or body < min_body * a:
        return None

    # Long: first close above range high, strong close near the candle high
    if c.close > hh and prev.close <= hh and (c.close - c.low) / rng >= 0.66:
        return Signal(NAME, "long", c.__dict__, 
                      {"range_high": hh, "range_low": ll, "body_atr": round(body / a, 2)},
                      stop_hint=min(c.low, hh), key_price=hh)

    # Short: first close below range low, strong close near the candle low
    if c.close < ll and prev.close >= ll and (c.high - c.close) / rng >= 0.66:
        return Signal(NAME, "short", c.__dict__,
                      {"range_high": hh, "range_low": ll, "body_atr": round(body / a, 2)},
                      stop_hint=max(c.high, ll), key_price=ll)
    return None
