# RUNBOOK — Full Lifecycle Playbook

You (the AI agent, whichever backend is configured in `config.yaml → agent`)
drive this entire process autonomously. Work through the phases IN ORDER. All
decisions are made by you — no human confirmation steps. The only manual
override is creating the `KILL_SWITCH` file.

---

## Phase 0 — Environment bootstrap (fully automated)

1. `pip install -r requirements.txt` and confirm `python3 --version` >= 3.10.

2. Verify Bybit API connectivity:
   - **Claude Code**: check that `mcp__bybit__*` tools appear (`claude mcp list`).
     If tool names differ, update `config.yaml → agent.claude_code.allowed_tools`
     and the `tools:` lines in `.claude/agents/*.md`.
   - **Other backends**: confirm the `pybit` library can reach the Bybit REST
     API. Run `python -c "from pybit.unified_trading import HTTP; print(HTTP(testnet=True).get_server_time())"`.
     See `agents/INDEX.md` for the MCP-to-REST mapping if you need to adapt
     any sub-agent instructions.

3. **Auto-bootstrap credentials** (reads `config.yaml → autonomous.auto_write_env`):
   - Read `BYBIT_API_KEY`, `BYBIT_API_SECRET`, `BYBIT_TESTNET` from environment
     variables. If present, write them to `.env` and log
     `"Credentials auto-written to .env"`.
   - If env vars are absent but `.env` already exists with non-empty keys, use it.
   - If neither: log `ERROR: No credentials found — set BYBIT_API_KEY and
     BYBIT_API_SECRET environment variables.` and halt. Do NOT prompt anyone.

4. Confirm `config.yaml` has `testnet: true` (enforced when
   `autonomous.auto_go_live` is false).

5. Self-test: call a read-only MCP tool (`mcp__bybit__getServerTime`). If it
   fails: log the error and halt — do not proceed without a working connection.

## Phase 1 — Strategy Discovery (fully automated, never gives up)

The goal of Phase 1 is to FIND a configuration that honestly passes the full
validation suite — not to validate one fixed strategy and quit. The search
space is `strategy library × discovery.symbols × discovery.timeframes`, and
the library itself grows: when nothing passes, the agent designs new
strategies and keeps searching. Two things never change:

- **The acceptance bar is immutable.** Never weaken thresholds, never re-tune
  on the test window, never count a marginal result as a pass. A rigged pass
  deploys a losing strategy with real money — worse than no pass.
- **The agent never trades an unvalidated strategy.** "Keep searching" and
  "start trading" are different decisions with different bars.

### Data preparation
6. Fetch data for every symbol × timeframe in `config.yaml → discovery:`:
   ```bash
   python backtest/fetch_data.py --symbol <SYM> --days 365 --interval <TF>
   ```
   (e.g. BTCUSDT/ETHUSDT/SOLUSDT × 15/60). Output:
   `backtest/data/<SYM>_<TF>m.csv`. `fetch_data.py` runs a data integrity
   check automatically after download — fix FAIL verdicts before proceeding.
   Determine the split per file:
   - TRAIN_START = first candle date, TRAIN_END ≈ +274 days (~9 months)
   - TEST_START = TRAIN_END, TEST_END = last candle date (~3 months)

### Discovery sweep: library × symbols × timeframes
7. For every (strategy, symbol, timeframe) combination, run the **optimizer**
   to sweep ALL parameter combinations and find the best config on the TRAIN
   window:
   ```bash
   python backtest/optimize.py --csv backtest/data/<SYM>_<TF>m.csv \
       --strategy <name> --start <TRAIN_START> --end <TRAIN_END> \
       --top-n 5 --monte-carlo 500 --workers 4
   ```
   The optimizer ranks all combinations by Sharpe ratio (primary) then
   expectancy_R and applies Monte Carlo ruin filtering on the top-5.
   Record every result in `backtest/results/RESULTS.md`
   (strategy, symbol, TF, trades, exp_R, Sharpe, PF, DD, verdict).

   - Optimizer exits 0 (best config PASSES + ruin < 5%) → use the best
     params from `backtest/results/optimize_<name>_<stamp>.json` and promote
     straight to step 8.
   - Optimizer exits 1 (no config passes train thresholds) → enter the
     redesign loop (max 3 rounds) using the strategy-designer prompt:
     ```
     STRATEGY: <name>
     ROUND: <1|2|3>
     RESULTS_JSON_PATH: backtest/results/optimize_<name>_<latest_stamp>.json
     SENSITIVITY_JSON_PATH: <path or "none">
     TRAIN_START / TRAIN_END / CSV_PATH / CHANGELOG: <...>
     Follow your Round <N> instructions. Output RERUN_BACKTEST: <name> when done.
     ```
     Re-run `optimize.py` after each round; run sensitivity after round 1.
   - Hopeless combos (all combinations with deeply negative expectancy) →
     record and skip; do not waste redesign rounds on them.

8. Each combo that passes train runs the full validation suite ON ITS OWN
   symbol/timeframe data:

   **a. Walk-forward:**
   ```bash
   python backtest/walk_forward.py --csv backtest/data/<SYM>_<TF>m.csv \
       --strategy <name> --train-months 9 --test-months 3 --step-months 1
   ```
   Must pass: >= 60% folds positive, combined exp > 0, combined DD < 20R.

   **b. Test window (one shot — never re-tune after seeing test):**
   ```bash
   python backtest/backtest.py --csv backtest/data/<SYM>_<TF>m.csv \
       --strategy <name> --start <TEST_START> --end <TEST_END> --monte-carlo 2000
   ```
   Must pass: exit 0 AND `ruin_pct < 5%`.

   **c. Second-symbol check (same params, no re-tuning):** run the test window
   on a DIFFERENT symbol from discovery.symbols at the same timeframe.
   Must have `expectancy_R > 0`.

   NOTE on multiple testing: the wider the search, the more likely a lucky
   pass. That is exactly why all three suite gates are mandatory and why a
   marginal test-window pass with a walk-forward fail is a FAIL.

8d. **Red-team gate (after all four gates pass, before enabling):**
   Invoke the `red-team` agent with the paths to every results JSON for the
   combo (train, walk-forward, test, second-symbol, sensitivity, optimize).
   It tries to kill the pass: regime homogeneity, margin-of-pass, optimizer
   selection bias, artifact inconsistency, fee realism.
   - `CONFIRM` → proceed to step 9.
   - `CHALLENGE: <reason>` → run the tiebreaker, ONE regime-split backtest
     on the full data span:
     ```bash
     python backtest/backtest.py --csv backtest/data/<SYM>_<TF>m.csv \
         --strategy <name> --regime-split
     ```
     `regime_split.verdict: PASS` (profitable in ≥ 2 active regimes — or its
     only active regime if regime-filtered — and no active regime below
     −0.1R) overrides the challenge → proceed to step 9. FAIL → the combo
     is rejected; record both the challenge and the regime table in
     RESULTS.md and return it to discovery.

9. **Autonomous outcomes — no confirmation step:**
   - A combo passes ALL four gates AND the red-team gate → if
     `autonomous.auto_enable_strategies: true`: set `symbol:` and `timeframe:`
     in config.yaml to the validated combo, set the strategy `enabled: true`
     **with `shadow: true`** (paper-trade first — see Phase 3a), write
     baseline metrics to `state/strategy_health.json`, log the decision, and
     continue to Phase 2.
     (If multiple combos pass, prefer: highest walk-forward pct_folds_positive,
     then highest test expectancy.)
   - A strategy family exhausts 3 redesign rounds → designer outputs
     `STRATEGY_DISABLED: <name>`; record it and move on to the next candidate.
   - **The ENTIRE search space is exhausted with no pass → DO NOT STOP.**
     Enter the expansion loop:
       1. Invoke strategy-designer in Round-3 mode to create 1-2 genuinely NEW
          strategy designs (different signal families from what failed — e.g.
          if all reversion failed, design carry/momentum/session-based ideas).
          Designer must register them (module + REGISTRY + config block +
          analyst file + PARAM_SPACES entry).
       2. Re-run steps 7-8 for the new designs only.
       3. Log each completed sweep to `state/journal.json`:
          `{"ts":"...","event":"discovery_sweep_complete","combos_tested":N,
            "passes":0,"new_strategies_added":[...]}`
       4. After every `discovery.refresh_days` days, re-fetch fresh data and
          re-run the full sweep — markets change; an edge can appear in new
          data that wasn't in the old window.
     The agent keeps discovering indefinitely. It reports progress honestly
     ("47 combos tested, 0 passed, 2 new designs queued") but it NEVER
     enables an unvalidated strategy just to have something running, and it
     NEVER weakens a gate to manufacture a pass.

## Phase 2 — Dry-run (automated pipeline test)

10. With testnet active, construct an event payload from the most recent candle
    in the training data and run the full agent pipeline (analyst → event-guard
    → risk-manager → executor) so a real testnet bracket order is placed.
11. Verify on testnet that the order exists WITH both TP and SL attached.
    Cancel it. If verification fails, fix the executor flow and retry once.
12. Confirm `state/journal.json` was written correctly.

## Phase 3 — Testnet forward-test (automated monitoring)

13. Start the daemon:
    ```bash
    python daemon/candle_watcher.py
    ```
    Log: `"Daemon started at <utc>. Symbol=<SYMBOL> testnet=<bool>."`
    Run under `tmux` or `systemd` for 24/7 uptime — no human confirmation needed.

13a. **Shadow promotion (paper-trade gate before real orders):**
    Every newly validated strategy starts with `shadow: true` — the full
    pipeline runs and journals simulated brackets, but no exchange order is
    placed. The monitor auto-promotes (`shadow: false`) when:
    - ≥ 50 shadow trades are journaled, AND
    - shadow rolling expectancy ≥ 50% of the backtest expectancy.
    If after 50 shadow trades expectancy is below that bar, treat it as a
    REDESIGN (the backtest doesn't survive contact with live data).
    Redesigned strategies returning from the Phase 1 loop ALSO re-enter
    through shadow mode — nothing goes from redesign straight to real orders.

14. Autonomous health monitoring:
    After every 50 signals (tracked via `state/journal.json → runs` count),
    invoke `strategy-monitor`. It reads `journal.json`, compares live metrics
    to backtest baselines in `strategy_health.json` (including live IC and
    TCA slippage), and writes its action:
    - `NONE` → continue.
    - `REDESIGN_<strategy>` → immediately re-enter Phase 1 redesign loop for
      that strategy (strategy-designer → backtest → full suite). If it passes,
      re-enable it automatically **via shadow mode** (step 13a). If it fails,
      auto-disable and log.
    - `DISABLE_<strategy>` → set `enabled: false` in config.yaml immediately,
      log to journal, restart daemon.
    After every monitor run with 2+ strategies enabled, invoke
    `portfolio-manager` to refresh `state/risk_budget.json`.

15. Phase 3 exit criteria (checked automatically by the monitor):
    - >= 50 signals processed
    - Zero unhandled execution failures (no ALERT entries in journal)
    - Rolling expectancy >= 50% of backtest expectancy
    When all three are met, proceed to Phase 4 automatically.

## Phase 4 — Go live (autonomous, gated by config)

16. Triggered automatically when Phase 3 exit criteria are met AND
    `config.autonomous.auto_go_live: true`.

    Agent actions (no human step):
    - Set `testnet: false` in `config.yaml`.
    - Log: `"AUTONOMOUS LIVE DEPLOYMENT at <utc>. All validation and forward-test
      gates passed."` Write to `state/journal.json`:
      `{"ts":"...","event":"went_live_auto","strategies_enabled":["..."]}`
    - Restart daemon.

    If `auto_go_live: false` (the default): remain on testnet indefinitely.
    After each Phase 3 health check, log:
    `"auto_go_live is false — staying on testnet. Set autonomous.auto_go_live:
    true in config.yaml to authorize live deployment."`

## Phase 5 — Ongoing autonomous operation

17. Any parameter or strategy change → automatically re-enter Phase 1 (full
    institutional suite) before it trades. Strategy-designer proposes; the
    backtest suite validates; config.yaml is updated automatically.
    All re-validations of strategies that have live fill history MUST use
    `--slip-bps live` (TCA-calibrated slippage from state/tca.json) — never
    the optimistic default.
18. New strategies → follow the recipe in README.md (create module, register,
    add both `agents/<name>-analyst.md` and `.claude/agents/<name>-analyst.md`,
    add config block), then Phase 1–3.
    No human gates in that process either.
19. Health reviews → `strategy-monitor` runs after every 50 signals. On
    REDESIGN or DISABLE: agent acts immediately and logs. No human step.
20. Emergency: `touch KILL_SWITCH` in the project root stops all new analysis
    and trades immediately. This is the only manual override in the system.
    Open positions must still be closed manually via the exchange UI.

---

## Standing rules for autonomous operation

- Never fabricate or guess backtest/forward results — report what numbers say.
- Never enable a strategy that hasn't passed all 4 Phase 1 gates.
- Never weaken an acceptance threshold or re-tune on the test window.
- Never touch withdrawal, transfer, or account-settings functionality.
- If ambiguous, choose the conservative option and log the decision to
  `state/journal.json` with a reasoning summary.
- KILL_SWITCH file is the only manual override — honour it immediately.
- Risk limits in `config.yaml → risk:` are hard ceilings; never exceed them.
- **Persist everything**: commit and push after every material change (new
  strategies, results, config, journal). The repo is the only durable memory —
  uncommitted work is destroyed on reclone. See ORCHESTRATOR.md → Persistence protocol.
- **Ground every claim**: metrics come only from artifacts read this session.
  See ORCHESTRATOR.md → Grounding protocol.
