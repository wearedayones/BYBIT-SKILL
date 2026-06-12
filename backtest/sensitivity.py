"""
sensitivity.py — parameter robustness probe.

For each tunable param in the chosen configuration, perturbs it ±1 step
within the known search space and re-runs the backtest. A strategy whose
edge collapses on a small perturbation is sitting on a fragile local maximum
and should not be deployed.

Usage:
    python backtest/sensitivity.py --csv backtest/data/BTCUSDT_15m.csv \\
        --strategy sweep --start 2025-01-01 --end 2025-10-01 \\
        --params min_wick_pct=35,swing_strength=2 --bracket fixed_rr=2.5

Verdict:
    STABLE  — all ±1 perturbations maintain expectancy_R > 0 and PF >= 1.0
    FRAGILE — at least one perturbation collapses the edge
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

# Ordered search spaces (must match BACKTEST.md and strategy-designer)
PARAM_SPACES: dict[str, dict[str, list]] = {
    "sweep": {
        "min_wick_pct":               [20, 25, 30, 35, 40, 45],
        "swing_strength":              [2, 3],
        "equal_level_tolerance_pct":   [0.03, 0.05, 0.08],
    },
    "breakout": {
        "range_lookback":  [24, 36, 48, 72],
        "min_body_atr":    [0.8, 1.0, 1.2, 1.5],
        "atr_period":      [10, 14, 20],
    },
}
BRACKET_SPACES: dict[str, list] = {
    "fixed_rr": [1.5, 2.0, 2.5, 3.0],
}


def _stats(trades: list[dict]) -> dict:
    if not trades:
        return {"trades": 0, "expectancy_R": 0.0, "profit_factor": 0.0,
                "max_drawdown_R": 0.0, "fragile": True}
    rs = [t["r"] for t in trades]
    wins = [r for r in rs if r > 0]
    gross_l = -sum(r for r in rs if r <= 0)
    exp = round(sum(rs) / len(rs), 3)
    pf = round(sum(wins) / gross_l, 2) if gross_l else float("inf")
    peak = dd = cum = 0.0
    for r in rs:
        cum += r; peak = max(peak, cum); dd = max(dd, peak - cum)
    return {
        "trades": len(rs),
        "expectancy_R": exp,
        "profit_factor": min(pf, 99.0),
        "max_drawdown_R": round(dd, 1),
        "fragile": exp <= 0 or pf < 1.0,
    }


def probe(candles, strategy_module, fcfg, bcfg, fees_bps, slip_bps) -> list[dict]:
    name = strategy_module.NAME
    results = []

    def evaluate(label, f, b, is_baseline=False):
        trades = run(candles, strategy_module, f, b, fees_bps, slip_bps)
        s = _stats(trades)
        s["label"] = label
        s["is_baseline"] = is_baseline
        return s

    results.append(evaluate("baseline", fcfg, bcfg, is_baseline=True))

    # Strategy params
    for param, values in PARAM_SPACES.get(name, {}).items():
        if param not in fcfg or fcfg[param] not in values:
            continue
        idx = values.index(fcfg[param])
        for delta, suffix in [(-1, "−1"), (+1, "+1")]:
            ni = idx + delta
            if 0 <= ni < len(values):
                pv = dict(fcfg); pv[param] = values[ni]
                results.append(evaluate(f"{param}={values[ni]} ({suffix})", pv, bcfg))

    # Bracket params
    for param, values in BRACKET_SPACES.items():
        if param not in bcfg or bcfg[param] not in values:
            continue
        idx = values.index(bcfg[param])
        for delta, suffix in [(-1, "−1"), (+1, "+1")]:
            ni = idx + delta
            if 0 <= ni < len(values):
                bv = dict(bcfg); bv[param] = values[ni]
                results.append(evaluate(f"{param}={values[ni]} ({suffix})", fcfg, bv))

    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--strategy", default="sweep", choices=list(REGISTRY))
    ap.add_argument("--start"); ap.add_argument("--end")
    ap.add_argument("--params", default="",
                    help="comma-separated baseline param overrides: k=v,k=v")
    ap.add_argument("--bracket", default="",
                    help="comma-separated baseline bracket overrides: k=v,k=v")
    ap.add_argument("--fees-bps", type=float, default=5.5)
    ap.add_argument("--slip-bps", type=float, default=2.0)
    args = ap.parse_args()

    cfg = yaml.safe_load((ROOT.parent / "config.yaml").read_text())
    scfg = cfg["strategies"][args.strategy]
    fcfg, bcfg = dict(scfg.get("params", {})), dict(scfg.get("bracket", {}))
    strategy = REGISTRY[args.strategy]
    for kv in (args.params or "").split(","):
        if "=" in kv:
            k, v = kv.strip().split("=", 1)
            fcfg[k] = float(v) if "." in v else int(v)
    for kv in (args.bracket or "").split(","):
        if "=" in kv:
            k, v = kv.strip().split("=", 1)
            bcfg[k] = float(v) if "." in v else int(v)

    candles = load_csv(args.csv)
    if args.start: candles = [c for c in candles if c.ts >= ts_of(args.start)]
    if args.end:   candles = [c for c in candles if c.ts < ts_of(args.end)]

    print(f"Loaded {len(candles)} candles | strategy={args.strategy}")
    print(f"Baseline params: {fcfg} | bracket: {bcfg}\n")

    results = probe(candles, strategy, fcfg, bcfg, args.fees_bps, args.slip_bps)

    any_fragile = any(r["fragile"] and not r["is_baseline"] for r in results)
    verdict = "STABLE" if not any_fragile else "FRAGILE"

    print(f"{'Variant':42} {'Trades':>7} {'Exp R':>8} {'PF':>6} {'DD':>7}  Flag")
    print("-" * 82)
    for r in results:
        flag = "*** FRAGILE ***" if r["fragile"] and not r["is_baseline"] else (
               "← baseline" if r["is_baseline"] else "")
        print(f"  {r['label']:40} {r['trades']:>7}  {r['expectancy_R']:>+7.3f}R "
              f"{r['profit_factor']:>5.2f}  {r['max_drawdown_R']:>5.1f}R  {flag}")

    print(f"\nVERDICT: {verdict}")
    if any_fragile:
        print("WARNING: params are on a fragile local maximum.")
        print("         Choose a param combination that remains positive across ±1 perturbations.")

    results_dir = ROOT / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = results_dir / f"sensitivity_{args.strategy}_{stamp}.json"
    out_path.write_text(json.dumps({
        "strategy": args.strategy, "params": fcfg, "bracket": bcfg,
        "window": {"start": args.start, "end": args.end},
        "verdict": verdict, "timestamp": stamp, "perturbations": results,
    }, indent=2))
    print(f"Result saved: {out_path}")
    sys.exit(0 if verdict == "STABLE" else 1)


if __name__ == "__main__":
    main()
