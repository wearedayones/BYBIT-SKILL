# Multi-Agent, Multi-Strategy Event-Driven Bracket System

Universal multi-agent trading system for Bybit (15m timeframe). This repo is
a **skill** — it is loaded and driven by whatever AI agent you already use.
No AI API keys live here; the agent handles its own authentication.

Works with any agent that has a headless / non-interactive CLI mode. Configure
it once with a single `command:` list in `config.yaml` and the daemon calls it
for every trading signal — no agent-type names, no presets, no special cases.

Strategy-pluggable: each strategy = a detector module + an analyst agent +
a config block. Shipped strategies: **sweep** (mean-reversion, enabled) and
**breakout** (momentum, disabled until it passes the backtest protocol).

---

## How it works

```
Bybit WebSocket (kline.15, confirm=true)
        │  every candle close
        ▼
candle_watcher.py  ─  runs ALL enabled strategy detectors (pure Python)
        │  only if a candidate exists (~5-10×/day instead of 96×)
        ▼
Your agent CLI  (set config.yaml → agent.backend)
        Reads ORCHESTRATOR.md, runs sub-agents SERIALLY:
        1. <strategy>-analyst → APPROVE/REJECT + entry/stop/target
        2. event-guard        → CLEAR/BLACKOUT (FOMC, CPI, NFP, news)
        3. risk-manager       → exact qty or VETO (live equity via Bybit API)
        4. executor           → ONE atomic bracket order (entry+TP+SL), verified
        ▼
state/journal.json  ─  the system's memory between runs
```

---

## Quick start

### 1. Install Python deps
```bash
pip install -r requirements.txt
```
Python ≥ 3.10 required.

### 2. Set your Bybit credentials
The daemon reads from environment variables and writes `.env` on startup:
```bash
export BYBIT_API_KEY=your_key
export BYBIT_API_SECRET=your_secret
export BYBIT_TESTNET=true      # keep true until validated
```
Create the Bybit key with **trade permission only** — no withdrawal, no transfer.

### 3. Point the daemon at your agent CLI

Add one `command:` list in `config.yaml`:

```yaml
agent:
  command: ["your-agent-cli", "run-headless-flag", "{prompt}"]
  timeout_sec: 600
```

`{prompt}` is replaced with the pipeline prompt at runtime. The rest of the
argv is passed verbatim. The agent CLI handles its own auth — nothing else
goes in `config.yaml`. If `command:` is absent the daemon defaults to the
Claude Code CLI (`claude -p {prompt} --allowedTools mcp__bybit__* …`).

### 4. Run
```bash
python daemon/candle_watcher.py
```
For 24/7: run under systemd or `tmux`/`supervisord` on a VPS:
```ini
[Service]
WorkingDirectory=/opt/bybit-skill
ExecStart=/usr/bin/python3 daemon/candle_watcher.py
Restart=always
```

### 5. Start
```bash
python daemon/candle_watcher.py
```

**Claude Code users:** open this folder in Claude Code and say
**"Follow RUNBOOK.md from Phase 0."** The runbook drives the agent through
every phase — bootstrap, backtest, dry-run, testnet, go-live — autonomously.

---

## Validate BEFORE running live

```bash
python backtest/fetch_data.py --symbol BTCUSDT --days 365
python backtest/backtest.py --csv backtest/data/BTCUSDT_15m.csv
```

Follow `backtest/BACKTEST.md` — a walk-forward + sensitivity + Monte Carlo
protocol. If the raw filter has no edge there, do not proceed.

---

## Controls

| Action | How |
|---|---|
| Kill switch | `touch KILL_SWITCH` in project root — stops all new analysis/trades |
| Daemon log | `logs/watcher.log` |
| Agent run logs | `logs/run_*.json` (one file per pipeline invocation) |
| Journal | `state/journal.json` — decisions, orders, daily P&L, swept levels |
| TCA | `state/tca.json` — rolling slippage vs backtest assumption |
| Daily report | `state/reports/<date>.md` — generated at UTC midnight |
| Alerts | Telegram (configure `alerts:` in `config.yaml`) |

Kill switch does NOT close open positions — do that manually on the exchange.

---

## Hard risk limits (config.yaml)

1% risk per trade, max 1 open position, 3% daily loss stop, 6% weekly loss
cap, 50% equity floor, min 2R, max 3× leverage. The risk-manager agent runs
`daemon/risk_engine.py` (fractional Kelly, streak throttle, correlation gate)
and holds absolute veto power. All limits are shared across all strategies.

---

## Agent definitions

Sub-agent instructions live in two parallel locations:

| Directory | Used by |
|---|---|
| `agents/*.md` | Universal — any agent CLI can read these as a system prompt |
| `.claude/agents/*.md` | Claude Code — same content with YAML front-matter for native sub-agent dispatch |

The canonical orchestrator prompt is `ORCHESTRATOR.md`.
`CLAUDE.md` is a thin shim that Claude Code auto-reads and points back to it.

See `agents/INDEX.md` for per-CLI setup notes and a MCP-to-REST API mapping
table (for CLIs without MCP support, the sub-agents call Bybit via `pybit`).

---

## Before going live — non-negotiable

1. Weeks on testnet, reviewing every `logs/run_*.json` for agent reasoning quality.
2. Verify the executor's TP/SL verification path by deliberately breaking it once.
3. Start live with the minimum order size regardless of the sizing formula.
4. LLM judgment cannot be backtested deterministically — collect 50+ forward-test
   signals before trusting any sizing above minimum.

This is experimental software, not financial advice. Strategies lose money in
conditions they weren't designed for. Automated systems fail in ways manual
trading doesn't. Never run capital you can't afford to lose.

---

## Adding a new strategy

1. Create `daemon/strategies/<name>.py` with `NAME` and `detect(candles, params) -> Signal | None`.
2. Register it in `daemon/strategies/__init__.py`.
3. Create `agents/<name>-analyst.md` (universal) **and**
   `.claude/agents/<name>-analyst.md` (Claude Code — add YAML front-matter).
4. Add a `strategies.<name>` block in `config.yaml` with `enabled: false`.
5. Run the full `backtest/BACKTEST.md` protocol; enable only on a PASS.

Risk limits in `config.yaml` are shared across ALL strategies combined.

---

## Repository structure

```
daemon/
  candle_watcher.py    — WebSocket listener + strategy dispatch
  agent_runner.py      — universal agent CLI dispatcher (command template)
  risk_engine.py       — deterministic position sizing (Kelly, streak, drawdown)
  tca.py               — transaction-cost analysis (slippage vs backtest)
  notifier.py          — Telegram alerts (config-gated)
  strategies/          — strategy detector modules
backtest/
  backtest.py          — core engine (walk-forward, Monte Carlo, regime-split)
  optimize.py          — parallel grid search optimizer (Sharpe-ranked)
  sensitivity.py       — parameter sensitivity analysis
  walk_forward.py      — rolling walk-forward validation
  data_integrity.py    — gap/duplicate/OHLC sanity checks
  fetch_data.py        — candle download + integrity check
agents/                — universal sub-agent definitions (any agent CLI)
.claude/agents/        — Claude Code sub-agent definitions (same + front-matter)
state/                 — journal.json, strategy_health.json, tca.json, risk_budget.json
tests/                 — pytest unit tests (strategy math, risk engine)
scripts/
  daily_report.py      — UTC-midnight equity + performance report
ORCHESTRATOR.md        — canonical orchestrator system prompt
CLAUDE.md              — Claude Code shim → ORCHESTRATOR.md
RUNBOOK.md             — full lifecycle playbook (Phase 0–5)
config.yaml            — all config: strategies, risk limits, agent CLI selection
```
