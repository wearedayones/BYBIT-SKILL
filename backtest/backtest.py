"""
backtest.py — replay real candles through the strategy plugin, simulate bracket
trades, report edge metrics.

Usage:
    python backtest/backtest.py --csv backtest/data/BTCUSDT_15m.csv
    python backtest/backtest.py --csv ... --start 2025-06-01 --end 2026-03-01
    python backtest/backtest.py --csv ... --min-wick 30 --swing 3 --rr 2.0
    python backtest/backtest.py --csv ... --monte-carlo 2000

Conservative conventions:
  * Entry = market at sweep-candle close, with slippage applied against you.
  * If SL and TP are both touched within one candle -> counted as a LOSS.
  * Round-trip taker fees converted to R and subtracted from every trade.

Results in R-multiples (1R = initial risk). Expectancy > ~0.05R after fees
across BOTH train and test windows is the minimum bar to take seriously.

Exit codes:
  0 = PASS  (expectancy_R > 0, profit_factor >= 1.05, max_drawdown_R < 15, trades >= 80)
  1 = FAIL
"""

import argparse
import csv
import json
import math
import statistics as _stats
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "daemon"))
from sweep_filter import Candle  # noqa: E402
from strategies import REGISTRY  # noqa: E402

ROOT = Path(__file__).resolve().parent
RESULTS_DIR = ROOT / "results"

PASS_THRESHOLDS = {
    "expectancy_R":    ("gt",  0.0),
    "profit_factor":   ("gte", 1.05),
    "max_drawdown_R":  ("lt",  15.0),
    "trades":          ("gte", 80),
}


def load_csv(path: str) -> list[Candle]:
    out = []
    with open(path) as f:
        for row in csv.DictReader(f):
            out.append(Candle(int(row["ts"]), float(row["open"]), float(row["high"]),
                              float(row["low"]), float(row["close"]), float(row["volume"])))
    return out


def ts_of(date_str: str) -> int:
    return int(datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


def run(candles, strategy, fcfg, bcfg, fees_bps=5.5, slip_bps=2.0, max_hold=96, warmup=200):
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
        cand = strategy.detect(candles[: i + 1], fcfg)
        if not cand:
            continue
        lp = cand.key_price
        if any(abs(lp - p) / p < 0.001 and c.ts - ts0 < 86_400_000 for p, ts0 in recent_levels):
            continue  # same level already traded today
        recent_levels.append((lp, c.ts))
        recent_levels[:] = [(p, t0) for p, t0 in recent_levels if c.ts - t0 < 86_400_000]

        slip = slip_bps / 10_000
        beyond = bcfg.get("stop_beyond_pct", bcfg.get("stop_beyond_wick_pct", 0.1)) / 100
        rr = bcfg.get("fixed_rr", 2.5)
        if cand.direction == "short":
            entry = c.close * (1 - slip)
            sl = cand.stop_hint * (1 + beyond)
            risk = sl - entry
            tp = entry - rr * risk
        else:
            entry = c.close * (1 + slip)
            sl = cand.stop_hint * (1 - beyond)
            risk = entry - sl
            tp = entry + rr * risk
        if risk <= 0 or tp <= 0:
            continue
        fee_r = (2 * fees_bps / 10_000) * entry / risk
        kind = cand.context.get("level", {}).get("kind", cand.strategy)
        open_trade = {"ts": c.ts, "dir": cand.direction, "kind": kind,
                      "entry": entry, "sl": sl, "tp": tp, "risk": risk, "rr": rr,
                      "fee_r": fee_r, "bars": 0}
    return trades


def report(trades, label="", days_covered: float = None):
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
    # Sharpe: trade-level, annualised. rf = 0 (crypto).
    std_R = _stats.stdev(rs) if len(rs) > 1 else 0.0
    if std_R > 0 and days_covered and days_covered > 0:
        annual_trades = len(rs) / (days_covered / 365)
        sharpe = round(sum(rs) / len(rs) / std_R * math.sqrt(annual_trades), 2)
    else:
        sharpe = 0.0
    tpm = round(len(rs) / max((days_covered or 0) / 30.44, 1), 1)
    stats = {
        "trades": len(rs),
        "win_rate_pct": round(100 * len(wins) / len(rs), 1),
        "expectancy_R": round(sum(rs) / len(rs), 3),
        "total_R": round(sum(rs), 1),
        "profit_factor": round(gross_w / gross_l, 2) if gross_l else float("inf"),
        "max_drawdown_R": round(dd, 1),
        "sharpe_ratio": sharpe,
        "trades_per_month": tpm,
    }
    print(f"\n== {label} ==")
    for k, v in stats.items():
        print(f"  {k:20} {v}")
    by_kind = {}
    for t in trades:
        by_kind.setdefault(t["kind"], []).append(t["r"])
    print("  by level kind:")
    for k, v in sorted(by_kind.items()):
        print(f"    {k:12} n={len(v):4}  exp={sum(v)/len(v):+.3f}R")
    return stats


def monte_carlo(trades: list[dict], n: int) -> dict:
    """Shuffle trade order N times and compute drawdown distribution."""
    import random
    rs = [t["r"] for t in trades]
    exp_samples, dd_samples, ruin_count = [], [], 0
    for _ in range(n):
        shuffled = rs[:]
        random.shuffle(shuffled)
        exp_samples.append(sum(shuffled) / len(shuffled))
        peak = dd = cum = 0.0
        ruined = False
        for r in shuffled:
            cum += r
            peak = max(peak, cum)
            dd = max(dd, peak - cum)
            if cum < -20:
                ruined = True
        dd_samples.append(dd)
        if ruined:
            ruin_count += 1
    exp_samples.sort()
    dd_samples.sort()

    def pct(lst, p):
        return lst[max(0, min(int(len(lst) * p / 100), len(lst) - 1))]

    return {
        "n": n,
        "expectancy_median": round(pct(exp_samples, 50), 3),
        "expectancy_p5":     round(pct(exp_samples,  5), 3),
        "expectancy_p95":    round(pct(exp_samples, 95), 3),
        "drawdown_p50":      round(pct(dd_samples, 50), 1),
        "drawdown_p95":      round(pct(dd_samples, 95), 1),
        "ruin_pct":          round(100 * ruin_count / n, 1),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--strategy", default="sweep", choices=list(REGISTRY))
    ap.add_argument("--start"); ap.add_argument("--end")
    ap.add_argument("--min-wick", type=float); ap.add_argument("--swing", type=int)
    ap.add_argument("--rr", type=float)
    ap.add_argument("--param", action="append",
                    help="generic strategy param override, e.g. --param range_lookback=64")
    ap.add_argument("--fees-bps", type=float, default=5.5)
    ap.add_argument("--slip-bps", default="2.0",
                    help="slippage in bps, or 'live' to use the TCA-calibrated "
                         "value from state/tca.json")
    ap.add_argument("--out", default=None, help="write trades csv + stats json here")
    ap.add_argument("--monte-carlo", type=int, default=0,
                    help="shuffle trade order N times and report drawdown distribution")
    args = ap.parse_args()

    # Resolve slippage: 'live' reads the TCA-calibrated value (never below
    # the default — see daemon/tca.py).
    if str(args.slip_bps).lower() == "live":
        tca_path = ROOT.parent / "state" / "tca.json"
        if tca_path.exists():
            args.slip_bps = float(json.loads(tca_path.read_text())["calibrated_slip_bps"])
            print(f"Using TCA-calibrated live slippage: {args.slip_bps} bps")
        else:
            args.slip_bps = 2.0
            print("No state/tca.json yet — falling back to default 2.0 bps")
    else:
        args.slip_bps = float(args.slip_bps)

    cfg = yaml.safe_load((ROOT.parent / "config.yaml").read_text())
    scfg = cfg["strategies"][args.strategy]
    fcfg, bcfg = dict(scfg.get("params", {})), dict(scfg.get("bracket", {}))
    strategy = REGISTRY[args.strategy]
    if args.min_wick: fcfg["min_wick_pct"] = args.min_wick
    if args.swing: fcfg["swing_strength"] = args.swing
    if args.rr: bcfg["fixed_rr"] = args.rr
    if args.param:
        for kv in args.param:
            k, v = kv.split("=", 1)
            fcfg[k] = float(v) if "." in v else int(v)

    candles = load_csv(args.csv)
    if args.start: candles = [c for c in candles if c.ts >= ts_of(args.start)]
    if args.end: candles = [c for c in candles if c.ts < ts_of(args.end)]
    span = (f"{datetime.fromtimestamp(candles[0].ts/1000, timezone.utc):%Y-%m-%d} -> "
            f"{datetime.fromtimestamp(candles[-1].ts/1000, timezone.utc):%Y-%m-%d}")
    days_covered = (candles[-1].ts - candles[0].ts) / 86_400_000 if len(candles) > 1 else 0
    print(f"{len(candles)} candles | {span} | strategy={args.strategy} | params={fcfg} "
          f"| rr={bcfg.get('fixed_rr')} fees={args.fees_bps}bps")

    trades = run(candles, strategy, fcfg, bcfg, args.fees_bps, args.slip_bps)
    stats = report(trades, span, days_covered=days_covered)

    # Monte Carlo drawdown analysis
    mc_stats = {}
    if args.monte_carlo > 0 and trades:
        mc_stats = monte_carlo(trades, args.monte_carlo)
        print(f"\n== Monte Carlo ({args.monte_carlo} shuffles) ==")
        print(f"  exp median/p5/p95     {mc_stats['expectancy_median']:+.3f} / "
              f"{mc_stats['expectancy_p5']:+.3f} / {mc_stats['expectancy_p95']:+.3f}R")
        print(f"  drawdown p50/p95      {mc_stats['drawdown_p50']:.1f} / "
              f"{mc_stats['drawdown_p95']:.1f}R")
        print(f"  ruin probability      {mc_stats['ruin_pct']:.1f}%  (pass < 5%)")

    # Pass/fail verdict
    ops = {"gt": lambda a, b: a > b, "gte": lambda a, b: a >= b, "lt": lambda a, b: a < b}
    passed = all(ops[op](stats.get(metric, -999 if op == "gt" else 9999), threshold)
                 for metric, (op, threshold) in PASS_THRESHOLDS.items())
    if mc_stats and mc_stats["ruin_pct"] >= 5.0:
        passed = False
    verdict = "PASS" if passed else "FAIL"
    print(f"\nVERDICT: {verdict}")

    # Always auto-save structured result
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    record = {
        "strategy": args.strategy, "params": fcfg, "bracket": bcfg,
        "window": {"start": args.start, "end": args.end},
        "metrics": stats, "verdict": verdict, "timestamp": stamp,
        "fees_bps": args.fees_bps, "slip_bps": args.slip_bps,
    }
    if mc_stats:
        record["monte_carlo"] = mc_stats
    out_path = RESULTS_DIR / f"{args.strategy}_{stamp}.json"
    out_path.write_text(json.dumps(record, indent=2))
    print(f"Result saved: {out_path}")

    if args.out:
        out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
        with open(out / "trades.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(trades[0].keys()) if trades else ["ts"])
            w.writeheader(); w.writerows(trades)
        (out / "stats.json").write_text(json.dumps(stats, indent=2))
        print(f"\nwrote {out}/trades.csv and stats.json")

    sys.exit(0 if verdict == "PASS" else 1)


if __name__ == "__main__":
    main()
