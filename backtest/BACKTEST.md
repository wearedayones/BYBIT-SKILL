# Backtest & Tuning Protocol (for Claude Code)

This is the procedure for validating and adjusting the pre-filter BEFORE the
system trades. Follow it exactly — the failure mode here is curve-fitting,
which produces beautiful backtests and dead accounts.

## What this backtest can and cannot prove

- It tests ONLY the deterministic pre-filter + fixed-RR brackets.
- It does NOT include the LLM agents. If the raw filter has no edge here, the
  LLM layer will not rescue it — abandon or redesign. If it has a small edge,
  the agents' job is to keep it, not create it.

## Procedure

1. **Fetch data** (run locally, needs Bybit API access):
   ```bash
   python backtest/fetch_data.py --symbol BTCUSDT --days 365
   ```

2. **Split the data — and never peek at the test window while tuning:**
   - TRAIN: first ~9 months
   - TEST:  last ~3 months (untouched until step 5)

3. **Baseline on TRAIN** with config defaults — run PER STRATEGY:
   ```bash
   python backtest/backtest.py --csv backtest/data/BTCUSDT_15m.csv \
       --strategy sweep --start <t0> --end <train_end> --out backtest/results/sweep_baseline
   python backtest/backtest.py --csv backtest/data/BTCUSDT_15m.csv \
       --strategy breakout --start <t0> --end <train_end> --out backtest/results/breakout_baseline
   ```
   Generic strategy params can be tuned with `--param k=v`
   (e.g. `--param range_lookback=64 --param min_body_atr=1.5` for breakout).

4. **Tune on TRAIN only.** Grid over AT MOST these three knobs, coarse steps:
   - `--min-wick` ∈ {25, 35, 45}
   - `--swing`    ∈ {2, 3}
   - `--rr`       ∈ {1.5, 2.0, 2.5}
   Selection rule: highest expectancy_R with **≥ 80 trades** and
   profit_factor ≥ 1.1. Fewer than 80 trades = statistical noise, reject.
   Maximum TWO tuning rounds, ever. Do not add new parameters to "fix" results.

5. **Validate once on TEST** with the chosen params. Acceptance bar:
   - expectancy_R > 0 after fees, AND
   - profit_factor ≥ 1.05, AND
   - max_drawdown_R < 15
   PASS → write chosen params into `config.yaml`, record everything in
   `backtest/results/RESULTS.md`, proceed to testnet forward-testing.
   FAIL → the edge doesn't generalize. Do NOT re-tune on the test window.
   Report honestly and stop.

6. **Robustness spot-checks** (report, don't tune on them):
   - Re-run TEST with `--fees-bps 8 --slip-bps 4` (bad execution scenario).
   - Run a second symbol (e.g. ETHUSDT) with the same params.
   - Note the per-level-kind breakdown: if all profit comes from one level
     kind, say so explicitly.

## Reporting format (backtest/results/RESULTS.md)

For every run: date range, params, trades, win rate, expectancy_R, profit
factor, max DD, and a one-paragraph honest verdict. Include losing runs.
The journal of failed configs is as valuable as the winner.

## Multi-strategy rules

- Each strategy passes or fails INDEPENDENTLY against the same bar. Never
  enable a failed strategy because "the portfolio looks better with it".
- Only after each passes alone, run both on TEST and check the combined
  equity curve isn't dominated by overlapping losers in the same weeks.
- Set `enabled: true` in config.yaml only for strategies that passed.
