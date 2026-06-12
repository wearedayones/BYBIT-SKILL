# RUNBOOK — Full Lifecycle Playbook (for Claude Code)

You (Claude Code) drive this entire process autonomously. Work through the
phases IN ORDER. All decisions are made by you — no human confirmation steps.
The only manual override is creating the `KILL_SWITCH` file.

---

## Phase 0 — Environment bootstrap (fully automated)

1. `pip install -r requirements.txt` and confirm `python3 --version` >= 3.10.

2. Verify the Bybit MCP is connected: check available tools. If tools are not
   named `mcp__bybit__*`, update the names in:
   - `daemon/candle_watcher.py` (`--allowedTools`)
   - every file in `.claude/agents/` (`tools:` line)

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

## Phase 1 — Institutional validation (fully automated)

### Data preparation
6. Fetch 365 days of candle data for both validation symbols:
   ```bash
   python backtest/fetch_data.py --symbol BTCUSDT --days 365
   python backtest/fetch_data.py --symbol ETHUSDT --days 365
   ```
   Determine date boundaries from the CSV. Suggested split:
   - TRAIN_START = first candle date, TRAIN_END ≈ +274 days (~9 months)
   - TEST_START = TRAIN_END, TEST_END = last candle date (~3 months)

### Per-strategy: train baseline → automated redesign loop → full suite
7. For each strategy in `config.yaml`, run the TRAIN-window baseline:
   ```bash
   python backtest/backtest.py --csv backtest/data/BTCUSDT_15m.csv \
       --strategy <name> --start <TRAIN_START> --end <TRAIN_END>
   ```
   Script exits 0 (PASS) or 1 (FAIL); always writes to
   `backtest/results/<strategy>_<datestamp>.json`.

   **Exit 0 → jump to step 8.**

   **Exit 1 → enter redesign loop (max 3 rounds, no human gate):**
   Invoke the `strategy-designer` agent for each round:
   ```
   STRATEGY: <name>
   ROUND: <1|2|3>
   RESULTS_JSON_PATH: backtest/results/<name>_<latest_stamp>.json
   SENSITIVITY_JSON_PATH: backtest/results/sensitivity_<name>_<stamp>.json (or "none")
   TRAIN_START: <date>
   TRAIN_END: <date>
   CSV_PATH: backtest/data/BTCUSDT_15m.csv
   CHANGELOG: <list of changes from prior rounds, empty on first>

   Follow your Round <N> instructions. Output RERUN_BACKTEST: <name> when done.
   ```
   After each round: re-run the backtest (step 7), check exit code. After round 1
   also run the sensitivity probe to guide round 2:
   ```bash
   python backtest/sensitivity.py --csv backtest/data/BTCUSDT_15m.csv \
       --strategy <name> --start <TRAIN_START> --end <TRAIN_END>
   ```
   If the designer outputs `STRATEGY_DISABLED: <name>` (all 3 rounds failed):
   skip to step 9, do not run the validation suite for this strategy.

8. Once train PASSES, run the full institutional validation suite:

   **a. Walk-forward** (proves edge persists across time):
   ```bash
   python backtest/walk_forward.py --csv backtest/data/BTCUSDT_15m.csv \
       --strategy <name> --train-months 9 --test-months 3 --step-months 1
   ```
   Must pass: >= 60% folds positive, combined exp > 0, combined DD < 20R.

   **b. Test window** (one shot — never re-tune after seeing test):
   ```bash
   python backtest/backtest.py --csv backtest/data/BTCUSDT_15m.csv \
       --strategy <name> --start <TEST_START> --end <TEST_END> --monte-carlo 2000
   ```
   Must pass: exit 0 AND `ruin_pct < 5%`.

   **c. Multi-symbol check** (same params, no re-tuning):
   ```bash
   python backtest/backtest.py --csv backtest/data/ETHUSDT_15m.csv \
       --strategy <name> --start <TEST_START> --end <TEST_END>
   ```
   Must have `expectancy_R > 0` on ETHUSDT.

9. **Autonomous outcomes — no confirmation required:**
   - Strategy passes ALL four gates (train + walk-forward + test + ETHUSDT):
     If `autonomous.auto_enable_strategies: true`, set `enabled: true` in
     `config.yaml`. Log: `"Strategy <name> autonomously enabled after passing
     all 4 validation gates."` Write baseline metrics to
     `state/strategy_health.json`.
   - Strategy failed (disabled after 3 redesign rounds or any suite gate):
     Leave `enabled: false`. Append to `backtest/results/RESULTS.md`:
     ```
     ## AUTONOMOUS DISABLE — <name> — <UTC>
     Reason: <failed train after 3 rounds | failed walk-forward | failed test | failed ETHUSDT>
     Last metrics: expectancy_R=<x>, profit_factor=<x>, max_drawdown_R=<x>
     ```
     Log to `state/journal.json`:
     `{"ts":"...","event":"strategy_auto_disabled","strategy":"<name>","reason":"..."}`
   - NO strategies passed → log to journal:
     `{"ts":"...","event":"system_halted","reason":"no strategies passed validation"}`
     Halt — do not proceed to Phase 2. Write `backtest/results/RESULTS.md` with
     the full failure summary and stop.
   - At least one strategy enabled → continue to Phase 2 automatically.

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

14. Autonomous health monitoring:
    After every 50 signals (tracked via `state/journal.json → runs` count),
    invoke `strategy-monitor`. It reads `journal.json`, compares live metrics
    to backtest baselines in `strategy_health.json`, and writes its action:
    - `NONE` → continue.
    - `REDESIGN_<strategy>` → immediately re-enter Phase 1 redesign loop for
      that strategy (strategy-designer → backtest → full suite). If it passes,
      re-enable it automatically. If it fails, auto-disable and log.
    - `DISABLE_<strategy>` → set `enabled: false` in config.yaml immediately,
      log to journal, restart daemon.

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
18. New strategies → follow the 5-step recipe in README.md, then Phase 1–3.
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
- Never touch withdrawal, transfer, or account-settings functionality.
- If ambiguous, choose the conservative option and log the decision to
  `state/journal.json` with a reasoning summary.
- KILL_SWITCH file is the only manual override — honour it immediately.
- Risk limits in `config.yaml → risk:` are hard ceilings; never exceed them.
