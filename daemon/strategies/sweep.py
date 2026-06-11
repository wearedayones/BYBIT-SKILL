"""Liquidity sweep strategy (mean-reversion) — wraps the proven detector."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sweep_filter import detect_sweep  # noqa: E402
from strategies.base import Signal  # noqa: E402

NAME = "sweep"


def detect(candles, params) -> Signal | None:
    cand = detect_sweep(candles, params)
    if not cand:
        return None
    stop_hint = cand.candle["high"] if cand.direction == "short" else cand.candle["low"]
    return Signal(
        strategy=NAME,
        direction=cand.direction,
        candle=cand.candle,
        context={"level": cand.level, "wick_pct": cand.wick_pct, **cand.extras},
        stop_hint=stop_hint,
        key_price=cand.level["price"],
    )
