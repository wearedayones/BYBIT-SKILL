# RUNBOOK — Full Lifecycle Playbook (for Claude Code)

You (Claude Code) drive this entire process. Work through the phases IN ORDER.
Never skip a gate. At each ⛔ HUMAN GATE, stop and ask the user — those
decisions are theirs, not yours.

---

## Phase 0 — Environment check

1. `pip install -r requirements.txt` (and confirm `python3 --version` >= 3.10).
2. Verify the Bybit MCP is connected: run `claude mcp list` / check available
   tools. If tools are not named `mcp__bybit__*`, update the names in:
   - `daemon/candle_watcher.py` (`--allowedTools`)
   - every file in `.claude/agents/` (`tools:` line)
3. ⛔ HUMAN GATE: ask the user to create `.env` from `.env.example` with a
   **testnet** API key (trade permission only, NO withdrawal/transfer). Never
   ask them to paste keys into chat; they edit the file themselves.
4. Confirm `config.yaml` has `testnet: true`. If not, set it.
5. Sanity test: call a read-only MCP tool (e.g. get wallet balance or
   server time). If it fails, debug the MCP config before continuing.

## Phase 1 — Validation (backtest)

6. Follow `backtest/BACKTEST.md` EXACTLY, for each strategy in `config.yaml`:
   fetch 365 days of data, train/test split, baseline, limited tuning on
   train only, single validation on test.
7. Write `backtest/results/RESULTS.md` with every run, including failures.
8. Apply outcomes to `config.yaml`: passing strategies `enabled: true` with
   their validated params; failing strategies `enabled: false`.
9. ⛔ HUMAN GATE: present the results honestly. If NO strategy passed, say so
   plainly and recommend stopping. Do not soften failed results. Proceed only
   if the user confirms.

## Phase 2 — Dry-run the pipeline (one manual cycle)

10. With testnet still on, simulate one signal end-to-end: take a recent
    candle from the data, construct the event payload by hand, and run the
    full agent pipeline (analyst → event-guard → risk-manager → executor) so
    a real testnet bracket order is placed.
11. Verify on testnet that the order exists WITH both TP and SL attached.
    Then cancel it. If verification fails, fix the executor flow first.
12. Check `state/journal.json` was written correctly.

## Phase 3 — Testnet forward-test (weeks, not days)

13. Start the daemon: `python daemon/candle_watcher.py`.
    ⛔ HUMAN GATE: for 24/7 operation the user must run this on an
    always-on machine/VPS (systemd unit example is in README.md). Confirm
    with them where it will run.
14. Ongoing duties when the user asks for a review:
    - Read `logs/run_*.json` — is each agent's reasoning sound? Any ALERTs?
    - Read `state/journal.json` — decisions, P&L, daily-stop behavior.
    - Compare forward-test stats to backtest expectations.
15. Exit criteria for this phase: >= 50 signals processed, zero unhandled
    execution failures, forward expectancy not drastically below backtest.

## Phase 4 — Go live

16. ⛔ HUMAN GATE: going live is ONLY the user's decision. When they say go:
    - They create a LIVE key (trade-only, no withdrawal) in `.env`.
    - Set `testnet: false` in `config.yaml`.
    - Regardless of the sizing formula, cap qty at the exchange minimum for
      the first 20+ live trades (tell the risk-manager via config or note).
17. First week live: review every single run log with the user.

## Phase 5 — Ongoing operation

18. Any parameter or strategy change goes BACK to Phase 1 before it trades.
19. New strategies: follow the 5-step recipe in README.md, then Phase 1–3.
20. Emergency: `touch KILL_SWITCH` stops new trades immediately. Open
    positions must be closed manually (their brackets remain on-exchange).

---

## Standing rules for you, the agent

- Never fabricate or guess backtest/forward results. Report what the numbers say.
- Never enable a strategy that hasn't passed Phase 1.
- Never touch withdrawal/transfer functionality, even if a tool exists for it.
- Never go live, raise risk limits, or change `testnet` without explicit user
  instruction at a HUMAN GATE.
- If anything is ambiguous, ask the user instead of assuming.
