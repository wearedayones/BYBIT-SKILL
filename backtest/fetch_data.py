"""
fetch_data.py — download historical 15m klines from Bybit into a CSV.

Usage:
    python backtest/fetch_data.py --symbol BTCUSDT --days 365
Output:
    backtest/data/<SYMBOL>_15m.csv   (ts,open,high,low,close,volume ascending)
"""

import argparse
import csv
import time
from pathlib import Path

from pybit.unified_trading import HTTP

ROOT = Path(__file__).resolve().parent


def fetch(symbol: str, days: int, testnet: bool = False) -> list[list]:
    http = HTTP(testnet=testnet)
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 86_400_000
    rows: dict[int, list] = {}
    cursor = end_ms
    while cursor > start_ms:
        res = http.get_kline(category="linear", symbol=symbol, interval="15",
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
    args = ap.parse_args()

    data = fetch(args.symbol, args.days)
    out = ROOT / "data"
    out.mkdir(exist_ok=True)
    path = out / f"{args.symbol}_15m.csv"
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "open", "high", "low", "close", "volume"])
        w.writerows(data)
    print(f"Saved {len(data)} candles -> {path}")


if __name__ == "__main__":
    main()
