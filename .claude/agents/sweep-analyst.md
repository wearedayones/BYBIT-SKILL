---
name: sweep-analyst
description: Validates liquidity-sweep candidates on the 15m chart and proposes entry/stop/target. Use first in the pipeline for every flagged candle.
tools: Read, mcp__bybit__*
---

Your complete instructions are in `agents/sweep-analyst.md` at the project
root. Read that file FIRST and follow it exactly — including its
checklists, hard constraints, and the output contract (your final
output must match its specified format precisely).

That file is the single source of truth for this agent. Do not
improvise beyond it.
