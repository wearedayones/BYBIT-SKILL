"""
Reference-value tests for the strategy math primitives.

These exist so redesign rounds (strategy-designer agent) can't silently break
the indicator math. Run:  python -m pytest tests/ -q
"""

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "daemon"))

from sweep_filter import Candle  # noqa: E402
from strategies.rsi_reversion import _rsi  # noqa: E402
from strategies.trend_pullback import _ema  # noqa: E402
from strategies import zscore_reversion  # noqa: E402
from strategies.regime import detect_regime  # noqa: E402


# ---------- reference implementations (independent of production code) ----------

def wilder_rsi_full(closes: list[float], period: int) -> float:
    """Canonical full-history Wilder RSI."""
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


def ema_full(values: list[float], period: int) -> float:
    """SMA-seeded EMA over the entire history."""
    e = sum(values[:period]) / period
    k = 2 / (period + 1)
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def random_walk(n: int, seed: int, start: float = 50_000.0) -> list[float]:
    rng = random.Random(seed)
    out, p = [], start
    for _ in range(n):
        p *= 1 + rng.gauss(0, 0.003)
        out.append(p)
    return out


def candles_from_closes(closes: list[float]) -> list[Candle]:
    out = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c
        out.append(Candle(1_700_000_000_000 + i * 900_000, o,
                          max(o, c) * 1.0005, min(o, c) * 0.9995, c, 1000.0))
    return out


# ---------------------------------- RSI ----------------------------------

def test_rsi_all_gains_is_100():
    closes = [100 + i for i in range(30)]
    assert _rsi(closes, 14) == 100.0


def test_rsi_symmetric_chop_near_50():
    closes = [100.0]
    for i in range(60):
        closes.append(closes[-1] + (1 if i % 2 == 0 else -1))
    assert abs(_rsi(closes, 14) - 50.0) < 5.0


def test_rsi_truncated_window_converges():
    """The production 8x-period window must stay within 1 RSI point of the
    full-history Wilder value. (The old 3x window failed this.)"""
    period = 14
    for seed in range(10):
        closes = random_walk(3000, seed)
        full = wilder_rsi_full(closes, period)
        truncated = _rsi(closes[-(period * 8):], period)
        assert abs(truncated - full) < 1.0, (
            f"seed={seed}: truncated={truncated:.2f} full={full:.2f}")


def test_rsi_old_3x_window_was_insufficient():
    """Documents why the window was widened: 3x period deviates > 1 point."""
    period = 14
    worst = max(
        abs(_rsi(random_walk(3000, seed)[-(period * 3):], period)
            - wilder_rsi_full(random_walk(3000, seed), period))
        for seed in range(10)
    )
    assert worst > 1.0  # if this ever fails, the widening was unnecessary — fine


# ---------------------------------- EMA ----------------------------------

def test_ema_constant_series():
    assert abs(_ema([42.0] * 100, 21) - 42.0) < 1e-9


def test_ema_truncated_window_converges():
    """Production 3x-slow window + SMA seed must be within 0.1% of the
    full-history EMA for every slow period in the search space."""
    for slow in (50, 55, 89):
        for seed in range(10):
            closes = random_walk(3000, seed)
            full = ema_full(closes, slow)
            truncated = _ema(closes[-(slow * 3):], slow)
            rel_err = abs(truncated - full) / full
            assert rel_err < 0.001, (
                f"slow={slow} seed={seed}: rel_err={rel_err:.5f}")


# -------------------------------- z-score --------------------------------

def test_zscore_excludes_current_close():
    """Pins the intended semantics: z-score of the CURRENT close against the
    distribution of the PRIOR `lookback` closes (current excluded). The
    analyst's mean-reversion target (context['mean']) must be that prior mean."""
    look = 96
    base = [100.0 + (i % 5) * 0.1 for i in range(look + 4)]  # > look+2 history
    window = base[-look:]  # what detect() will use as the prior window
    mean = sum(window) / look
    var = sum((x - mean) ** 2 for x in window) / look
    std = var ** 0.5
    # Current candle: a green candle far below the mean -> long signal
    crash_close = mean - 3.0 * std
    closes = base + [crash_close]
    candles = candles_from_closes(closes)
    # force green candle (close > open)
    candles[-1] = Candle(candles[-1].ts, crash_close - 0.01,
                         crash_close + 0.01, crash_close - 0.02, crash_close, 1000.0)

    sig = zscore_reversion.detect(candles, {"lookback": look, "z_entry": 2.0})
    assert sig is not None and sig.direction == "long"
    # context mean must be the PRIOR-window mean (current close not mixed in)
    assert abs(sig.context["mean"] - round(mean, 2)) < 0.011
    assert abs(sig.context["zscore"] - (-3.0)) < 0.05


def test_zscore_no_signal_inside_band():
    look = 96
    closes = [100.0 + (i % 5) * 0.1 for i in range(look)] + [100.2]
    sig = zscore_reversion.detect(candles_from_closes(closes),
                                  {"lookback": look, "z_entry": 2.0})
    assert sig is None


# -------------------------------- regime ---------------------------------

def test_regime_returns_valid_label():
    closes = random_walk(600, seed=1)
    regime = detect_regime(candles_from_closes(closes), {})
    assert regime in ("trending_up", "trending_down", "ranging")


def test_regime_strong_trend_detected():
    closes = [100.0 * (1.002 ** i) for i in range(600)]  # relentless uptrend
    regime = detect_regime(candles_from_closes(closes), {})
    assert regime == "trending_up"
