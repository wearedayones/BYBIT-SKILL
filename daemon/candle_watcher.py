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
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml
from pybit.unified_trading import HTTP, WebSocket

from dataclasses import asdict
from sweep_filter import Candle
from strategies import enabled_strategies
from strategies.regime import detect_regime, vol_regime
from notifier import alert

ROOT = Path(__file__).resolve().parent.parent
CFG = yaml.safe_load((ROOT / "config.yaml").read_text())
LOGS = ROOT / "logs"
KILL = ROOT / "KILL_SWITCH"

SYMBOL = CFG["symbol"]
TESTNET = CFG["testnet"]
INTERVAL = str(CFG.get("timeframe", "15"))  # set by Phase 1 to the validated timeframe
INTERVAL_MS = int(INTERVAL) * 60_000
JOURNAL = ROOT / "state" / "journal.json"
RECONCILE_EVERY = 8  # candles between position reconciliations (2h on 15m)

candles: list[Candle] = []
last_invoke_ts = 0
last_kline_ts = 0.0   # wall-clock time of last confirmed kline (watchdog)
candles_since_reconcile = 0


def load_env_keys() -> tuple[str, str]:
    """Read API key/secret from .env (written by bootstrap_env)."""
    env_path = ROOT / ".env"
    kv = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                kv[k.strip()] = v.strip()
    return kv.get("BYBIT_API_KEY", ""), kv.get("BYBIT_API_SECRET", "")


def bootstrap_env():
    """Auto-write .env from environment variables (autonomous.auto_write_env)."""
    if not CFG.get("autonomous", {}).get("auto_write_env", True):
        return
    env_path = ROOT / ".env"
    key = os.environ.get("BYBIT_API_KEY", "")
    secret = os.environ.get("BYBIT_API_SECRET", "")
    testnet = os.environ.get("BYBIT_TESTNET", "true")
    if key and secret:
        env_path.write_text(
            f"BYBIT_API_KEY={key}\nBYBIT_API_SECRET={secret}\nBYBIT_TESTNET={testnet}\n"
        )
        print(f"[bootstrap_env] Credentials auto-written to {env_path}")
    elif not env_path.exists() or not env_path.read_text().strip():
        print("ERROR: No credentials found. Set BYBIT_API_KEY and BYBIT_API_SECRET "
              "environment variables before running.", file=sys.stderr)
        sys.exit(1)


def log(msg: str):
    line = f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    with open(LOGS / "watcher.log", "a") as f:
        f.write(line + "\n")


def bootstrap_history():
    """Pull last 400 closed candles via REST so levels exist immediately.
    (trend_pullback with ema_slow=89 needs 269 candles of history.)"""
    http = HTTP(testnet=TESTNET)
    res = http.get_kline(category="linear", symbol=SYMBOL, interval=INTERVAL, limit=400)
    rows = sorted(res["result"]["list"], key=lambda r: int(r[0]))
    for r in rows[:-1]:  # drop the still-open candle
        candles.append(Candle(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])))
    log(f"Bootstrapped {len(candles)} candles for {SYMBOL}")


def reconcile_positions():
    """Compare exchange truth against journal.open_positions. The exchange is
    ALWAYS the source of truth — on mismatch, journal an ALERT and adopt it.
    Runs at startup (crash recovery) and every RECONCILE_EVERY candles."""
    key, secret = load_env_keys()
    if not key or not secret:
        log("reconcile: no API keys in .env — skipped (read-only mode).")
        return
    try:
        http = HTTP(testnet=TESTNET, api_key=key, api_secret=secret)
        res = http.get_positions(category="linear", symbol=SYMBOL)
        live = [
            {"symbol": p["symbol"], "side": p["side"].lower(),
             "qty": p["size"], "entry": float(p.get("avgPrice") or 0),
             "sl": float(p.get("stopLoss") or 0), "tp": float(p.get("takeProfit") or 0)}
            for p in res["result"]["list"] if float(p.get("size") or 0) > 0
        ]
    except Exception as e:
        log(f"reconcile: REST query failed ({e}) — will retry next cycle.")
        return

    journal = json.loads(JOURNAL.read_text()) if JOURNAL.exists() else {}
    recorded = journal.get("open_positions", [])

    def keyset(positions):
        return {(p.get("symbol"), p.get("side"), str(p.get("qty"))) for p in positions}

    if keyset(live) != keyset(recorded):
        log(f"reconcile MISMATCH: exchange={live} journal={recorded} — adopting exchange truth.")
        alert(f"Position reconcile MISMATCH on {SYMBOL}: exchange shows "
              f"{len(live)} position(s), journal had {len(recorded)}. "
              f"Journal updated to exchange truth — review logs/watcher.log.")
        journal["open_positions"] = live
        journal.setdefault("runs", []).append({
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event": "position_reconcile_mismatch",
            "decision": "ALERT",
            "reasoning_summary": "Exchange positions diverged from journal "
                                 "(daemon crash or missed fill). Journal updated to exchange truth.",
            "exchange_positions": live,
            "journal_positions_before": recorded,
        })
        JOURNAL.write_text(json.dumps(journal, indent=2))
    else:
        log(f"reconcile OK: {len(live)} open position(s) match journal.")


def invoke_claude(signal, bracket_cfg):
    global last_invoke_ts
    cooldown = CFG.get("invoke_cooldown_sec", 900)
    if time.time() - last_invoke_ts < cooldown:
        log("Signal found but inside cooldown — skipped.")
        return
    last_invoke_ts = time.time()

    payload = {
        "event": f"{signal.strategy}_signal",
        "strategy": signal.strategy,
        "symbol": SYMBOL,
        "timeframe": "15m",
        "utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "signal": {
            "direction": signal.direction,
            "candle": signal.candle,
            "context": signal.context,
            "stop_hint": signal.stop_hint,
            "key_price": signal.key_price,
        },
        "bracket_rules": bracket_cfg,
        "recent_candles": [asdict(c) for c in candles[-40:]],
    }

    prompt = (
        f"A 15m candle just closed and the '{signal.strategy}' pre-filter flagged a signal. "
        f"Follow the pipeline in CLAUDE.md exactly: dispatch {signal.strategy}-analyst, then "
        "event-guard, then risk-manager, then (only if all approve) executor — SERIALLY, one "
        "at a time. Update state/journal.json before exiting, whatever the outcome.\n\n"
        f"EVENT DATA:\n{json.dumps(payload, indent=2)}"
    )

    cmd = [
        "claude", "-p", prompt,
        "--allowedTools", "mcp__bybit__*", "Read", "Write", "Edit", "WebFetch", "WebSearch", "Task",
        "--max-turns", str(CFG.get("max_turns", 30)),
        "--output-format", "json",
    ]
    log(f"Invoking Claude Code: [{signal.strategy}] {signal.direction} @ key {signal.key_price}")
    # Retry policy: retry ONLY if the CLI failed without touching the journal
    # (i.e. it died before doing anything). Never retry after a timeout or a
    # journal write — the pipeline may already have placed an order.
    for attempt in range(1, 4):
        journal_mtime_before = JOURNAL.stat().st_mtime if JOURNAL.exists() else 0
        try:
            result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                                    timeout=CFG.get("invoke_timeout_sec", 600))
        except subprocess.TimeoutExpired:
            log("ERROR: Claude run timed out — check for stuck orders manually! (no retry)")
            alert(f"Claude pipeline TIMED OUT on {SYMBOL} {signal.strategy} signal — "
                  "an order may be partially placed. Check the exchange.")
            return
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        (LOGS / f"run_{stamp}.json").write_text(result.stdout or result.stderr)
        log(f"Claude run finished (exit {result.returncode}); log: logs/run_{stamp}.json")
        if result.returncode == 0:
            return
        journal_mtime_after = JOURNAL.stat().st_mtime if JOURNAL.exists() else 0
        if journal_mtime_after != journal_mtime_before:
            log("Claude exited non-zero but journal was modified — NOT retrying "
                "(an order may exist). Reconcile will verify next cycle.")
            return
        if attempt < 3:
            wait = 5 * (3 ** (attempt - 1))  # 5s, 15s
            log(f"Claude failed cleanly (attempt {attempt}/3) — retrying in {wait}s.")
            time.sleep(wait)
    log("ERROR: Claude invocation failed 3 times — signal dropped.")


def on_kline(msg):
    global last_kline_ts, candles_since_reconcile
    try:
        data = msg["data"][0]
        if not data.get("confirm"):
            return  # candle still forming
        last_kline_ts = time.time()
        c = Candle(int(data["start"]), float(data["open"]), float(data["high"]),
                   float(data["low"]), float(data["close"]), float(data["volume"]))
        if candles and c.ts <= candles[-1].ts:
            return  # duplicate
        candles.append(c)
        del candles[:-400]
        log(f"Candle closed: O{c.open} H{c.high} L{c.low} C{c.close}")

        candles_since_reconcile += 1
        if candles_since_reconcile >= RECONCILE_EVERY:
            candles_since_reconcile = 0
            reconcile_positions()

        if KILL.exists():
            log("KILL_SWITCH present — analysis suppressed.")
            return
        for module, params, bracket in enabled_strategies(CFG):
            # Regime filter: suppress strategy if current market regime doesn't match
            regime_cfg = CFG["strategies"].get(module.NAME, {}).get("regime_filter")
            if regime_cfg and regime_cfg.get("enabled"):
                regime = detect_regime(candles, regime_cfg)
                allowed = regime_cfg.get("allowed_regimes",
                                         ["ranging", "trending_up", "trending_down"])
                if regime not in allowed:
                    log(f"[{module.NAME}] Regime={regime}, allowed={allowed} — skipped.")
                    continue
            # Volatility-regime filter (low/normal/high realized-vol rank)
            allowed_vol = CFG["strategies"].get(module.NAME, {}).get("allowed_vol_regimes")
            if allowed_vol:
                vreg = vol_regime(candles)
                if vreg not in allowed_vol:
                    log(f"[{module.NAME}] VolRegime={vreg}, allowed={allowed_vol} — skipped.")
                    continue
            signal = module.detect(candles, params)
            if signal:
                invoke_claude(signal, bracket)
                break  # highest-priority signal wins; one invocation per candle
        else:
            log("No signal.")
    except Exception as e:
        log(f"on_kline error: {e}")


def start_ws() -> WebSocket:
    ws = WebSocket(testnet=TESTNET, channel_type="linear")
    ws.kline_stream(interval=INTERVAL, symbol=SYMBOL, callback=on_kline)
    return ws


def main():
    global last_kline_ts
    LOGS.mkdir(exist_ok=True)
    bootstrap_env()
    bootstrap_history()
    reconcile_positions()  # crash recovery: exchange truth wins on restart
    ws = start_ws()
    last_kline_ts = time.time()
    alert(f"Daemon started: {SYMBOL} {INTERVAL}m testnet={TESTNET}")
    log(f"Watching kline.{INTERVAL} stream… (touch KILL_SWITCH to halt trading)")

    # Watchdog: a WebSocket can die silently. If no confirmed kline arrives
    # within 2.5x the interval, tear down and reconnect.
    stale_after = 2.5 * INTERVAL_MS / 1000
    reconnects = 0
    last_report_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    while True:
        time.sleep(60)

        # Daily report at UTC midnight
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != last_report_date:
            try:
                subprocess.run([sys.executable, str(ROOT / "scripts" / "daily_report.py"),
                                "--date", last_report_date],
                               cwd=ROOT, capture_output=True, timeout=120)
                log(f"Daily report generated for {last_report_date}.")
            except Exception as e:
                log(f"Daily report failed: {e}")
            last_report_date = today

        if time.time() - last_kline_ts > stale_after:
            log(f"WATCHDOG: no kline for > {stale_after:.0f}s — reconnecting WebSocket.")
            try:
                ws.exit()
            except Exception as e:
                log(f"WATCHDOG: ws.exit() error ignored: {e}")
            try:
                ws = start_ws()
                last_kline_ts = time.time()
                reconnects += 1
                log("WATCHDOG: WebSocket reconnected.")
                if reconnects in (1, 5, 20):  # escalating, not spamming
                    alert(f"Daemon WebSocket reconnected (count={reconnects}) on {SYMBOL}.")
            except Exception as e:
                log(f"WATCHDOG: reconnect failed ({e}) — retrying in 60s.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
