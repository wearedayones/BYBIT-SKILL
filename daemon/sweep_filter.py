"""
Deterministic pre-filter: detect liquidity-sweep candidates in pure Python.
Claude Code is only invoked when this returns a candidate, saving ~90% of wakes.

A "sweep" here = price wicks through a liquidity level and CLOSES back on the
other side of it within the same 15m candle.
  - Bearish setup: high > buyside level (swing high / equal highs / PDH), close < level
  - Bullish setup: low < sellside level (swing low / equal lows / PDL), close > level
"""

from dataclasses import dataclass, field, asdict


@dataclass
class Candle:
    ts: int      # open time, ms
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Level:
    price: float
    kind: str          # "swing_high" | "swing_low" | "equal_highs" | "equal_lows" | "pdh" | "pdl"
    ts: int            # when the level was formed
    touches: int = 1


@dataclass
class SweepCandidate:
    direction: str          # "short" (buyside swept) | "long" (sellside swept)
    level: dict
    candle: dict
    wick_pct: float         # wick beyond level as % of candle range
    close_back_inside: bool = True
    extras: dict = field(default_factory=dict)


def swing_points(candles: list[Candle], n: int = 2) -> tuple[list[Level], list[Level]]:
    """Fractal swings: a high higher than n candles on each side (and inverse for lows)."""
    highs, lows = [], []
    for i in range(n, len(candles) - n):
        c = candles[i]
        if all(c.high > candles[j].high for j in range(i - n, i + n + 1) if j != i):
            highs.append(Level(price=c.high, kind="swing_high", ts=c.ts))
        if all(c.low < candles[j].low for j in range(i - n, i + n + 1) if j != i):
            lows.append(Level(price=c.low, kind="swing_low", ts=c.ts))
    return highs, lows


def cluster_equal_levels(levels: list[Level], tol_pct: float, kind: str) -> list[Level]:
    """Group swings within tol_pct of each other -> 'equal highs/lows' (resting liquidity)."""
    out: list[Level] = []
    used = set()
    for i, a in enumerate(levels):
        if i in used:
            continue
        group = [a]
        for j, b in enumerate(levels[i + 1:], start=i + 1):
            if j in used:
                continue
            if abs(b.price - a.price) / a.price * 100 <= tol_pct:
                group.append(b)
                used.add(j)
        if len(group) >= 2:
            avg = sum(g.price for g in group) / len(group)
            out.append(Level(price=avg, kind=kind, ts=max(g.ts for g in group), touches=len(group)))
    return out


def build_levels(candles: list[Candle], cfg: dict) -> list[Level]:
    lookback = cfg.get("lookback_candles", 192)   # ~2 days of 15m
    window = candles[-lookback:]
    highs, lows = swing_points(window, n=cfg.get("swing_strength", 2))

    tol = cfg.get("equal_level_tolerance_pct", 0.05)
    levels = (
        highs[-10:] + lows[-10:]
        + cluster_equal_levels(highs, tol, "equal_highs")
        + cluster_equal_levels(lows, tol, "equal_lows")
    )

    # Prior-day high/low (UTC day boundaries)
    day_ms = 86_400_000
    today_start = (window[-1].ts // day_ms) * day_ms
    prev = [c for c in window if today_start - day_ms <= c.ts < today_start]
    if prev:
        levels.append(Level(price=max(c.high for c in prev), kind="pdh", ts=today_start))
        levels.append(Level(price=min(c.low for c in prev), kind="pdl", ts=today_start))
    return levels


def detect_sweep(candles: list[Candle], cfg: dict) -> SweepCandidate | None:
    """Check whether the LAST CLOSED candle swept any tracked level."""
    if len(candles) < 30:
        return None
    c = candles[-1]
    rng = max(c.high - c.low, 1e-12)
    levels = build_levels(candles[:-1], cfg)  # levels must pre-exist the sweep candle
    min_wick = cfg.get("min_wick_pct", 25.0)

    best: SweepCandidate | None = None
    for lv in levels:
        # Buyside swept -> potential SHORT
        if lv.kind in ("swing_high", "equal_highs", "pdh") and c.high > lv.price and c.close < lv.price:
            wick_pct = (c.high - lv.price) / rng * 100
            upper_wick_pct = (c.high - max(c.open, c.close)) / rng * 100
            if upper_wick_pct >= min_wick:
                cand = SweepCandidate("short", asdict(lv), asdict(c), round(wick_pct, 2),
                                      extras={"upper_wick_pct": round(upper_wick_pct, 2)})
                # prefer the outermost swept pool (closest to the wick high)
                if best is None or lv.price > best.level["price"]:
                    best = cand
        # Sellside swept -> potential LONG
        if lv.kind in ("swing_low", "equal_lows", "pdl") and c.low < lv.price and c.close > lv.price:
            wick_pct = (lv.price - c.low) / rng * 100
            lower_wick_pct = (min(c.open, c.close) - c.low) / rng * 100
            if lower_wick_pct >= min_wick:
                cand = SweepCandidate("long", asdict(lv), asdict(c), round(wick_pct, 2),
                                      extras={"lower_wick_pct": round(lower_wick_pct, 2)})
                # prefer the outermost swept pool (closest to the wick low)
                if best is None or (best.direction == "long" and lv.price < best.level["price"]):
                    best = cand
    return best
