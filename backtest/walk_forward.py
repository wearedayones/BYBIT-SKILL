"""
walk_forward.py — rolling walk-forward validation.

Slices the CSV into overlapping (train, test) windows, runs each test window
out-of-sample with fixed params (no re-tuning per fold), and aggregates
statistics across folds. This is the institutional standard for detecting
overfitting that a single train/test split cannot catch.

Usage:
    python backtest/walk_forward.py --csv backtest/data/BTCUSDT_15m.csv \\
        --strategy sweep --train-months 9 --test-months 3 --step-months 1 \\
        [--min-wick 35] [--swing 2] [--rr 2.5] [--param k=v] \\
        [--fees-bps 5.5] [--slip-bps 2.0]

Institutional pass bar:
    pct_folds_positive >= 0.60   (edge present in >= 60% of OOS periods)
    combined_expectancy_R > 0    (positive in aggregate)
    combined_max_drawdown_R < 20 (survives all regimes combined)
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "daemon"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backtest import load_csv, run, ts_of  # noqa: E402
from strategies import REGISTRY  # noqa: E402

ROOT = Path(__file__).resolve().parent

DAYS_PER_MONTH = 30.44
MS_PER_DAY = 86_400_000


def months_to_ms(n: int) -> int:
    return int(n * DAYS_PER_MONTH * MS_PER_DAY)


def walk_forward(candles, strategy_module, fcfg, bcfg,
                 train_months, test_months, step_months,
                 fees_bps, slip_bps):
    if not candles:
        return {"error": "No candles loaded"}

    train_ms = months_to_ms(train_months)
    test_ms = months_to_ms(test_months)
    step_ms = months_to_ms(step_months)
    t_end = candles[-1].ts

    folds = []
    train_start = candles[0].ts

    while True:
        train_end = train_start + train_ms
        test_end = train_end + test_ms
        if test_end > t_end:
            break

        # Use all candles up to test_end for warmup context;
        # only attribute trades that opened in the test window.
        context = [c for c in candles if c.ts < test_end]
        trades = run(context, strategy_module, fcfg, bcfg, fees_bps, slip_bps)
        test_trades = [t for t in trades if train_end <= t["ts"] < test_end]

        if test_trades:
            rs = [t["r"] for t in test_trades]
            wins = [r for r in rs if r > 0]
            gross_l = -sum(r for r in rs if r <= 0)
            exp = round(sum(rs) / len(rs), 3)
            pf = round(sum(wins) / gross_l, 2) if gross_l else float("inf")
        else:
            exp, pf = 0.0, 0.0

        folds.append({
            "train_start": datetime.fromtimestamp(train_start / 1000, timezone.utc).strftime("%Y-%m-%d"),
            "train_end": datetime.fromtimestamp(train_end / 1000, timezone.utc).strftime("%Y-%m-%d"),
            "test_end": datetime.fromtimestamp(test_end / 1000, timezone.utc).strftime("%Y-%m-%d"),
            "oos_trades": len(test_trades),
            "expectancy_R": exp,
            "profit_factor": min(pf, 99.0),
            "total_R": round(sum(t["r"] for t in test_trades), 2) if test_trades else 0.0,
        })

        train_start += step_ms
        if train_start + train_ms + test_ms > t_end:
            break

    if not folds:
        return {"error": "Not enough data for any walk-forward fold"}

    # Aggregate: combined equity curve over folds (fold totals as proxy)
    total_trades = sum(f["oos_trades"] for f in folds)
    total_R = sum(f["total_R"] for f in folds)
    positive_folds = sum(1 for f in folds if f["expectancy_R"] > 0)
    pct_positive = positive_folds / len(folds)
    combined_exp = round(total_R / max(total_trades, 1), 3)

    # Max drawdown on fold-level equity curve
    peak = dd = cum = 0.0
    for f in folds:
        cum += f["total_R"]
        peak = max(peak, cum)
        dd = max(dd, peak - cum)

    PASS = (pct_positive >= 0.60 and combined_exp > 0 and dd < 20)

    return {
        "folds": folds,
        "n_folds": len(folds),
        "positive_folds": positive_folds,
        "pct_folds_positive": round(pct_positive, 2),
        "combined_expectancy_R": combined_exp,
        "combined_total_R": round(total_R, 1),
        "combined_max_drawdown_R": round(dd, 1),
        "total_trades_oos": total_trades,
        "verdict": "PASS" if PASS else "FAIL",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--strategy", default="sweep", choices=list(REGISTRY))
    ap.add_argument("--train-months", type=int, default=9)
    ap.add_argument("--test-months", type=int, default=3)
    ap.add_argument("--step-months", type=int, default=1)
    ap.add_argument("--min-wick", type=float)
    ap.add_argument("--swing", type=int)
    ap.add_argument("--rr", type=float)
    ap.add_argument("--param", action="append",
                    help="generic strategy param override e.g. --param range_lookback=48")
    ap.add_argument("--fees-bps", type=float, default=5.5)
    ap.add_argument("--slip-bps", type=float, default=2.0)
    args = ap.parse_args()

    cfg = yaml.safe_load((ROOT.parent / "config.yaml").read_text())
    scfg = cfg["strategies"][args.strategy]
    fcfg, bcfg = dict(scfg.get("params", {})), dict(scfg.get("bracket", {}))
    strategy = REGISTRY[args.strategy]
    if args.min_wick:  fcfg["min_wick_pct"] = args.min_wick
    if args.swing:     fcfg["swing_strength"] = args.swing
    if args.rr:        bcfg["fixed_rr"] = args.rr
    if args.param:
        for kv in args.param:
            k, v = kv.split("=", 1)
            fcfg[k] = float(v) if "." in v else int(v)

    candles = load_csv(args.csv)
    span = (f"{datetime.fromtimestamp(candles[0].ts/1000, timezone.utc):%Y-%m-%d} → "
            f"{datetime.fromtimestamp(candles[-1].ts/1000, timezone.utc):%Y-%m-%d}")
    print(f"{len(candles)} candles  |  {span}")
    print(f"strategy={args.strategy}  train={args.train_months}mo  "
          f"test={args.test_months}mo  step={args.step_months}mo")
    print(f"params={fcfg}  bracket={bcfg}  fees={args.fees_bps}bps\n")

    result = walk_forward(candles, strategy, fcfg, bcfg,
                          args.train_months, args.test_months, args.step_months,
                          args.fees_bps, args.slip_bps)

    if "error" in result:
        print(f"ERROR: {result['error']}")
        sys.exit(1)

    # Print fold table
    print(f"{'Test window end':16} {'OOS trades':>11} {'Exp R':>8} {'PF':>6} {'Total R':>9}  Pass?")
    print("-" * 62)
    for f in result["folds"]:
        mark = "✓" if f["expectancy_R"] > 0 else "✗"
        print(f"  {f['test_end']:16} {f['oos_trades']:>11}  {f['expectancy_R']:>+7.3f}R "
              f"{f['profit_factor']:>5.2f}  {f['total_R']:>+8.1f}R  {mark}")

    # Summary
    print(f"\n== Walk-Forward Summary ==")
    print(f"  Folds evaluated      {result['n_folds']}")
    print(f"  Folds positive       {result['positive_folds']}/{result['n_folds']}  "
          f"({result['pct_folds_positive']*100:.0f}%)   pass ≥ 60%")
    print(f"  Combined exp_R       {result['combined_expectancy_R']:+.3f}R          pass > 0")
    print(f"  Combined max DD      {result['combined_max_drawdown_R']:.1f}R           pass < 20")
    print(f"  OOS trades (total)   {result['total_trades_oos']}")
    print(f"\nVERDICT: {result['verdict']}")

    results_dir = ROOT / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = results_dir / f"wf_{args.strategy}_{stamp}.json"
    out_path.write_text(json.dumps({
        "strategy": args.strategy, "params": fcfg, "bracket": bcfg,
        "train_months": args.train_months, "test_months": args.test_months,
        "step_months": args.step_months, "timestamp": stamp,
        **result,
    }, indent=2))
    print(f"Result saved: {out_path}")
    sys.exit(0 if result["verdict"] == "PASS" else 1)


if __name__ == "__main__":
    main()
