"""
tca.py — transaction cost analysis over the journal.

Pure functions: read state/journal.json, compute live execution-quality
metrics, persist the calibrated values to state/tca.json so backtests can use
LIVE slippage instead of an optimistic assumption. Retail backtests overstate
edge 20-40% by ignoring this feedback loop.

Usage (CLI, used by execution-auditor agent and strategy-monitor):
    python daemon/tca.py            # recompute state/tca.json from the journal
    python daemon/tca.py --show     # print current TCA state
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JOURNAL = ROOT / "state" / "journal.json"
TCA_PATH = ROOT / "state" / "tca.json"

# What the backtest assumes by default (backtest.py --slip-bps default).
BACKTEST_SLIP_ASSUMPTION_BPS = 2.0
DEGRADED_MULTIPLE = 2.0  # live rolling slippage > 2x assumption => EXECUTION_DEGRADED


def fills_from_journal(journal: dict) -> list[dict]:
    """Extract every run that recorded an actual fill with slippage."""
    out = []
    for run in journal.get("runs", []):
        order = run.get("order") or {}
        if order.get("slippage_bps") is not None and order.get("fill_price"):
            out.append({
                "ts": run.get("ts"),
                "strategy": run.get("event", "").replace("_signal", ""),
                "slippage_bps": float(order["slippage_bps"]),
                "side": order.get("side"),
            })
    return out


def rolling_slippage_bps(journal: dict, n: int = 20) -> float | None:
    """Mean slippage of the last n filled trades. None if no fills yet."""
    fills = fills_from_journal(journal)
    if not fills:
        return None
    window = fills[-n:]
    return round(sum(f["slippage_bps"] for f in window) / len(window), 2)


def calibrated_slip_bps(journal: dict, n: int = 20) -> float:
    """Slippage value backtests should use: live rolling mean if we have
    >= 5 fills, otherwise the conservative default assumption."""
    fills = fills_from_journal(journal)
    if len(fills) < 5:
        return BACKTEST_SLIP_ASSUMPTION_BPS
    live = rolling_slippage_bps(journal, n)
    # Never calibrate BELOW the default — a lucky run of positive slippage
    # must not make backtests more optimistic.
    return max(BACKTEST_SLIP_ASSUMPTION_BPS, round(live, 2))


def compute_tca(journal: dict) -> dict:
    fills = fills_from_journal(journal)
    rolling = rolling_slippage_bps(journal)
    degraded = (rolling is not None
                and rolling > DEGRADED_MULTIPLE * BACKTEST_SLIP_ASSUMPTION_BPS)
    return {
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fills_recorded": len(fills),
        "rolling_slippage_bps_20": rolling,
        "calibrated_slip_bps": calibrated_slip_bps(journal),
        "backtest_assumption_bps": BACKTEST_SLIP_ASSUMPTION_BPS,
        "execution_degraded": degraded,
        "note": ("Live slippage exceeds 2x the backtest assumption — raise the "
                 "analyst min-RR requirement by 0.5 until this normalizes."
                 if degraded else "OK"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true", help="print current tca.json")
    args = ap.parse_args()

    if args.show:
        if TCA_PATH.exists():
            print(TCA_PATH.read_text())
        else:
            print("{} (no TCA state yet)")
        return

    journal = json.loads(JOURNAL.read_text()) if JOURNAL.exists() else {}
    tca = compute_tca(journal)
    TCA_PATH.write_text(json.dumps(tca, indent=2))
    print(json.dumps(tca, indent=2))
    sys.exit(0 if not tca["execution_degraded"] else 1)


if __name__ == "__main__":
    main()
