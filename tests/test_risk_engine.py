"""
Tests for daemon/risk_engine.py — sizing can only ever go DOWN from the
config ceiling, and every halt condition fires when it should.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "daemon"))

import risk_engine  # noqa: E402

CEILING = risk_engine.CFG["risk"]["risk_per_trade_pct"]


def make_journal(trades=None, open_positions=None, daily_pnl=0.0,
                 starting_equity=None):
    now = datetime.now(timezone.utc)
    runs = []
    for i, (strategy, r) in enumerate(trades or []):
        runs.append({
            "ts": (now - timedelta(hours=len(trades) - i)).isoformat(timespec="seconds"),
            "event": f"{strategy}_signal", "strategy": strategy,
            "order": {"realized_r": r},
        })
    j = {"open_positions": open_positions or [], "daily_pnl_pct": daily_pnl,
         "runs": runs}
    if starting_equity:
        j["starting_equity"] = starting_equity
    return j


def patch_journal(monkeypatch, journal, tmp_path):
    p = tmp_path / "journal.json"
    p.write_text(json.dumps(journal))
    monkeypatch.setattr(risk_engine, "JOURNAL", p)


# ------------------------- ceiling is inviolable -------------------------

def test_risk_never_exceeds_ceiling(monkeypatch, tmp_path):
    """Even a fantastic live record (90% wins at 3R) must cap at the ceiling."""
    trades = [("sweep", 3.0)] * 45 + [("sweep", -1.0)] * 5
    patch_journal(monkeypatch, make_journal(trades), tmp_path)
    out = risk_engine.evaluate("sweep", 10_000)
    assert out["verdict"] == "SIZED"
    assert out["risk_pct"] <= CEILING


def test_weak_edge_sizes_down(monkeypatch, tmp_path):
    """Marginal live record -> quarter-Kelly sizes below the ceiling."""
    # ~42% win rate at 1.5 RR: kelly_f = 0.419 - 0.581/1.5 = 0.032 ->
    # quarter-Kelly ~0.81% — below the 1% ceiling but above the 0.25% floor.
    # Interleaved wins keep weekly PnL positive and the loss streak short.
    trades = []
    for i in range(30):
        trades.append(("sweep", 1.5 if i % 10 < 4 else -1.0))
    trades.append(("sweep", 1.5))  # end on a win: streak multiplier = 1.0
    patch_journal(monkeypatch, make_journal(trades), tmp_path)
    out = risk_engine.evaluate("sweep", 10_000)
    assert out["verdict"] == "SIZED"
    assert out["checks"]["streak"]["multiplier"] == 1.0
    assert round(CEILING * 0.25, 3) < out["risk_pct"] < CEILING


def test_few_trades_uses_ceiling(monkeypatch, tmp_path):
    patch_journal(monkeypatch, make_journal([("sweep", 1.0)] * 5), tmp_path)
    out = risk_engine.evaluate("sweep", 10_000)
    assert out["risk_pct"] == CEILING
    assert "ceiling used" in out["checks"]["kelly"]["basis"]


# ----------------------------- streak throttle -----------------------------

def test_streak_3_losses_halves(monkeypatch, tmp_path):
    trades = [("sweep", 2.0)] * 30 + [("sweep", -1.0)] * 3
    patch_journal(monkeypatch, make_journal(trades), tmp_path)
    out = risk_engine.evaluate("sweep", 10_000)
    assert out["checks"]["streak"] == {"consecutive_losses": 3, "multiplier": 0.5}


def test_streak_5_losses_quarters(monkeypatch, tmp_path):
    trades = [("sweep", 2.0)] * 30 + [("sweep", -1.0)] * 5
    patch_journal(monkeypatch, make_journal(trades), tmp_path)
    out = risk_engine.evaluate("sweep", 10_000)
    assert out["checks"]["streak"]["multiplier"] == 0.25


def test_win_resets_streak(monkeypatch, tmp_path):
    trades = [("sweep", -1.0)] * 5 + [("sweep", 2.0)]
    patch_journal(monkeypatch, make_journal(trades), tmp_path)
    out = risk_engine.evaluate("sweep", 10_000)
    assert out["checks"]["streak"]["multiplier"] == 1.0


def test_streak_is_account_wide(monkeypatch, tmp_path):
    """Tilt protection counts losses from ANY strategy."""
    trades = [("sweep", 2.0)] * 30 + [("breakout", -1.0)] * 3
    patch_journal(monkeypatch, make_journal(trades), tmp_path)
    out = risk_engine.evaluate("sweep", 10_000)
    assert out["checks"]["streak"]["multiplier"] == 0.5


# ------------------------------- halts ----------------------------------

def test_weekly_drawdown_halts(monkeypatch, tmp_path):
    # 7 losses x 1R x 1% = -7% weekly < -6% limit
    trades = [("sweep", -1.0)] * 7
    patch_journal(monkeypatch, make_journal(trades), tmp_path)
    out = risk_engine.evaluate("sweep", 10_000)
    assert out["verdict"] == "HALT"
    assert "weekly_drawdown" in out["checks"]
    assert "FAIL" in out["checks"]["weekly_drawdown"]


def test_daily_stop_halts(monkeypatch, tmp_path):
    patch_journal(monkeypatch, make_journal(daily_pnl=-3.5), tmp_path)
    out = risk_engine.evaluate("sweep", 10_000)
    assert out["verdict"] == "HALT"
    assert "FAIL" in out["checks"]["daily_stop"]


def test_equity_floor_halts(monkeypatch, tmp_path):
    patch_journal(monkeypatch, make_journal(starting_equity=10_000), tmp_path)
    out = risk_engine.evaluate("sweep", 4_900)  # < 50% of 10k
    assert out["verdict"] == "HALT"
    assert "FAIL" in out["checks"]["equity_floor"]


def test_equity_above_floor_passes(monkeypatch, tmp_path):
    patch_journal(monkeypatch, make_journal(starting_equity=10_000), tmp_path)
    out = risk_engine.evaluate("sweep", 5_100)
    assert out["verdict"] == "SIZED"


# --------------------------- correlation gate ---------------------------

def test_correlated_open_position_vetoes(monkeypatch, tmp_path):
    """Two strategies with identical recent return streams -> r = 1.0 -> VETO."""
    pattern = [2.0, -1.0, 2.0, 2.0, -1.0, -1.0, 2.0, -1.0, 2.0, 2.0,
               -1.0, 2.0, -1.0, -1.0, 2.0, 2.0, -1.0, 2.0, -1.0, 2.0]
    trades = []
    for r in pattern:
        trades.append(("sweep", r))
        trades.append(("breakout", r))  # perfectly correlated
    journal = make_journal(trades, open_positions=[
        {"symbol": "BTCUSDT", "side": "buy", "qty": "0.01", "strategy": "breakout"}])
    patch_journal(monkeypatch, journal, tmp_path)
    out = risk_engine.evaluate("sweep", 10_000)
    assert out["verdict"] == "VETO"
    assert out["checks"]["correlation"]["blocked_by"] == "breakout"


def test_uncorrelated_open_position_allowed(monkeypatch, tmp_path):
    pattern_a = [2.0, -1.0] * 10
    pattern_b = [-1.0, 2.0] * 10  # anti-correlated
    trades = []
    for ra, rb in zip(pattern_a, pattern_b):
        trades.append(("sweep", ra))
        trades.append(("breakout", rb))
    journal = make_journal(trades, open_positions=[
        {"symbol": "BTCUSDT", "side": "buy", "qty": "0.01", "strategy": "breakout"}])
    patch_journal(monkeypatch, journal, tmp_path)
    out = risk_engine.evaluate("sweep", 10_000)
    assert out["verdict"] == "SIZED"
