# Agent Index — Universal Sub-Agent Definitions

This directory contains framework-agnostic definitions for every sub-agent in
the trading system. These files work with any LLM framework that can:
1. Load a markdown file as a system prompt
2. Call the Bybit REST API (via MCP or direct HTTP / pybit)
3. Read and write files in the repository

## Quick-start for each framework

### Claude Code (native)
Agent definitions are also present in `.claude/agents/*.md` with YAML
front-matter for native dispatch. Claude Code loads them automatically.
No extra wiring needed.

### OpenAI / Codex / o-series
In `config.yaml` set:
```yaml
agent:
  backend: openai
  openai:
    api_key_env: OPENAI_API_KEY
    model: gpt-4o                   # or o3, o4-mini, etc.
    system_prompt_file: ORCHESTRATOR.md
```
Then run the daemon normally. The orchestrator prompt is loaded from
`ORCHESTRATOR.md`; sub-agent definitions are in this directory.

To dispatch a sub-agent, include its `agents/<name>.md` content in a
second `system` message or in the user prompt as a quoted block.

### Hermes / OpenClaw / any OpenAI-compatible endpoint
```yaml
agent:
  backend: openai_compatible
  openai_compatible:
    api_key_env: AGENT_API_KEY      # or leave blank if no auth
    base_url: http://localhost:11434/v1   # your endpoint
    model: your-model-name
    system_prompt_file: ORCHESTRATOR.md
```

### Generic HTTP (Hermas, custom agent servers)
```yaml
agent:
  backend: http
  http:
    url: http://localhost:8080/run
    system_prompt_file: ORCHESTRATOR.md
    headers:
      Authorization: "Bearer ${AGENT_API_KEY}"
    # body_template and response_field — see agent_runner.py
```

## Bybit API: MCP vs direct REST

All sub-agents reference Bybit tools by their MCP name (`mcp__bybit__createOrder`,
etc.). For frameworks without MCP support, map each call to the `pybit` REST API:

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

Credentials come from `.env` (written by `bootstrap_env()` in `candle_watcher.py`).

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
