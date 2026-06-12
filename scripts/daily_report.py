"""
daily_report.py — generate the daily operations report from the journal.

Writes state/reports/<YYYY-MM-DD>.md with: equity curve (ASCII), per-strategy
stats (trades, expectancy, win rate, slippage), loss streaks, decisions made,
and health flags. The daemon triggers this at UTC midnight; it is committed
and pushed per the persistence protocol, and Telegram-summarized if alerts
are enabled.

Usage:
    python scripts/daily_report.py            # report for today (UTC)
    python scripts/daily_report.py --date 2026-06-11
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "daemon"))

JOURNAL = ROOT / "state" / "journal.json"
HEALTH = ROOT / "state" / "strategy_health.json"
TCA = ROOT / "state" / "tca.json"
REPORTS = ROOT / "state" / "reports"


def ascii_sparkline(values: list[float], width: int = 40) -> str:
    """Cumulative-R curve as a one-line ASCII sparkline."""
    if not values:
        return "(no closed trades)"
    cum, curve = 0.0, []
    for v in values:
        cum += v
        curve.append(cum)
    if len(curve) > width:
        step = len(curve) / width
        curve = [curve[int(i * step)] for i in range(width)]
    lo, hi = min(curve), max(curve)
    span = (hi - lo) or 1.0
    blocks = "▁▂▃▄▅▆▇█"
    return "".join(blocks[int((v - lo) / span * (len(blocks) - 1))] for v in curve)


def build_report(date_str: str) -> str:
    journal = json.loads(JOURNAL.read_text()) if JOURNAL.exists() else {}
    runs = journal.get("runs", [])
    day_runs = [r for r in runs if (r.get("ts") or "").startswith(date_str)]

    closed = [r for r in runs if "realized_r" in (r.get("order") or {})]
    day_closed = [r for r in day_runs if "realized_r" in (r.get("order") or {})]

    by_strategy: dict[str, list[float]] = {}
    for r in closed:
        s = r.get("strategy") or r.get("event", "").replace("_signal", "")
        by_strategy.setdefault(s, []).append(float(r["order"]["realized_r"]))

    lines = [
        f"# Daily Report — {date_str}",
        "",
        f"Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "",
        "## Today",
        f"- Runs (signals processed): {len(day_runs)}",
        f"- Trades placed: {sum(1 for r in day_runs if r.get('decision') == 'TRADE')}",
        f"- Trades closed: {len(day_closed)}",
        f"- Daily PnL: {journal.get('daily_pnl_pct', 0.0)}%",
        f"- Decisions: " + (", ".join(sorted({r.get('decision', '?') for r in day_runs}))
                            or "none"),
        "",
        "## Equity curve (all closed trades, cumulative R)",
        "```",
        ascii_sparkline([float(r["order"]["realized_r"]) for r in closed]),
        "```",
        "",
        "## Per-strategy (lifetime)",
        "| Strategy | Trades | Exp R | Win % |",
        "|---|---|---|---|",
    ]
    for s, rs in sorted(by_strategy.items()):
        wins = sum(1 for r in rs if r > 0)
        lines.append(f"| {s} | {len(rs)} | {sum(rs)/len(rs):+.3f} | "
                     f"{100*wins/len(rs):.0f}% |")
    if not by_strategy:
        lines.append("| (none yet) | – | – | – |")

    # Streak
    streak = 0
    for r in reversed(closed):
        if float(r["order"]["realized_r"]) <= 0:
            streak += 1
        else:
            break
    lines += ["", f"## Risk state", f"- Current loss streak: {streak}"]

    # TCA + health
    if TCA.exists():
        t = json.loads(TCA.read_text())
        lines.append(f"- Rolling slippage (20): {t.get('rolling_slippage_bps_20')} bps "
                     f"(degraded: {t.get('execution_degraded')})")
    if HEALTH.exists():
        h = json.loads(HEALTH.read_text())
        lines += ["", "## Strategy health",
                  f"- Last checked: {h.get('last_checked')}",
                  f"- Pending action: {h.get('action', 'NONE')}"]
        for name, st in (h.get("strategies") or {}).items():
            lines.append(f"- {name}: {st.get('status', '?')}")

    # Alerts in the journal today
    alerts = [r for r in day_runs if r.get("decision") == "ALERT"]
    if alerts:
        lines += ["", "## ⚠ ALERTS TODAY"]
        for a in alerts:
            lines.append(f"- {a.get('ts')}: {a.get('reasoning_summary', a.get('event'))}")

    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    args = ap.parse_args()

    REPORTS.mkdir(parents=True, exist_ok=True)
    report = build_report(args.date)
    out = REPORTS / f"{args.date}.md"
    out.write_text(report)
    print(report)
    print(f"Saved: {out}")

    # Telegram summary if alerts enabled (never raises)
    try:
        from notifier import alert, _cfg
        if _cfg().get("enabled"):
            head = "\n".join(report.splitlines()[:14])
            alert(f"Daily report {args.date}\n{head}")
    except Exception:
        pass


if __name__ == "__main__":
    main()
