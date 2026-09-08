# Wall Street Long/Short with Alpaca

This project translates the crypto long/short concept into a liquid U.S. equity
strategy. It ranks a fixed large-cap universe using **12-1 month momentum**, buys
the leaders, shorts the laggards, inverse-volatility weights both books, caps each
position, and volatility-scales the portfolio. It is a research framework, not
investment advice or a promise of returns.

The default paper capital base is **$2,200**.
Only 60% gross exposure is allocated initially, leaving a buffer because Alpaca
requires at least $2,000 account equity for short selling. If equity falls below
that threshold, the program refuses to open a long/short rebalance.

## Safety model

- Dry-run and paper trading are the defaults.
- Shorts must be `shortable` and `easy_to_borrow` according to Alpaca.
- Every stock must pass price and 20-day dollar-volume thresholds.
- Gross exposure is capped at 60%, while per-name weights and volatility are capped.
- Live execution requires both `ALPACA_PAPER=false` and the explicit environment
  value `ALLOW_LIVE_TRADING=I_UNDERSTAND_THE_RISK`.

## Setup and run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[test]'
cp .env.example .env
# Put separate Alpaca PAPER credentials in .env
pytest
wallstreet-ls                    # data + proposed trade_plan.csv only
wallstreet-ls --submit           # submit to the PAPER account
wallstreet-ls-backtest --mode enhanced --sentiment-events sentiment_events.csv
wallstreet-ls-backtest --mode video-proxy --output video_proxy_results
wallstreet-ls-scalper-backtest --strategy sneaky-pivot --symbol SPY --output sneaky_pivot_results
# Refresh the dashboard's published original-model metrics and equity curve.
wallstreet-ls-backtest --output backtest_results --dashboard-public dashboard/public
# Publish the current Alpaca account and filled positions for the dashboard (read-only).
wallstreet-ls-dashboard-sync
```

The optional sentiment file must be point-in-time data with columns
`timestamp,symbol,source,sentiment,credibility,novelty`. `source` is either
`news` or `social`; numeric scores range from -1 to 1 for sentiment and 0 to 1
for credibility and novelty. Future-dated events are rejected at every
rebalance, news decays over roughly three days, and social sentiment decays
within a day. Social data contributes only 5% of the active-sleeve score and
cannot place orders directly.

The `video-proxy` mode is an explicit approximation for the Mark Tilbury
investing video: 80% SPY plus a monthly 20% sleeve of ten liquid large-cap
names ranked by momentum and lower volatility. The video itself does not give
an executable algorithm, so this result is not evidence of the creator's
personal returns; quality, valuation, taxes, and contributions are not modeled.

Live credentials may be kept in `.env.local`; that file is ignored by Git and is
intentionally not loaded by the normal paper-trading command. Do not copy live
keys into `.env` during paper testing. Promoting this strategy to live execution
should be a separate, explicit change after reviewing paper results, slippage,
borrow failures, and rebalance behavior.

Run after the U.S. close on a weekly or monthly schedule. Review `trade_plan.csv`
before submission. The included universe is deliberately static and should be
revisited without survivorship bias before interpreting any backtest.

## Signal and portfolio rules

1. Require at least 127 adjusted daily observations and $20M average dollar volume.
2. Rank return from 126 trading days ago through 21 trading days ago.
3. Long the top five and short the bottom five.
4. Give lower-volatility names more weight, with 10% maximum per stock.
5. Start with 50% long / 50% short and scale down if estimated annual volatility
   exceeds 12%.

The IEX feed is selected so the example works with Alpaca's common entry-level
data setup. Market orders can experience slippage; a production system should add
limit-price logic, fill reconciliation, borrow monitoring, and a persistent audit
log.
