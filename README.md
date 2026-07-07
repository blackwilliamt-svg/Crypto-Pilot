# CryptoPilot — Paper-Trading Crypto Bot

An automated crypto trading bot that combines **technical analysis** with **news headline
sentiment** to decide trades, executed against a **simulated $10,000 paper portfolio**.
No exchange account, API keys, or real money involved.

## Run it

```
pip install fastapi uvicorn requests
python main.py
```

Then open **http://127.0.0.1:8899**. The first cycle takes ~15 seconds (it fetches
candles for 10 coins plus 5 news feeds), after which the bot re-evaluates every 75 seconds.

## How it decides

Each cycle, for 10 major coins (BTC, ETH, SOL, XRP, ADA, DOGE, DOT, LINK, AVAX, LTC):

1. **Technical score (65% weight)** from hourly Kraken candles:
   RSI(14) overbought/oversold, MACD(12,26,9) crossovers and momentum,
   EMA20/EMA50 trend, Bollinger band position, 10-hour rate of change.
2. **News score (35% weight)**: headlines from CoinDesk, Cointelegraph, Decrypt,
   CryptoSlate, and Bitcoin Magazine RSS feeds are scored with a bullish/bearish
   keyword lexicon, matched to coins, and recency-weighted (12-hour half-life).
   Market-wide news bleeds into every coin at reduced weight.
3. **Combined signal** (−100…+100): ≥ +30 opens a position (max 5, ~18% of equity each),
   ≤ −25 closes one. Every position also carries a −5% stop-loss and +12% take-profit.

Every trade is logged with the reasoning behind it, and the dashboard's **Bot Findings**
panel summarizes the current market read, strongest/weakest setups, news flow, and
actions taken each cycle.

## Files

| File | Purpose |
|---|---|
| `main.py` | FastAPI app, bot loop, REST API |
| `market.py` | Kraken public OHLC client |
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
