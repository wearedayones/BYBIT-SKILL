"""
risk_engine.py — deterministic position-sizing and trade-permission math.

The risk-manager agent calls this CLI before sizing any trade. Every output
is a NUMBER the agent must respect; config.yaml -> risk: values remain
immutable CEILINGS — everything here can only size DOWN from them, never up.

Checks (in order):
  1. Equity floor      — equity < equity_floor_pct of starting equity => HALT
  2. Weekly drawdown   — rolling 7-day PnL <= -max_weekly_loss_pct => HALT 48h
  3. Daily stop        — daily_pnl_pct <= -max_daily_loss_pct => HALT today
  4. Streak throttle   — 3 losses: risk x0.5; 5 losses: risk x0.25; win resets
  5. Fractional Kelly  — quarter-Kelly from rolling stats, capped at ceiling
  6. Correlation gate  — strategy vs open positions' strategies, r > 0.6 blocks

Usage:
    python daemon/risk_engine.py --strategy sweep --equity 10000
    python daemon/risk_engine.py --strategy sweep --equity 10000 --json
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
JOURNAL = ROOT / "state" / "journal.json"
CFG = yaml.safe_load((ROOT / "config.yaml").read_text())

KELLY_FRACTION = 0.25       # quarter-Kelly: standard practitioner safety factor
MIN_TRADES_FOR_KELLY = 20   # below this, edge estimate too noisy — use ceiling
STREAK_HALF_AT = 3          # consecutive losses before risk x0.5
STREAK_QUARTER_AT = 5       # consecutive losses before risk x0.25
CORRELATION_BLOCK_R = 0.6
CORRELATION_MIN_OVERLAP = 10  # need >= N paired returns to trust the estimate


def _closed_trades(journal: dict) -> list[dict]:
    """Runs that ended in a closed trade with a realized R (newest last)."""
    out = []
    for run in journal.get("runs", []):
        order = run.get("order") or {}
        if "realized_r" in order:
            out.append({"ts": run.get("ts"),
                        "strategy": run.get("strategy")
                                    or run.get("event", "").replace("_signal", ""),
                        "r": float(order["realized_r"])})
    return out


def streak_multiplier(journal: dict, strategy: str) -> tuple[float, int]:
    """Consecutive-loss throttle across ALL strategies (tilt is account-wide)."""
    trades = _closed_trades(journal)
    streak = 0
    for t in reversed(trades):
        if t["r"] <= 0:
            streak += 1
        else:
            break
    if streak >= STREAK_QUARTER_AT:
        return 0.25, streak
    if streak >= STREAK_HALF_AT:
        return 0.5, streak
    return 1.0, streak


def kelly_risk_pct(journal: dict, strategy: str, ceiling_pct: float) -> tuple[float, dict]:
    """Quarter-Kelly from the strategy's rolling live stats, capped at ceiling.
    kelly_f = win_rate - (1 - win_rate) / avg_rr   (classic Kelly for fixed odds)
    """
    trades = [t for t in _closed_trades(journal) if t["strategy"] == strategy][-50:]
    detail = {"live_trades_used": len(trades)}
    if len(trades) < MIN_TRADES_FOR_KELLY:
        detail["basis"] = f"fewer than {MIN_TRADES_FOR_KELLY} live trades — ceiling used"
        return ceiling_pct, detail

    wins = [t["r"] for t in trades if t["r"] > 0]
    losses = [t["r"] for t in trades if t["r"] <= 0]
    win_rate = len(wins) / len(trades)
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 1.0
    rr = avg_win / avg_loss if avg_loss > 0 else 0.0
    detail.update({"win_rate": round(win_rate, 3), "avg_rr": round(rr, 2)})

    if rr <= 0:
        detail["basis"] = "no winning trades in window — minimum risk"
        return round(ceiling_pct * 0.25, 3), detail

    kelly_f = win_rate - (1 - win_rate) / rr
    quarter = max(kelly_f, 0.0) * KELLY_FRACTION * 100  # as pct of equity
    risk = min(ceiling_pct, round(quarter, 3))
    detail.update({"kelly_f": round(kelly_f, 4),
                   "quarter_kelly_pct": round(quarter, 3),
                   "basis": "quarter-Kelly (capped at ceiling)" if quarter < ceiling_pct
                            else "ceiling (quarter-Kelly above it)"})
    # Negative edge in live stats -> floor at 25% of ceiling, never zero
    # (zero would blind the monitor — it needs trades to measure recovery).
    return max(risk, round(ceiling_pct * 0.25, 3)), detail


def weekly_pnl_pct(journal: dict) -> float:
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    total = 0.0
    for t in _closed_trades(journal):
        try:
            ts = datetime.fromisoformat(t["ts"].replace("Z", "+00:00"))
        except (ValueError, AttributeError, TypeError):
            continue
        if ts >= cutoff:
            # r is in R-multiples; convert via the per-trade risk pct ceiling
            total += t["r"] * CFG["risk"]["risk_per_trade_pct"]
    return round(total, 2)


def pearson_r(a: list[float], b: list[float]) -> float:
    n = min(len(a), len(b))
    if n < 2:
        return 0.0
    a, b = a[-n:], b[-n:]
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    va = sum((x - ma) ** 2 for x in a) ** 0.5
    vb = sum((y - mb) ** 2 for y in b) ** 0.5
    if va == 0 or vb == 0:
        return 0.0
    return cov / (va * vb)


def correlation_gate(journal: dict, strategy: str) -> tuple[bool, dict]:
    """Block if the firing strategy's recent returns correlate > 0.6 with any
    strategy that currently has an open position."""
    open_strategies = {p.get("strategy") for p in journal.get("open_positions", [])
                       if p.get("strategy") and p.get("strategy") != strategy}
    if not open_strategies:
        return True, {"open_strategies_checked": []}
    mine = [t["r"] for t in _closed_trades(journal) if t["strategy"] == strategy][-20:]
    checked = {}
    for other in open_strategies:
        theirs = [t["r"] for t in _closed_trades(journal) if t["strategy"] == other][-20:]
        if min(len(mine), len(theirs)) < CORRELATION_MIN_OVERLAP:
            checked[other] = "insufficient data — allowed"
            continue
        r = round(pearson_r(mine, theirs), 3)
        checked[other] = r
        if r > CORRELATION_BLOCK_R:
            return False, {"open_strategies_checked": checked, "blocked_by": other}
    return True, {"open_strategies_checked": checked}


def evaluate(strategy: str, equity: float) -> dict:
    journal = json.loads(JOURNAL.read_text()) if JOURNAL.exists() else {}
    risk_cfg = CFG["risk"]
    ceiling = risk_cfg["risk_per_trade_pct"]
    out = {"strategy": strategy, "equity": equity, "checks": {}}

    # 1. Equity floor
    floor_pct = risk_cfg.get("equity_floor_pct", 50)
    start_eq = journal.get("starting_equity")
    if start_eq and equity < start_eq * floor_pct / 100:
        out["verdict"] = "HALT"
        out["checks"]["equity_floor"] = (
            f"FAIL: equity {equity} < {floor_pct}% of starting {start_eq}")
        return out
    out["checks"]["equity_floor"] = "OK" if start_eq else "SKIP (no starting_equity recorded)"

    # 2. Weekly drawdown
    max_weekly = risk_cfg.get("max_weekly_loss_pct", 6.0)
    wk = weekly_pnl_pct(journal)
    if wk <= -max_weekly:
        out["verdict"] = "HALT"
        out["checks"]["weekly_drawdown"] = f"FAIL: 7d PnL {wk}% <= -{max_weekly}% — halt 48h"
        return out
    out["checks"]["weekly_drawdown"] = f"OK ({wk}% vs -{max_weekly}% limit)"

    # 3. Daily stop
    daily = journal.get("daily_pnl_pct", 0.0)
    if daily <= -risk_cfg["max_daily_loss_pct"]:
        out["verdict"] = "HALT"
        out["checks"]["daily_stop"] = f"FAIL: daily {daily}%"
        return out
    out["checks"]["daily_stop"] = f"OK ({daily}%)"

    # 4. Correlation gate
    allowed, corr_detail = correlation_gate(journal, strategy)
    out["checks"]["correlation"] = corr_detail
    if not allowed:
        out["verdict"] = "VETO"
        return out

    # 5+6. Kelly x streak (both only ever reduce)
    kelly_pct, kelly_detail = kelly_risk_pct(journal, strategy, ceiling)
    mult, streak = streak_multiplier(journal, strategy)
    final = round(min(kelly_pct * mult, ceiling), 3)
    out["checks"]["kelly"] = kelly_detail
    out["checks"]["streak"] = {"consecutive_losses": streak, "multiplier": mult}
    out["risk_pct"] = final
    out["risk_usd"] = round(equity * final / 100, 2)
    out["ceiling_pct"] = ceiling
    out["verdict"] = "SIZED"
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", required=True)
    ap.add_argument("--equity", type=float, required=True,
                    help="current account equity in USDT")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    result = evaluate(args.strategy, args.equity)
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["verdict"] == "SIZED" else 1)


if __name__ == "__main__":
    main()
