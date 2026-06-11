"""
backtest.py — replay real candles through the SAME detect_sweep() the live
daemon uses, simulate bracket trades, report edge metrics.

Usage:
    python backtest/backtest.py --csv backtest/data/BTCUSDT_15m.csv
    python backtest/backtest.py --csv ... --start 2025-06-01 --end 2026-03-01
    python backtest/backtest.py --csv ... --min-wick 30 --swing 3 --rr 2.0

Conservative conventions:
  * Entry = market at sweep-candle close, with slippage applied against you.
  * If SL and TP are both touched within one candle -> counted as a LOSS.
  * Round-trip taker fees converted to R and subtracted from every trade.

Results in R-multiples (1R = initial risk). Expectancy > ~0.05R after fees
across BOTH train and test windows is the minimum bar to take seriously.
"""

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "daemon"))
from sweep_filter import Candle, detect_sweep  # noqa: E402

ROOT = Path(__file__).resolve().parent


def load_csv(path: str) -> list[Candle]:
    out = []
    with open(path) as f:
        for row in csv.DictReader(f):
            out.append(Candle(int(row["ts"]), float(row["open"]), float(row["high"]),
                              float(row["low"]), float(row["close"]), float(row["volume"])))
    return out


def ts_of(date_str: str) -> int:
    return int(datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


def run(candles, fcfg, bcfg, fees_bps=5.5, slip_bps=2.0, max_hold=96, warmup=200):
    trades = []
    open_trade = None
    recent_levels = []  # (price, ts) of levels already traded, 1-day dedupe

    for i in range(warmup, len(candles)):
        c = candles[i]

        # --- manage open trade ---
        if open_trade:
            t = open_trade
            t["bars"] += 1
            hit_sl = c.high >= t["sl"] if t["dir"] == "short" else c.low <= t["sl"]
            hit_tp = c.low <= t["tp"] if t["dir"] == "short" else c.high >= t["tp"]
            done = None
            if hit_sl:                 # SL first / both-in-one-candle = loss
                done = -1.0
            elif hit_tp:
                done = t["rr"]
            elif t["bars"] >= max_hold:
                raw = (t["entry"] - c.close) if t["dir"] == "short" else (c.close - t["entry"])
                done = raw / t["risk"]
            if done is not None:
                t["r"] = done - t["fee_r"]
                t["exit_ts"] = c.ts
                trades.append(t)
                open_trade = None
            continue  # one position at a time, no same-candle re-entry

        # --- look for a new signal ---
        cand = detect_sweep(candles[: i + 1], fcfg)
        if not cand:
            continue
        lp = cand.level["price"]
        if any(abs(lp - p) / p < 0.001 and c.ts - ts0 < 86_400_000 for p, ts0 in recent_levels):
            continue  # same level already traded today
        recent_levels.append((lp, c.ts))
        recent_levels[:] = [(p, t0) for p, t0 in recent_levels if c.ts - t0 < 86_400_000]

        slip = slip_bps / 10_000
        beyond = bcfg.get("stop_beyond_wick_pct", 0.1) / 100
        rr = bcfg.get("fixed_rr", 2.5)
        if cand.direction == "short":
            entry = c.close * (1 - slip)
            sl = cand.candle["high"] * (1 + beyond)
            risk = sl - entry
            tp = entry - rr * risk
        else:
            entry = c.close * (1 + slip)
            sl = cand.candle["low"] * (1 - beyond)
            risk = entry - sl
            tp = entry + rr * risk
        if risk <= 0 or tp <= 0:
            continue
        fee_r = (2 * fees_bps / 10_000) * entry / risk
        open_trade = {"ts": c.ts, "dir": cand.direction, "kind": cand.level["kind"],
                      "entry": entry, "sl": sl, "tp": tp, "risk": risk, "rr": rr,
                      "fee_r": fee_r, "bars": 0}
    return trades


def report(trades, label=""):
    if not trades:
        print(f"{label}: no trades.")
        return {}
    rs = [t["r"] for t in trades]
    wins = [r for r in rs if r > 0]
    gross_w = sum(wins)
    gross_l = -sum(r for r in rs if r <= 0)
    # max drawdown on cumulative R curve
    peak = dd = cum = 0.0
    for r in rs:
        cum += r
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    stats = {
        "trades": len(rs),
        "win_rate_pct": round(100 * len(wins) / len(rs), 1),
        "expectancy_R": round(sum(rs) / len(rs), 3),
        "total_R": round(sum(rs), 1),
        "profit_factor": round(gross_w / gross_l, 2) if gross_l else float("inf"),
        "max_drawdown_R": round(dd, 1),
    }
    print(f"\n== {label} ==")
    for k, v in stats.items():
        print(f"  {k:16} {v}")
    by_kind = {}
    for t in trades:
        by_kind.setdefault(t["kind"], []).append(t["r"])
    print("  by level kind:")
    for k, v in sorted(by_kind.items()):
        print(f"    {k:12} n={len(v):4}  exp={sum(v)/len(v):+.3f}R")
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--start"); ap.add_argument("--end")
    ap.add_argument("--min-wick", type=float); ap.add_argument("--swing", type=int)
    ap.add_argument("--rr", type=float)
    ap.add_argument("--fees-bps", type=float, default=5.5)
    ap.add_argument("--slip-bps", type=float, default=2.0)
    ap.add_argument("--out", default=None, help="write trades csv + stats json here")
    args = ap.parse_args()

    cfg = yaml.safe_load((ROOT.parent / "config.yaml").read_text())
    fcfg, bcfg = dict(cfg["filter"]), dict(cfg["bracket"])
    if args.min_wick: fcfg["min_wick_pct"] = args.min_wick
    if args.swing: fcfg["swing_strength"] = args.swing
    if args.rr: bcfg["fixed_rr"] = args.rr

    candles = load_csv(args.csv)
    if args.start: candles = [c for c in candles if c.ts >= ts_of(args.start)]
    if args.end: candles = [c for c in candles if c.ts < ts_of(args.end)]
    span = (f"{datetime.fromtimestamp(candles[0].ts/1000, timezone.utc):%Y-%m-%d} -> "
            f"{datetime.fromtimestamp(candles[-1].ts/1000, timezone.utc):%Y-%m-%d}")
    print(f"{len(candles)} candles | {span} | params: wick>={fcfg['min_wick_pct']} "
          f"swing={fcfg['swing_strength']} rr={bcfg['fixed_rr']} fees={args.fees_bps}bps")

    trades = run(candles, fcfg, bcfg, args.fees_bps, args.slip_bps)
    stats = report(trades, span)

    if args.out:
        out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
        with open(out / "trades.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(trades[0].keys()) if trades else ["ts"])
            w.writeheader(); w.writerows(trades)
        (out / "stats.json").write_text(json.dumps(stats, indent=2))
        print(f"\nwrote {out}/trades.csv and stats.json")


if __name__ == "__main__":
    main()
