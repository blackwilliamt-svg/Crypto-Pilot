# CryptoPilot — Crypto Trading Bot

An automated crypto trading bot that combines **technical analysis** with **news headline
sentiment** to decide trades. By default it runs against a **simulated $10,000 paper
portfolio** — no exchange account, API keys, or real money involved. An optional live
mode can mirror trades as real Kraken orders (see **Going live** below).

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
3. **Combined signal** (−100…+100): crossing the buy threshold opens a position,
   crossing the sell threshold closes one. Stops and targets are adaptive: sized
   off each coin's hourly ATR, with the stop trailing price upward
   chandelier-style (only ever tightens) and the target extending while the
   trend holds. A per-coin re-entry cooldown prevents fast-signal churn.
   Positions can also be closed manually from the dashboard at any time.

## Trading styles

Switchable live from the dashboard header (persisted across restarts):

| | Conservative | Balanced | Aggressive (default) |
|---|---|---|---|
| Entry / exit threshold | +38 / −28 | +30 / −25 | +24 / −18 |
| Max positions × size | 4 × 15% | 5 × 18% | 8 × 12% |
| ATR stop / target mult | 2.5× / 3.5× | 2.0× / 3.0× | 1.8× / 2.8× |
| Max risk per position | −6% | −10% | −10% |
| Re-entry cooldown | 2h | 1h | 30min |
| TA / news weight | 60/40 | 65/35 | 70/30 |

## Going live (real orders — read this first)

Live mode mirrors every bot decision as a **real Kraken market order with real
money**. It is off by default and cannot turn itself on.

1. Create a Kraken API key with **only** "Query funds" and "Create & modify
   orders" permissions. Never grant withdrawal rights to a bot key.
2. Set environment variables before starting the bot (never commit these):
   - `KRAKEN_API_KEY` / `KRAKEN_API_SECRET` — the credentials
   - `LIVE_MAX_ORDER_USD` — hard cap per buy order (default 100)
   - `LIVE_BANKROLL_USD` — cap on how much of the account the bot manages
     (default: full available USD balance)
3. Close any open paper positions, then click **Go Live** in the dashboard and
   type the confirmation phrase.

Safety behavior: buys are capped at `LIVE_MAX_ORDER_USD`; **sells are never
capped** (exits always fully close); if an order is rejected by Kraken the
local book is untouched and the error is shown on the dashboard; if credentials
disappear the bot falls back to paper on restart; live trades are tagged
`[LIVE]` in the trade log. The bot's simulated 0.1% fee understates Kraken's
real taker fees (~0.25–0.4%), so live P&L will run slightly worse than paper.
**This is experimental software — do not give it money you can't afford to lose.**

Every trade is logged with the reasoning behind it, and the dashboard's **Bot Findings**
panel summarizes the current market read, strongest/weakest setups, news flow, and
actions taken each cycle.

## Files

| File | Purpose |
|---|---|
| `main.py` | FastAPI app, bot loop, REST API |
| `market.py` | Kraken public OHLC client + CoinGecko universe discovery |
| `exchange.py` | Kraken private API client + live order executor |
| `indicators.py` | RSI / MACD / EMA / Bollinger + composite TA score |
| `news.py` | RSS scanning + lexicon sentiment |
| `strategy.py` | Score weighting and signal thresholds |
| `trader.py` | Paper portfolio engine (SQLite: `cryptopilot.db`) |
| `static/` | Dashboard GUI |

## Notes

- Data sources are free public endpoints (Kraken public API, public RSS); if one is
  unreachable the bot degrades gracefully and shows the error in the header.
- State persists across restarts in `cryptopilot.db` (including trading style and
  paper/live mode); the **Reset** button starts over.
- This is an experimental trading bot and **not financial advice**. Live mode is
  strictly opt-in and guarded — see **Going live** above.
