# Multi-Agent, Multi-Strategy Event-Driven Bracket System

Universal multi-agent trading system for Bybit (15m timeframe). Works with
**any AI agent framework** — Claude Code, OpenAI / Codex, Hermes, OpenClaw,
Ollama, or your own HTTP agent server. Switch frameworks with one line in
`config.yaml`.

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
AI agent (configurable backend — see config.yaml → agent.backend)
        Reads ORCHESTRATOR.md, runs sub-agents SERIALLY:
        1. <strategy>-analyst → APPROVE/REJECT + entry/stop/target
        2. event-guard        → CLEAR/BLACKOUT (FOMC, CPI, NFP, news)
        3. risk-manager       → exact qty or VETO (live equity via Bybit API)
        4. executor           → ONE atomic bracket order (entry+TP+SL), verified
        ▼
state/journal.json  ─  the system's memory between runs
```

---

## Quick start — Claude Code (default)

Open this folder in Claude Code and say: **"Follow RUNBOOK.md from Phase 0."**
The runbook drives the agent through every phase — environment bootstrap,
backtest validation, dry-run, testnet forward-testing, and go-live — fully
autonomously (no human confirmation steps).

## Quick start — other frameworks

1. Pick your backend in `config.yaml`:
   ```yaml
   agent:
     backend: openai          # or: openai_compatible | http
   ```
2. Fill in the backend's config block (API key env var, model, URL, etc.).
3. Run the daemon — everything else is identical:
   ```bash
   python daemon/candle_watcher.py
   ```

See `agents/INDEX.md` for full wiring guides (OpenAI, Hermes, OpenClaw,
Ollama, generic HTTP) and a MCP-to-REST mapping table for frameworks
without MCP support.

---

## Manual setup

### 1. Install Python deps
```bash
pip install -r requirements.txt
```
Requires Python ≥ 3.10.

### 2. Bybit API access

**With Claude Code (MCP):** the official MCP server is pre-configured in
`.mcp.json`. It spins up automatically — run `claude mcp list` to confirm
the `mcp__bybit__*` tools appear.

**Without MCP (all other backends):** the system uses the `pybit` library
directly (already in `requirements.txt`). Sub-agent instructions reference
MCP tool names; `agents/INDEX.md` maps each one to its `pybit` equivalent.

### 3. API key
Create a Bybit key with **trade permission only** — no withdrawal, no
transfer. Start on **testnet** (`testnet: true` in `config.yaml`).

Credentials are read from environment variables; the daemon writes them to
`.env` automatically on startup:
```bash
export BYBIT_API_KEY=xxx
export BYBIT_API_SECRET=yyy
export BYBIT_TESTNET=true   # default
```

### 4. Agent backend credentials (non-Claude Code)

```bash
# OpenAI
export OPENAI_API_KEY=sk-...

# OpenAI-compatible (Hermes, OpenClaw, Ollama, LM Studio)
export AGENT_API_KEY=...    # if your endpoint requires auth

# Generic HTTP (Hermas, custom servers)
export AGENT_API_KEY=...    # used in Authorization header if configured
```

### 5. Run the watcher
```bash
python daemon/candle_watcher.py
```
For 24/7: run under systemd or `tmux`/`supervisord` on a VPS. Example
systemd unit:
```ini
[Service]
WorkingDirectory=/opt/bybit-skill
ExecStart=/usr/bin/python3 daemon/candle_watcher.py
Restart=always
```

---

## Validate BEFORE running live (do this first)

```bash
python backtest/fetch_data.py --symbol BTCUSDT --days 365
python backtest/backtest.py --csv backtest/data/BTCUSDT_15m.csv
```

Then follow `backtest/BACKTEST.md` — a walk-forward + sensitivity + Monte
Carlo protocol that an AI agent can run autonomously without curve-fitting.
If the raw filter has no edge there, do not proceed to live trading.

---

## Controls

| Action | How |
|---|---|
| Kill switch | `touch KILL_SWITCH` in project root — no new analysis or trades |
| Daemon log | `logs/watcher.log` |
| Agent run logs | `logs/run_*.json` (one file per pipeline invocation) |
| Journal | `state/journal.json` — decisions, orders, daily P&L, swept levels |
| TCA | `state/tca.json` — rolling slippage vs backtest assumption |
| Daily report | `state/reports/<date>.md` — generated at UTC midnight |
| Alerts | Telegram (configure `alerts:` in `config.yaml`) |

The kill switch does NOT close open positions — do that manually on the exchange.

---

## Hard risk limits (config.yaml)

1% risk per trade, max 1 open position, 3% daily loss stop, 6% weekly loss
cap, 50% equity floor, min 2R, max 3× leverage. The risk-manager agent runs
`daemon/risk_engine.py` (fractional Kelly, streak throttle, correlation gate)
and holds absolute veto power. All limits are shared across all strategies.

---

## Agent backends (config.yaml → agent.backend)

| Backend | Use for | Credential env var |
|---|---|---|
| `claude_code` | Claude Code CLI (default) | — |
| `openai` | OpenAI GPT-4o / o3 / Codex | `OPENAI_API_KEY` |
| `openai_compatible` | Hermes, OpenClaw, Ollama, LM Studio | `AGENT_API_KEY` |
| `http` | Hermas, custom agent servers | `AGENT_API_KEY` |

Swap `backend:` to switch. All other pipeline logic is identical.
See `daemon/agent_runner.py` for implementation; `agents/INDEX.md` for
framework-specific wiring notes.

---

## Agent definitions

Sub-agent instructions are in two places:

| Directory | Used by |
|---|---|
| `agents/*.md` | Universal — any framework loads these as system prompts |
| `.claude/agents/*.md` | Claude Code — same content with YAML front-matter for native dispatch |

The canonical orchestrator prompt is in `ORCHESTRATOR.md`.
`CLAUDE.md` is a thin Claude Code compatibility shim that points to it.

---

## Before going live — non-negotiable

1. Weeks on testnet, reviewing every `logs/run_*.json` for agent reasoning quality.
2. Verify the executor's TP/SL verification path by deliberately breaking it once.
3. Start live with the minimum order size regardless of the sizing formula.
4. Understand: LLM judgment cannot be backtested deterministically. Collect a
   forward-test sample (50+ signals) before trusting any sizing above minimum.

This is experimental software, not financial advice. Strategies lose money in
conditions they weren't designed for. Automated systems fail in ways manual
trading doesn't (stuck orders, missed fills, API outages, model regressions).
Never run capital you can't afford to lose.

---

## Adding a new strategy

1. Create `daemon/strategies/<name>.py` with `NAME` and `detect(candles, params) -> Signal | None`.
2. Register it in `daemon/strategies/__init__.py`.
3. Create `agents/<name>-analyst.md` (universal) **and**
   `.claude/agents/<name>-analyst.md` (Claude Code, add YAML front-matter).
4. Add a `strategies.<name>` block in `config.yaml` with `enabled: false`.
5. Run the full `backtest/BACKTEST.md` protocol; enable only on a PASS.

Risk limits in `config.yaml` are shared across ALL strategies combined.

## Repository structure

```
daemon/
  candle_watcher.py    — WebSocket listener + strategy dispatch
  agent_runner.py      — AI backend abstraction (claude_code/openai/http/…)
  risk_engine.py       — deterministic position sizing (Kelly, streak, drawdown)
  tca.py               — transaction-cost analysis (slippage vs backtest)
  notifier.py          — Telegram alerts (config-gated)
  strategies/          — strategy detector modules
backtest/
  backtest.py          — core backtest engine (walk-forward, Monte Carlo, regime-split)
  optimize.py          — parallel grid search optimizer (Sharpe-ranked)
  sensitivity.py       — parameter sensitivity analysis
  walk_forward.py      — rolling walk-forward validation
  data_integrity.py    — gap/duplicate/OHLC sanity checks
  fetch_data.py        — candle download + integrity check
agents/                — universal sub-agent definitions (any framework)
.claude/agents/        — Claude Code sub-agent definitions (same content + front-matter)
state/                 — journal.json, strategy_health.json, tca.json, risk_budget.json
tests/                 — pytest unit tests (strategy math, risk engine)
scripts/
  daily_report.py      — UTC-midnight equity + performance report
ORCHESTRATOR.md        — canonical system prompt (framework-agnostic)
CLAUDE.md              — Claude Code shim → ORCHESTRATOR.md
RUNBOOK.md             — full lifecycle playbook (Phase 0 → 5)
config.yaml            — all configuration (strategies, risk limits, agent backend)
```
