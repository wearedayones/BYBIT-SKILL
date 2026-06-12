"""
notifier.py — critical-alert delivery (Telegram), config-gated.

Philosophy: ALERT FATIGUE IS A FAILURE MODE. Only events that demand the
operator's attention go out; everything else stays in the journal.

Critical events:  TRADE placed, ALERT journal entries, daily/weekly stop hit,
monitor DISABLE/REDESIGN, daemon crash/restart, equity floor breach.

Setup (optional — system runs fine without it):
  config.yaml:   alerts: { enabled: true, channel: telegram }
  environment:   TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

Every send is also appended to logs/alerts.log so the journal of what was
(or would have been) sent survives even with alerts disabled.
"""

import json
import os
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "logs"


def _cfg() -> dict:
    try:
        return yaml.safe_load((ROOT / "config.yaml").read_text()).get("alerts", {}) or {}
    except Exception:
        return {}


def _log_line(status: str, msg: str):
    LOGS.mkdir(exist_ok=True)
    line = (f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] "
            f"{status}: {msg}\n")
    with open(LOGS / "alerts.log", "a") as f:
        f.write(line)


def alert(msg: str) -> bool:
    """Send a critical alert. Returns True if delivered (or intentionally
    disabled), False on delivery failure. NEVER raises — an alerting failure
    must not break the trading path."""
    cfg = _cfg()
    if not cfg.get("enabled"):
        _log_line("SUPPRESSED(disabled)", msg)
        return True

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        _log_line("SUPPRESSED(no-creds)", msg)
        return True

    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": f"\N{POLICE CARS REVOLVING LIGHT} {msg}",
        }).encode()
        with urllib.request.urlopen(url, data=data, timeout=10) as resp:
            ok = json.loads(resp.read()).get("ok", False)
        _log_line("SENT" if ok else "FAILED(api)", msg)
        return ok
    except Exception as e:
        _log_line(f"FAILED({e})", msg)
        return False


if __name__ == "__main__":
    import sys
    alert(" ".join(sys.argv[1:]) or "notifier test message")
