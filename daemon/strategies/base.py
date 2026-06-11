"""
base.py — the strategy contract.

A strategy is anything that implements:
    NAME: str
    detect(candles: list[Candle], params: dict) -> Signal | None

The Signal carries everything downstream layers need; the watcher, backtester,
orchestrator, risk-manager, and executor never contain strategy-specific logic.
"""

from dataclasses import dataclass, field


@dataclass
class Signal:
    strategy: str        # e.g. "sweep", "breakout" — also selects the analyst agent
    direction: str       # "long" | "short"
    candle: dict         # the closed candle that produced the signal
    context: dict        # strategy-specific (swept level, broken range, ...)
    stop_hint: float     # price at which the setup is structurally invalid
    key_price: float     # level identifying this setup (for dedupe / cooldown)
    notes: dict = field(default_factory=dict)
