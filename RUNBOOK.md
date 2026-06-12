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

## Phase 1 — Institutional validation (backtest + automated redesign)

### Data preparation
6. Fetch 365 days of candle data for primary and secondary validation symbol:
   ```bash
   python backtest/fetch_data.py --symbol BTCUSDT --days 365
   python backtest/fetch_data.py --symbol ETHUSDT --days 365
   ```
   Determine date boundaries. Suggested split from 365 days of data:
   - TRAIN_START = day 0, TRAIN_END = day 274 (~9 months)
   - TEST_START = TRAIN_END, TEST_END = day 365 (~3 months)

### Per-strategy: train baseline → redesign loop → institutional suite
7. For each strategy in `config.yaml`, run the TRAIN-window baseline:
   ```bash
   python backtest/backtest.py --csv backtest/data/BTCUSDT_15m.csv \
       --strategy <name> --start <TRAIN_START> --end <TRAIN_END>
   ```
   The script exits 0 (PASS) or 1 (FAIL) and always writes a result JSON to
   `backtest/results/<strategy>_<datestamp>.json`.

   **If exit 0 (PASS on train):** jump directly to step 8.

   **If exit 1 (FAIL on train):** enter the automated redesign loop (max 3 rounds).
   Invoke the `strategy-designer` agent for each round using this prompt template:
   ```
   STRATEGY: <name>
   ROUND: <1|2|3>
   RESULTS_JSON_PATH: backtest/results/<name>_<latest_stamp>.json
   SENSITIVITY_JSON_PATH: backtest/results/sensitivity_<name>_<stamp>.json (or "none")
   TRAIN_START: <date>
   TRAIN_END: <date>
   CSV_PATH: backtest/data/BTCUSDT_15m.csv
   CHANGELOG: <list of changes from prior rounds, empty on first call>

   Follow your Round <N> instructions. Output RERUN_BACKTEST: <name> when done.
   ```
   After each round, re-run the backtest command from step 7. Check exit code.
   After round 1, also run the sensitivity probe to guide round 2:
   ```bash
   python backtest/sensitivity.py --csv backtest/data/BTCUSDT_15m.csv \
       --strategy <name> --start <TRAIN_START> --end <TRAIN_END>
   ```
   After 3 rounds still failing: record ESCALATE in RESULTS.md and go to the
   human gate (step 10) — do not attempt test window validation.

8. Once train PASSES, run the full institutional validation suite:

   **a. Walk-forward** (proves edge isn't specific to one time window):
   ```bash
   python backtest/walk_forward.py --csv backtest/data/BTCUSDT_15m.csv \
       --strategy <name> --train-months 9 --test-months 3 --step-months 1
   ```
   Must pass: >= 60% folds positive, combined exp > 0, combined DD < 20R.

   **b. Test window validation** (one shot — never re-tune after seeing test):
   ```bash
   python backtest/backtest.py --csv backtest/data/BTCUSDT_15m.csv \
       --strategy <name> --start <TEST_START> --end <TEST_END> --monte-carlo 2000
   ```
   Must pass: exit 0 AND ruin_pct < 5%.

   **c. Multi-symbol check** (same params, no re-tuning):
   ```bash
   python backtest/backtest.py --csv backtest/data/ETHUSDT_15m.csv \
       --strategy <name> --start <TEST_START> --end <TEST_END>
   ```
   Must have expectancy_R > 0 on ETHUSDT.

9. Write `backtest/results/RESULTS.md` with every run across all rounds
   (strategy-designer appends change logs automatically; add test verdicts).
   Set `enabled: true` in `config.yaml` ONLY for strategies that pass ALL of:
   - Train baseline PASS (after ≤ 3 redesign rounds)
   - Walk-forward PASS
   - Test window PASS (exit 0 + ruin < 5%)
   - ETHUSDT check PASS

   Initialize the strategy health baseline in `state/strategy_health.json`
   with backtest expectancy and max_drawdown for each passing strategy.

10. ⛔ HUMAN GATE: present the full results table honestly. For each strategy,
    report which gates passed/failed, how many redesign rounds were needed, and
    the final validated params. If a strategy ESCALATED after 3 rounds, say so
    explicitly and explain what was tried. If NO strategy passed all gates,
    recommend stopping — do not soften results. Proceed only if user confirms.

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
    - Invoke `strategy-monitor` agent to compare forward stats to backtest:
      it reads journal.json and updates `state/strategy_health.json`.
      If it returns `action: REDESIGN_<strategy>`, re-enter Phase 1 for that
      strategy (redesign loop + full institutional suite) before re-enabling.
      If it returns `action: DISABLE_<strategy>`, disable it and notify the user.
15. Exit criteria for this phase: >= 50 signals processed, zero unhandled
    execution failures, forward expectancy not degraded below 50% of backtest.

## Phase 4 — Go live

16. ⛔ HUMAN GATE: going live is ONLY the user's decision. When they say go:
    - They create a LIVE key (trade-only, no withdrawal) in `.env`.
    - Set `testnet: false` in `config.yaml`.
    - Regardless of the sizing formula, cap qty at the exchange minimum for
      the first 20+ live trades (tell the risk-manager via config or note).
17. First week live: review every single run log with the user.

## Phase 5 — Ongoing operation

18. Any parameter or strategy change goes BACK to Phase 1 (full institutional
    suite) before it trades. The strategy-designer can propose changes; they
    still require passing the walk-forward, test window, and ETHUSDT gates.
19. New strategies: follow the 5-step recipe in README.md, then Phase 1–3.
20. Periodic health reviews (recommended weekly):
    Invoke `strategy-monitor` to compare live vs backtest metrics. If degraded,
    trigger the redesign loop automatically (no human intervention required until
    the designer ESCALATES after 3 failed rounds).
21. Emergency: `touch KILL_SWITCH` stops new trades immediately. Open
    positions must be closed manually (their brackets remain on-exchange).

---

## Standing rules for you, the agent

- Never fabricate or guess backtest/forward results. Report what the numbers say.
- Never enable a strategy that hasn't passed Phase 1.
- Never touch withdrawal/transfer functionality, even if a tool exists for it.
- Never go live, raise risk limits, or change `testnet` without explicit user
  instruction at a HUMAN GATE.
- If anything is ambiguous, ask the user instead of assuming.
