# Replacement strategy research audit — 2026-09-06

Reviewed saved local results and the enhanced backtest implementation. No broker calls were made.

| Saved run | Strategy return | SPY return | Strategy max drawdown | SPY max drawdown |
| --- | ---: | ---: | ---: | ---: |
| replacement_momentum20_2020 | 171.48% | 158.73% | -21.70% | -24.51% |
| replacement_momentum20_2022 | 126.70% | 71.34% | -21.70% | -24.51% |
| replacement_momentum20_2022_cost50 | 117.48% | 71.34% | -21.84% | -24.51% |
| replacement_momentum20_2024 | 64.14% | 67.87% | -21.70% | -18.74% |
| replacement_momentum20_2024_cost50 | 60.00% | 67.87% | -21.84% | -18.74% |

## Verification

All five saved curves reproduce their reported ending equity, and daily returns compound back to every equity observation within $0.0000001. These are overlapping historical windows, not independent out-of-sample trials.

## Material modeling finding

`src/wallstreet_ls/backtest.py`, in `run_enhanced_backtest`, applies the same target weight vector to each daily return throughout each monthly holding period. This represents daily restoration of target weights, although transaction costs are charged only on monthly target changes. A portfolio holding shares between monthly rebalances instead experiences weight drift. Consequently, both returns and turnover need a drift-aware rerun before these results can validate monthly execution. The direction and size of the difference have not yet been measured.

The first requested month also remains in cash until its month-end signal, while SPY starts immediately. This is an explicit comparison mismatch to consider when evaluating start-date sensitivity. Closing prices determine signals and also anchor the subsequent close-to-close returns; executable next-session fills are not modeled.

## Interpretation and next research step

The 2024-start base run trails SPY and has a deeper maximum drawdown. Raising costs does not establish robustness to execution assumptions or static-universe survivorship bias. Preserve these artifacts as historical outputs; next implement and test share-holding/weight-drift accounting in the research backtest, then regenerate comparable results using the same input prices. No conclusion about current account performance follows from these simulations.
