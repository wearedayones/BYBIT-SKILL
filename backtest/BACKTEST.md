# Backtest & Tuning Protocol

This is the procedure for validating and adjusting the pre-filter BEFORE the
system trades. An AI agent (any supported backend — see `config.yaml → agent`)
follows this automatically from RUNBOOK.md. Follow it exactly — the failure
mode here is curve-fitting, which produces beautiful backtests and dead accounts.

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
   - Note the per-level-kind breakdown: if all profit comes from one level
     kind, say so explicitly.

## Institutional validation (required before enabling at scale)

7. **Walk-forward validation** — proves the edge persists across time, not
   just one lucky train/test cut:
   ```bash
   python backtest/walk_forward.py --csv backtest/data/BTCUSDT_15m.csv \
       --strategy <name> --train-months 9 --test-months 3 --step-months 1 \
       [same params chosen in step 4]
   ```
   Pass bar: >= 60% of out-of-sample folds positive, combined expectancy > 0,
   combined max drawdown < 20R. FAIL = do not enable, regardless of single-window results.

8. **Parameter sensitivity** — confirms chosen params are on a flat plateau,
   not a fragile local maximum:
   ```bash
   python backtest/sensitivity.py --csv backtest/data/BTCUSDT_15m.csv \
       --strategy <name> --start <TRAIN_START> --end <TRAIN_END> \
       --params <param1>=<val>,<param2>=<val> --bracket fixed_rr=<val>
   ```
   Any FRAGILE flag = reconsider the param combination before proceeding.

9. **Multi-symbol required** (not optional at institutional level):
   Repeat steps 3–8 for ETHUSDT with the SAME params, no re-tuning.
   A strategy with an edge only on BTCUSDT is likely fitted to BTCUSDT noise.
   Pass bar: expectancy_R > 0 on both BTCUSDT and ETHUSDT.

10. **Monte Carlo stress test** on the final test window:
    ```bash
    python backtest/backtest.py --csv backtest/data/BTCUSDT_15m.csv \
        --strategy <name> --start <TEST_START> --end <TEST_END> --monte-carlo 2000
    ```
    Pass bar: `ruin_pct < 5%` (probability of cumulative R ever reaching -20R).

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
