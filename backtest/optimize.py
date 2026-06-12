"""
optimize.py — automated parameter grid search with parallel execution and
leaderboard. The institutional equivalent of "try everything, rank by quality."

Sweeps ALL combinations from sensitivity.PARAM_SPACES × BRACKET_SPACES,
runs them in parallel, ranks by Sharpe ratio (primary) then expectancy_R,
and applies Monte Carlo ruin filtering on the top-N to find the best STABLE
configuration.

Usage:
    python backtest/optimize.py --csv backtest/data/BTCUSDT_15m.csv \\
        --strategy sweep --start 2025-01-01 --end 2025-10-01 \\
        [--top-n 10] [--monte-carlo 500] [--workers 4]

Output:
    backtest/results/optimize_<strategy>_<stamp>.json — full ranked list
    Console leaderboard sorted by Sharpe

Exit codes:
    0 = rank-1 candidate passes all train thresholds + ruin < 5%
    1 = no candidate meets all thresholds
"""

import argparse
import itertools
import json
import math
import statistics as _stats
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
DAEMON_DIR = ROOT.parent / "daemon"

# Inject daemon path for the main process (forked workers inherit this on Linux)
sys.path.insert(0, str(DAEMON_DIR))
sys.path.insert(0, str(ROOT))

PASS_THRESHOLDS = {
    "expectancy_R":   ("gt",  0.0),
    "profit_factor":  ("gte", 1.05),
    "max_drawdown_R": ("lt",  15.0),
    "trades":         ("gte", 80),
}


def _passes(metrics: dict) -> bool:
    ops = {"gt": lambda a, b: a > b, "gte": lambda a, b: a >= b,
           "lt": lambda a, b: a < b}
    return all(ops[op](metrics.get(m, -999 if op in ("gt", "gte") else 9999), thr)
               for m, (op, thr) in PASS_THRESHOLDS.items())


def build_grid(strategy_name: str) -> list[tuple[dict, dict]]:
    from sensitivity import PARAM_SPACES, BRACKET_SPACES
    param_keys = list(PARAM_SPACES.get(strategy_name, {}).keys())
    param_vals = [PARAM_SPACES[strategy_name][k] for k in param_keys]
    bk_keys = list(BRACKET_SPACES.keys())
    bk_vals = [BRACKET_SPACES[k] for k in bk_keys]
    combos = []
    for pv in itertools.product(*param_vals):
        for bv in itertools.product(*bk_vals):
            combos.append((dict(zip(param_keys, pv)), dict(zip(bk_keys, bv))))
    return combos


def _eval_combo(args_tuple):
    """Worker: evaluate one (strategy, params, bracket) combination.
    Must be top-level for multiprocessing pickling.
    Returns metrics + R values for later MC filtering.
    """
    import sys
    from pathlib import Path as _Path
    _root = _Path(__file__).resolve().parent
    sys.path.insert(0, str(_root.parent / "daemon"))
    sys.path.insert(0, str(_root))

    candles, strategy_name, fcfg, bcfg, fees_bps, slip_bps, days_covered = args_tuple

    from strategies import REGISTRY
    from backtest import run

    module = REGISTRY[strategy_name]
    trades = run(candles, module, fcfg, bcfg, fees_bps, slip_bps)

    if not trades:
        return {
            "params": fcfg, "bracket": bcfg,
            "metrics": {
                "trades": 0, "expectancy_R": -999.0, "profit_factor": 0.0,
                "max_drawdown_R": 0.0, "sharpe_ratio": 0.0,
                "trades_per_month": 0.0, "win_rate_pct": 0.0,
            },
            "rs": [],
        }

    rs = [t["r"] for t in trades]
    wins = [r for r in rs if r > 0]
    gross_l = -sum(r for r in rs if r <= 0)
    exp = round(sum(rs) / len(rs), 3)
    pf = round(sum(wins) / gross_l, 2) if gross_l else float("inf")
    peak = dd = cum = 0.0
    for r in rs:
        cum += r
        peak = max(peak, cum)
        dd = max(dd, peak - cum)

    std_R = _stats.stdev(rs) if len(rs) > 1 else 0.0
    if std_R > 0 and days_covered > 0:
        annual_trades = len(rs) / (days_covered / 365)
        sharpe = round(exp / std_R * math.sqrt(annual_trades), 2)
    else:
        sharpe = 0.0
    tpm = round(len(rs) / max(days_covered / 30.44, 1), 1)

    return {
        "params": fcfg,
        "bracket": bcfg,
        "metrics": {
            "trades": len(rs),
            "win_rate_pct": round(100 * len(wins) / len(rs), 1),
            "expectancy_R": exp,
            "profit_factor": min(pf, 99.0),
            "max_drawdown_R": round(dd, 1),
            "sharpe_ratio": sharpe,
            "trades_per_month": tpm,
        },
        "rs": rs,
    }


def _monte_carlo(rs: list[float], n: int) -> float:
    """Return ruin_pct (ruin = cumulative R ever drops below -20)."""
    import random
    ruin = 0
    for _ in range(n):
        s = rs[:]
        random.shuffle(s)
        cum = 0.0
        for r in s:
            cum += r
            if cum < -20:
                ruin += 1
                break
    return round(100 * ruin / n, 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--strategy", required=True)
    ap.add_argument("--start"); ap.add_argument("--end")
    ap.add_argument("--top-n", type=int, default=10,
                    help="number of top candidates to run Monte Carlo on")
    ap.add_argument("--monte-carlo", type=int, default=500,
                    help="MC shuffles per top-N candidate (0 = skip)")
    ap.add_argument("--workers", type=int, default=4,
                    help="parallel worker processes")
    ap.add_argument("--fees-bps", type=float, default=5.5)
    ap.add_argument("--slip-bps", type=float, default=2.0)
    args = ap.parse_args()

    from backtest import load_csv, ts_of
    from strategies import REGISTRY

    if args.strategy not in REGISTRY:
        print(f"ERROR: Unknown strategy '{args.strategy}'. "
              f"Available: {list(REGISTRY)}")
        sys.exit(1)

    candles = load_csv(args.csv)
    if args.start:
        candles = [c for c in candles if c.ts >= ts_of(args.start)]
    if args.end:
        candles = [c for c in candles if c.ts < ts_of(args.end)]

    if not candles:
        print("ERROR: No candles after date filtering.")
        sys.exit(1)

    days_covered = (candles[-1].ts - candles[0].ts) / 86_400_000
    span = (f"{datetime.fromtimestamp(candles[0].ts/1000, timezone.utc):%Y-%m-%d} → "
            f"{datetime.fromtimestamp(candles[-1].ts/1000, timezone.utc):%Y-%m-%d}")

    grid = build_grid(args.strategy)
    if not grid:
        print(f"ERROR: No PARAM_SPACES defined for '{args.strategy}'. "
              f"Add it to backtest/sensitivity.py.")
        sys.exit(1)

    print(f"\nOptimizer | strategy={args.strategy} | combos={len(grid)} | "
          f"workers={args.workers} | {span}")
    print("Running grid search...", end="", flush=True)

    tasks = [
        (candles, args.strategy, fcfg, bcfg, args.fees_bps, args.slip_bps, days_covered)
        for fcfg, bcfg in grid
    ]

    results = []
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(_eval_combo, t): i for i, t in enumerate(tasks)}
        done = 0
        milestone = max(len(tasks) // 10, 1)
        for fut in as_completed(futures):
            done += 1
            if done % milestone == 0:
                print(f"\r  {done}/{len(tasks)} combos...", end="", flush=True)
            results.append(fut.result())

    print(f"\r  {len(tasks)}/{len(tasks)} combos done.        ")

    # Rank: Sharpe DESC, then expectancy_R DESC
    results.sort(key=lambda r: (
        -r["metrics"]["sharpe_ratio"],
        -r["metrics"]["expectancy_R"],
    ))

    # Monte Carlo on top-N
    top = results[:args.top_n]
    if args.monte_carlo > 0 and top:
        print(f"Monte Carlo ({args.monte_carlo} shuffles) on top {len(top)} candidates...")
        for r in top:
            if r["rs"]:
                r["ruin_pct"] = _monte_carlo(r["rs"], args.monte_carlo)
            else:
                r["ruin_pct"] = 100.0

    # Strip rs from output before printing / saving (large, not useful in JSON)
    for r in results:
        r.pop("rs", None)

    # Print leaderboard
    mc_col = args.monte_carlo > 0
    hdr = (f"{'Rk':>3}  {'Params':<38} {'Tr':>5} {'ExpR':>8} {'Sharpe':>7} "
           f"{'PF':>5} {'DD':>5} {'Tr/mo':>5}")
    if mc_col:
        hdr += f"  {'Ruin':>5}  Status"
    else:
        hdr += "  Status"
    print(f"\n{hdr}")
    print("-" * len(hdr))

    for i, r in enumerate(top, 1):
        m = r["metrics"]
        p = r["params"]
        b = r["bracket"]
        param_str = " ".join(f"{k}={v}" for k, v in list(p.items())[:2])
        param_str += f" rr={b.get('fixed_rr', '?')}"

        ruin = r.get("ruin_pct")
        thin = m["trades"] < 80
        if thin:
            status = "THIN"
        elif not _passes(m):
            status = "FAIL"
        elif ruin is not None and ruin >= 5.0:
            status = f"RUIN({ruin:.0f}%)"
        elif i == 1:
            status = "BEST ★"
        else:
            status = "STABLE"

        line = (f"{i:>3}  {param_str:<38} {m['trades']:>5} {m['expectancy_R']:>+8.3f}R "
                f"{m['sharpe_ratio']:>7.2f} {m['profit_factor']:>5.2f} "
                f"{m['max_drawdown_R']:>5.1f} {m['trades_per_month']:>5.1f}")
        if mc_col:
            ruin_str = f"{ruin:.1f}%" if ruin is not None else "  n/a"
            line += f"  {ruin_str:>5}  {status}"
        else:
            line += f"  {status}"
        print(line)

    # Overall verdict
    best = top[0] if top else None
    passed = False
    best_ruin = None
    if best:
        best_ruin = best.get("ruin_pct", 0.0)
        passed = (_passes(best["metrics"])
                  and (args.monte_carlo == 0 or (best_ruin is not None and best_ruin < 5.0)))

    verdict = "PASS" if passed else "FAIL"
    print(f"\nVERDICT: {verdict}")
    if passed and best:
        print(f"  Best params  : {best['params']}")
        print(f"  Best bracket : {best['bracket']}")
        print(f"  Sharpe={best['metrics']['sharpe_ratio']:.2f}  "
              f"ExpR={best['metrics']['expectancy_R']:+.3f}R  "
              f"Ruin={best_ruin:.1f}%")
        print("  → Ready for walk-forward + test-window validation.")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = ROOT / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"optimize_{args.strategy}_{stamp}.json"
    out_path.write_text(json.dumps({
        "strategy": args.strategy,
        "window": {"start": args.start, "end": args.end},
        "combos_tested": len(results),
        "verdict": verdict,
        "best": best,
        "top_n": top,
        "timestamp": stamp,
        "fees_bps": args.fees_bps,
        "slip_bps": args.slip_bps,
    }, indent=2))
    print(f"Result saved: {out_path}")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
