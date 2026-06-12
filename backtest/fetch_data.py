"""
fetch_data.py — download historical klines from Bybit into a CSV.

Usage:
    python backtest/fetch_data.py --symbol BTCUSDT --days 365 [--interval 15]
Output:
    backtest/data/<SYMBOL>_<interval>m.csv  (ts,open,high,low,close,volume ascending)
"""

import argparse
import csv
import sys
import time
from pathlib import Path

from pybit.unified_trading import HTTP

sys.path.insert(0, str(Path(__file__).resolve().parent))

ROOT = Path(__file__).resolve().parent


def fetch(symbol: str, days: int, interval: str = "15", testnet: bool = False) -> list[list]:
    http = HTTP(testnet=testnet)
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 86_400_000
    rows: dict[int, list] = {}
    cursor = end_ms
    while cursor > start_ms:
        res = http.get_kline(category="linear", symbol=symbol, interval=interval,
                             end=cursor, limit=1000)
        batch = res["result"]["list"]
        if not batch:
            break
        for r in batch:
            ts = int(r[0])
            if ts >= start_ms:
                rows[ts] = [ts, float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])]
        oldest = min(int(r[0]) for r in batch)
        if oldest >= cursor:
            break
        cursor = oldest - 1
        time.sleep(0.15)  # be polite to the API
        print(f"\rfetched back to {time.strftime('%Y-%m-%d', time.gmtime(oldest/1000))} "
              f"({len(rows)} candles)", end="")
    print()
    return [rows[k] for k in sorted(rows)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--interval", default="15", help="kline interval in minutes (15, 60, 240, ...)")
    args = ap.parse_args()

    data = fetch(args.symbol, args.days, args.interval)
    out = ROOT / "data"
    out.mkdir(exist_ok=True)
    path = out / f"{args.symbol}_{args.interval}m.csv"
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "open", "high", "low", "close", "volume"])
        w.writerows(data)
    print(f"Saved {len(data)} candles -> {path}")
    from data_integrity import check
    check(str(path), interval=int(args.interval))


if __name__ == "__main__":
    main()
