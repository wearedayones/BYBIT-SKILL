# Multi-Agent, Multi-Strategy Event-Driven Bracket System

Claude Code multi-agent trading system for Bybit (15m timeframe).
Strategy-pluggable: each strategy = a detector module + an analyst agent +
a config block. Shipped strategies: **sweep** (mean-reversion, enabled) and
**breakout** (momentum, disabled until it passes the backtest protocol).

## How it works

```
Bybit WebSocket (kline.15, confirm=true)
        │  every candle close
        ▼
candle_watcher.py  ──  runs ALL enabled strategy detectors (pure Python)
        │  only if a candidate exists (~5-10×/day instead of 96×)
        ▼
claude -p (headless)  ──  reads CLAUDE.md, runs subagents SERIALLY:
        1. <strategy>-analyst → APPROVE/REJECT + entry/stop/target
        2. event-guard     → CLEAR/BLACKOUT (FOMC, CPI, NFP, news)
        3. risk-manager    → exact qty or VETO (live equity via Bybit MCP)
        4. executor        → ONE atomic bracket order (entry+TP+SL), verified
        ▼
state/journal.json  ──  the system's memory between runs
```

## Quick start with Claude Code

Open this folder in Claude Code and say: **"Follow RUNBOOK.md from Phase 0."**
The runbook walks the agent through every phase — environment check,
backtest validation, a dry-run trade, testnet forward-testing, and the
go-live gates — pausing for your confirmation at each decision point.

## Manual setup

1. **Install deps**
   ```bash
   pip install pybit pyyaml
   npm install -g @anthropic-ai/claude-code
   ```
2. **Bybit MCP server** — preconfigured in `.mcp.json` with the official
   [bybit-exchange/trading-mcp](https://github.com/bybit-exchange/trading-mcp)
   (`npx -y bybit-official-trading-server@latest`, so it always pulls the
   latest version — no manual install). Run `claude mcp list` to confirm it
   connects. Tools are exposed as `mcp__bybit__*`, matching the
   `--allowedTools` list in `daemon/candle_watcher.py` and the `tools:` lines
   in `.claude/agents/*.md`.
3. **API key**: create a Bybit key with trade permission ONLY — no withdrawal,
   no transfer. Start on **testnet** (`testnet: true` in `config.yaml`).
4. **Run the watcher**
   ```bash
   python daemon/candle_watcher.py
   ```
   For 24/7: run under systemd or `tmux`/`supervisord` on a VPS so it survives
   disconnects. Example systemd unit:
   ```ini
   [Service]
   WorkingDirectory=/opt/liquidity-sweep-bot
   ExecStart=/usr/bin/python3 daemon/candle_watcher.py
   Restart=always
   ```

## Validate BEFORE running live (do this first)

```bash
python backtest/fetch_data.py --symbol BTCUSDT --days 365
python backtest/backtest.py --csv backtest/data/BTCUSDT_15m.csv
```
Then follow `backtest/BACKTEST.md` — a strict walk-forward protocol (train/test
split, max 3 tunable knobs, hard acceptance bar) designed so Claude Code can
adjust parameters WITHOUT curve-fitting. If the raw filter shows no edge there,
do not proceed to live trading.

## Controls

- **Kill switch**: `touch KILL_SWITCH` in project root → no new analysis or
  trades (does NOT close open positions — do that manually).
- **Logs**: `logs/watcher.log` (daemon) and `logs/run_*.json` (every Claude run).
- **Journal**: `state/journal.json` — decisions, orders, daily P&L, swept levels.

## Hard risk limits (config.yaml)

1% risk per trade, max 1 open position, 3% daily loss stop, min 2R, max 3x
leverage. The risk-manager agent treats these as ceilings and holds veto power.

## Before going live — non-negotiable

1. Weeks on testnet, reviewing every `logs/run_*.json` for agent reasoning quality.
2. Verify the executor's TP/SL verification path by deliberately breaking it once.
3. Start live with the minimum order size regardless of the sizing formula.
4. Understand: LLM judgment cannot be backtested deterministically. Collect a
   forward-test sample (50+ signals) before trusting any sizing above minimum.

This is experimental software, not financial advice. Liquidity-sweep strategies
lose money in strong trends; automated systems fail in ways manual trading
doesn't (stuck orders, missed fills, API outages). Never run capital you can't
afford to lose.

## Adding a new strategy

1. Create `daemon/strategies/<name>.py` with `NAME` and `detect(candles, params) -> Signal | None`.
2. Register it in `daemon/strategies/__init__.py`.
3. Add `.claude/agents/<name>-analyst.md`.
4. Add a `strategies.<name>` block in `config.yaml` with `enabled: false`.
5. Run the full `backtest/BACKTEST.md` protocol; enable only on a PASS.

Risk limits in `config.yaml` are shared across ALL strategies combined.
