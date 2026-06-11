"""
candle_watcher.py — the trigger layer.

Subscribes to Bybit's public kline.15 WebSocket. When a candle arrives with
confirm=true (candle officially closed), runs the deterministic sweep
pre-filter. Only if a candidate exists does it invoke Claude Code headless
(`claude -p`) to run the multi-agent pipeline.

Run:  python daemon/candle_watcher.py
Stop trading instantly:  touch KILL_SWITCH   (in project root)
"""

import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml
from pybit.unified_trading import HTTP, WebSocket

from sweep_filter import Candle, detect_sweep, asdict

ROOT = Path(__file__).resolve().parent.parent
CFG = yaml.safe_load((ROOT / "config.yaml").read_text())
LOGS = ROOT / "logs"
KILL = ROOT / "KILL_SWITCH"

SYMBOL = CFG["symbol"]
TESTNET = CFG["testnet"]
INTERVAL = "15"

candles: list[Candle] = []
last_invoke_ts = 0


def log(msg: str):
    line = f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    with open(LOGS / "watcher.log", "a") as f:
        f.write(line + "\n")


def bootstrap_history():
    """Pull last 200 closed candles via REST so levels exist immediately."""
    http = HTTP(testnet=TESTNET)
    res = http.get_kline(category="linear", symbol=SYMBOL, interval=INTERVAL, limit=200)
    rows = sorted(res["result"]["list"], key=lambda r: int(r[0]))
    for r in rows[:-1]:  # drop the still-open candle
        candles.append(Candle(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])))
    log(f"Bootstrapped {len(candles)} candles for {SYMBOL}")


def invoke_claude(candidate):
    global last_invoke_ts
    cooldown = CFG.get("invoke_cooldown_sec", 900)
    if time.time() - last_invoke_ts < cooldown:
        log("Candidate found but inside cooldown — skipped.")
        return
    last_invoke_ts = time.time()

    payload = {
        "event": "sweep_candidate",
        "symbol": SYMBOL,
        "timeframe": "15m",
        "utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "candidate": {
            "direction": candidate.direction,
            "level": candidate.level,
            "candle": candidate.candle,
            "wick_pct": candidate.wick_pct,
            "extras": candidate.extras,
        },
        "recent_candles": [asdict(c) for c in candles[-40:]],
    }

    prompt = (
        "A 15m candle just closed and the pre-filter flagged a liquidity-sweep candidate. "
        "Follow the pipeline in CLAUDE.md exactly: dispatch sweep-analyst, then event-guard, "
        "then risk-manager, then (only if all approve) executor — SERIALLY, one at a time. "
        "Update state/journal.json before exiting, whatever the outcome.\n\n"
        f"EVENT DATA:\n{json.dumps(payload, indent=2)}"
    )

    cmd = [
        "claude", "-p", prompt,
        "--allowedTools", "mcp__bybit__*", "Read", "Write", "Edit", "WebFetch", "WebSearch", "Task",
        "--max-turns", str(CFG.get("max_turns", 30)),
        "--output-format", "json",
    ]
    log(f"Invoking Claude Code: {candidate.direction} candidate at {candidate.level['kind']} "
        f"{candidate.level['price']}")
    try:
        result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                                timeout=CFG.get("invoke_timeout_sec", 600))
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        (LOGS / f"run_{stamp}.json").write_text(result.stdout or result.stderr)
        log(f"Claude run finished (exit {result.returncode}); log: logs/run_{stamp}.json")
    except subprocess.TimeoutExpired:
        log("ERROR: Claude run timed out — check for stuck orders manually!")


def on_kline(msg):
    try:
        data = msg["data"][0]
        if not data.get("confirm"):
            return  # candle still forming
        c = Candle(int(data["start"]), float(data["open"]), float(data["high"]),
                   float(data["low"]), float(data["close"]), float(data["volume"]))
        if candles and c.ts <= candles[-1].ts:
            return  # duplicate
        candles.append(c)
        del candles[:-400]
        log(f"Candle closed: O{c.open} H{c.high} L{c.low} C{c.close}")

        if KILL.exists():
            log("KILL_SWITCH present — analysis suppressed.")
            return
        candidate = detect_sweep(candles, CFG["filter"])
        if candidate:
            invoke_claude(candidate)
        else:
            log("No sweep candidate.")
    except Exception as e:
        log(f"on_kline error: {e}")


def main():
    LOGS.mkdir(exist_ok=True)
    bootstrap_history()
    ws = WebSocket(testnet=TESTNET, channel_type="linear")
    ws.kline_stream(interval=INTERVAL, symbol=SYMBOL, callback=on_kline)
    log("Watching kline.15 stream… (touch KILL_SWITCH to halt trading)")
    while True:
        time.sleep(30)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
