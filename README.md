# CryptoPilot — Paper-Trading Crypto Bot

An automated crypto trading bot that combines **technical analysis** with **news headline
sentiment** to decide trades, executed against a **simulated $10,000 paper portfolio**.
No exchange account, API keys, or real money involved.

## Run it

```
pip install fastapi uvicorn requests
python main.py
```

Then open **http://127.0.0.1:8899**. The first cycle takes a bit longer than usual
since it discovers the tradable coin universe and pulls full candle history for
each coin; after that the bot re-evaluates every 75 seconds with cheap incremental
candle fetches.

## How it decides

**Coin universe** (refreshed every 6 hours): every coin with market cap over
**$100M**, excluding stablecoins, that's also spot-tradable against USD on Kraken
(our only price/candle source — CoinGecko provides market cap + stablecoin data,
Kraken provides prices, so the tradable set is their intersection, capped at 150
coins by market cap). Typically ~100-150 coins.

Each cycle (every 45 seconds), for every coin in the universe:

1. **Technical score (70% weight)** from 15-minute Kraken candles:
   RSI(14) overbought/oversold, MACD(12,26,9) crossovers and momentum,
   EMA20/EMA50 trend, Bollinger band position, 2.5-hour rate of change —
   then adjusted up to ±15 points by trend alignment on the higher
   timeframes (1h 50% / 4h 30% / 1d 20%), so trades with the larger trend
   get boosted and counter-trend setups get penalized.
2. **News score (30% weight)**: headlines from CoinDesk, Cointelegraph, Decrypt,
   CryptoSlate, and Bitcoin Magazine RSS feeds are scored with a bullish/bearish
   keyword lexicon, matched to coins, and recency-weighted (8-hour half-life).
   Market-wide news bleeds into every coin at reduced weight.
3. **Combined signal** (−100…+100): ≥ +24 opens a position (max 8, ~12% of equity
   each), ≤ −18 closes one. Stops and targets are adaptive: sized off each coin's
   hourly ATR (stop 1.8×, target 2.8×, max risk −10%), with the stop trailing
   price upward chandelier-style (only ever tightens) and the target extending
   while the trend holds. A 30-minute re-entry cooldown per coin prevents
   fast-signal churn.

Every trade is logged with the reasoning behind it, and the dashboard's **Bot Findings**
panel summarizes the current market read, strongest/weakest setups, news flow, and
actions taken each cycle.

## Files

| File | Purpose |
|---|---|
| `main.py` | FastAPI app, bot loop, REST API |
| `market.py` | Kraken public OHLC client + CoinGecko universe discovery |
| `indicators.py` | RSI / MACD / EMA / Bollinger + composite TA score |
| `news.py` | RSS scanning + lexicon sentiment |
| `strategy.py` | Score weighting and signal thresholds |
| `trader.py` | Paper portfolio engine (SQLite: `cryptopilot.db`) |
| `static/` | Dashboard GUI |

## Notes

- Data sources are free public endpoints (Kraken public API, public RSS); if one is
  unreachable the bot degrades gracefully and shows the error in the header.
- State persists across restarts in `cryptopilot.db`; the **Reset** button starts over.
- This is an experimental paper trader, **not financial advice**, and deliberately has
  no live-trading capability wired in.
