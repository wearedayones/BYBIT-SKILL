# Agent Index — Universal Sub-Agent Definitions

This directory contains framework-agnostic definitions for every sub-agent in
the trading system. This repo is a **skill** — the AI agent framework that
loads and runs it handles its own authentication. No API keys belong here.

The daemon's only job is to call your agent's headless CLI with the pipeline
prompt. Which CLI it calls is set by one line in `config.yaml → agent.backend`.

---

## Quick-start: pick your CLI

### Claude Code (default, no config change needed)
```bash
pip install -r requirements.txt
python daemon/candle_watcher.py
```
The daemon runs `claude -p "<prompt>" --allowedTools mcp__bybit__* …` for you.

---

### OpenAI Codex CLI
Install the Codex CLI and authenticate it (`codex login`), then:
```yaml
# config.yaml
agent:
  backend: codex
```
The daemon runs `codex exec --full-auto "<prompt>"`.

---

### Google Gemini CLI
Install and authenticate the Gemini CLI (`gemini auth`), then:
```yaml
agent:
  backend: gemini
```
The daemon runs `gemini --yolo -p "<prompt>"`.

---

### OpenCode
```yaml
agent:
  backend: opencode
```
The daemon runs `opencode run "<prompt>"`.

---

### Aider
```yaml
agent:
  backend: aider
```
The daemon runs `aider --yes --message "<prompt>"`.

---

### Any other CLI (OpenClaw, Hermas, custom agents)
Use `backend: custom` and supply the full argv list with a `{prompt}` placeholder:
```yaml
agent:
  backend: custom
  custom:
    command: ["myagent", "run", "--headless", "{prompt}"]
    timeout_sec: 600
```
The daemon substitutes the pipeline prompt for `{prompt}` and runs the command.

---

## Bybit API: MCP vs direct REST

All sub-agents reference Bybit tools by their MCP name (`mcp__bybit__createOrder`,
etc.). For agent CLIs that do not support MCP, point the agent at the pybit
REST API (already in `requirements.txt`). `daemon/fetch_data.py` shows the
connection pattern.

| MCP tool name | pybit method |
|---|---|
| `mcp__bybit__createOrder` | `HTTP.place_order()` |
| `mcp__bybit__getWalletBalance` | `HTTP.get_wallet_balance()` |
| `mcp__bybit__getPositionInfo` | `HTTP.get_positions()` |
| `mcp__bybit__getOpenOrders` | `HTTP.get_open_orders()` |
| `mcp__bybit__cancelOrder` | `HTTP.cancel_order()` |
| `mcp__bybit__setTradingStop` | `HTTP.set_trading_stop()` |
| `mcp__bybit__getOrderDetail` | `HTTP.get_order_detail()` |
| `mcp__bybit__getTradeHistory` | `HTTP.get_trade_history()` |
| `mcp__bybit__getClosedPnl` | `HTTP.get_closed_pnl()` |
| `mcp__bybit__getFundingRateHistory` | `HTTP.get_funding_rate_history()` |
| `mcp__bybit__getOpenInterest` | `HTTP.get_open_interest()` |
| `mcp__bybit__getMarketKline` | `HTTP.get_kline()` |
| `mcp__bybit__getRecentPublicTrades` | `HTTP.get_public_trade_history()` |
| `mcp__bybit__getLongShortRatio` | `HTTP.get_long_short_ratio()` |

Bybit credentials come from `.env` (written on startup by `bootstrap_env()`
in `candle_watcher.py` from `BYBIT_API_KEY` / `BYBIT_API_SECRET` env vars).

---

## Agent roster

| File | Pipeline stage | Required capabilities |
|---|---|---|
| `sweep-analyst.md` | step 3 | File read, Bybit API (kline, funding, OI) |
| `breakout-analyst.md` | step 3 | File read, Bybit API (kline, funding, OI) |
| `trend_pullback-analyst.md` | step 3 | File read, Bybit API (kline, funding, OI) |
| `rsi_reversion-analyst.md` | step 3 | File read, Bybit API (kline, funding, OI) |
| `vol_squeeze-analyst.md` | step 3 | File read, Bybit API (kline, funding, OI) |
| `zscore_reversion-analyst.md` | step 3 | File read, Bybit API (kline, funding, OI) |
| `event-guard.md` | step 4 | File read, web search |
| `risk-manager.md` | step 5 | File read, shell, Bybit API (balance, positions) |
| `executor.md` | step 6 | File read/write, Bybit API (orders, verification) |
| `execution-auditor.md` | step 2 | File read/write, shell, Bybit API (fills, PnL) |
| `portfolio-manager.md` | setup/review | File read/write |
| `red-team.md` | setup/review | File read, shell, file search |
| `strategy-designer.md` | setup/review | File read/edit/write |
| `strategy-monitor.md` | setup/review | File read/write, shell |

## I/O contracts summary

Every agent returns a single JSON object. Exact schemas are in each agent file.

| Agent | Output key | Values |
|---|---|---|
| analyst | `verdict` | `"APPROVE"` \| `"REJECT"` |
| event-guard | `verdict` | `"CLEAR"` \| `"BLACKOUT"` |
| risk-manager | `verdict` | `"SIZED"` \| `"VETO"` |
| executor | `verdict` | `"PLACED"` \| `"FAILED"` \| `"ALERT"` |
| execution-auditor | `verdict` | `"OK"` \| `"DEGRADED"` \| `"NOTHING_TO_AUDIT"` |
| portfolio-manager | writes `state/risk_budget.json` | — |
| red-team | first line | `"CONFIRM"` or `"CHALLENGE: <reason>"` |
| strategy-designer | last line | `"RERUN_BACKTEST: <strategy>"` or `"STRATEGY_DISABLED: <strategy>"` |
| strategy-monitor | `action` field in health JSON | `"NONE"` \| `"REDESIGN_*"` \| `"DISABLE_*"` |
