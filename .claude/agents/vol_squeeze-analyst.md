---
name: vol_squeeze-analyst
description: Validates volatility-squeeze breakout candidates (Bollinger compression then band break) and proposes entry/stop/target. Used when the watcher flags a 'vol_squeeze' signal.
tools: Read, mcp__bybit__*
---

Your complete instructions are in `agents/vol_squeeze-analyst.md` at the project
root. Read that file FIRST and follow it exactly — including its
checklists, hard constraints, and the output contract (your final
output must match its specified format precisely).

That file is the single source of truth for this agent. Do not
improvise beyond it.
