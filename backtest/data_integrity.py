"""
data_integrity.py — validate candle CSV quality before backtesting.

Checks for missing gaps, duplicate timestamps, bad OHLC values, and extreme
single-candle price jumps. Run before any backtest to catch silent data
corruption that would silently distort results.

Usage:
    python backtest/data_integrity.py --csv backtest/data/BTCUSDT_15m.csv
    python backtest/data_integrity.py --csv ... --interval 60 --max-gap-candles 5

Exit codes:
    0 = CLEAN or WARN (data is usable; warnings noted but not fatal)
    1 = FAIL (duplicate timestamps or bad OHLC — do not backtest this file)
"""

import argparse
import csv
import sys
from datetime import datetime, timezone
from pathlib import Path


def check(csv_path: str, interval: int = 15, max_gap_candles: int = 3,
          extreme_jump_pct: float = 15.0, min_candles: int = 5000) -> dict:
    """Run all integrity checks and print a report. Returns result dict."""
    path = Path(csv_path)
    if not path.exists():
        print(f"ERROR: File not found: {csv_path}")
        return {"verdict": "FAIL", "error": "file not found"}

    rows = []
    with open(path) as f:
        for row in csv.DictReader(f):
            rows.append((
                int(row["ts"]),
                float(row["open"]),
                float(row["high"]),
                float(row["low"]),
                float(row["close"]),
                float(row["volume"]),
            ))

    if not rows:
        return {"verdict": "FAIL", "error": "empty file"}

    interval_ms = interval * 60 * 1000
    gap_threshold_ms = interval_ms * (max_gap_candles + 0.5)

    # 1. Duplicate timestamps
    ts_seen: set = set()
    dupes = 0
    for r in rows:
        if r[0] in ts_seen:
            dupes += 1
        ts_seen.add(r[0])

    # 2. Bad OHLC (zero or negative close/open)
    bad_ohlc = sum(1 for r in rows if r[4] <= 0 or r[1] <= 0)

    # 3. Gaps (consecutive timestamps too far apart)
    gaps = []
    for i in range(1, len(rows)):
        delta = rows[i][0] - rows[i - 1][0]
        if delta > gap_threshold_ms:
            missing = round(delta / interval_ms) - 1
            gap_dt = datetime.fromtimestamp(
                rows[i - 1][0] / 1000, timezone.utc
            ).strftime("%Y-%m-%d %H:%M")
            gaps.append({"at": gap_dt, "missing_candles": missing})

    # 4. Extreme single-candle price jumps
    jumps = []
    for i in range(1, len(rows)):
        prior = rows[i - 1][4]
        cur = rows[i][4]
        if prior > 0:
            pct = abs(cur - prior) / prior * 100
            if pct > extreme_jump_pct:
                jump_dt = datetime.fromtimestamp(
                    rows[i][0] / 1000, timezone.utc
                ).strftime("%Y-%m-%d")
                jumps.append({"at": jump_dt, "pct": round(pct, 1)})

    t0 = datetime.fromtimestamp(rows[0][0] / 1000, timezone.utc).strftime("%Y-%m-%d")
    t1 = datetime.fromtimestamp(rows[-1][0] / 1000, timezone.utc).strftime("%Y-%m-%d")
    worst_gap = max((g["missing_candles"] for g in gaps), default=0)

    if dupes > 0 or bad_ohlc > 0:
        verdict = "FAIL"
    elif gaps or jumps or len(rows) < min_candles:
        verdict = "WARN"
    else:
        verdict = "CLEAN"

    result = {
        "verdict": verdict,
        "candles": len(rows),
        "date_range": f"{t0} → {t1}",
        "interval_m": interval,
        "gaps_count": len(gaps),
        "worst_gap_candles": worst_gap,
        "duplicates": dupes,
        "bad_ohlc": bad_ohlc,
        "extreme_jumps": len(jumps),
        "gaps": gaps[:5],
        "jump_examples": jumps[:3],
    }

    print(f"\nData integrity: {path.name}")
    print(f"  Candles            : {len(rows):,}")
    print(f"  Date range         : {t0} → {t1}")
    print(f"  Interval           : {interval}m")
    print(f"  Gaps (>{max_gap_candles} missing)   : {len(gaps)}"
          + (f"  (worst: {worst_gap} candles at {gaps[0]['at']})" if gaps else ""))
    print(f"  Duplicate ts       : {dupes}")
    print(f"  Bad OHLC           : {bad_ohlc}")
    print(f"  Extreme jumps >15% : {len(jumps)}"
          + (f"  (e.g. {jumps[0]['at']} {jumps[0]['pct']:+.1f}%)" if jumps else ""))
    if len(rows) < min_candles:
        print(f"  WARNING: only {len(rows):,} candles — {min_candles:,} recommended for 9mo training")
    print(f"VERDICT: {verdict}\n")

    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--interval", type=int, default=15,
                    help="expected candle interval in minutes")
    ap.add_argument("--max-gap-candles", type=int, default=3,
                    help="gaps larger than N missing candles are flagged")
    args = ap.parse_args()

    result = check(args.csv, interval=args.interval,
                   max_gap_candles=args.max_gap_candles)
    sys.exit(0 if result["verdict"] in ("CLEAN", "WARN") else 1)


if __name__ == "__main__":
    main()
