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

1. **Technical score** from 15-minute Kraken candles: RSI(14), MACD(12,26,9)
   crossovers/momentum, EMA20/EMA50 trend, Bollinger position, rate of change,
   Stochastic RSI turns, and volume-surge confirmation — with trend-following
   components **scaled by ADX** so EMA/MACD signals are discounted in choppy
   markets where they mostly whipsaw. The score is then adjusted up to ±15
   points by trend alignment on higher timeframes (1h 50% / 4h 30% / 1d 20%).
2. **News score**: headlines + descriptions from CoinDesk, Cointelegraph,
   Decrypt, CryptoSlate, and Bitcoin Magazine RSS feeds, scored with a
   bullish/bearish lexicon, near-duplicate-deduped, matched to coins, and
   recency-weighted (8-hour half-life). Severe coin-specific events (hacks,
   bankruptcies, approvals) hit that coin 1.5x harder. Market-wide news bleeds
   into every coin at reduced weight.
3. **Market regime overlay**: BTC 4h/1d trend + market breadth (% of coins
   above their 1h EMA50) classify the market as risk-on / neutral / risk-off.
   Risk-off raises the entry bar by 10 points, halves risk per trade, and cuts
   max positions — exits are never restricted.
4. **Combined signal** (−100…+100): crossing the buy threshold opens a position
   (max 2 new entries per cycle), crossing the sell threshold closes one.
5. **Risk engine**: positions are **equal-risk sized** — each stands to lose
   the same fraction of equity if its initial stop is hit, so volatile coins
   get small positions and calm coins larger ones (capped by a max notional
   fraction). Stops/targets are ATR-adaptive: the stop trails price upward
   chandelier-style (only ever tightens), the target extends while the trend
   holds. A **daily circuit breaker** halts new buys (never exits) if equity
   drops more than the style's daily loss limit, until the next UTC day.
   Per-coin re-entry cooldowns prevent churn; positions can always be closed
   manually from the dashboard.

## Analytics & backtesting

- **Performance panel**: win rate, profit factor, expectancy per trade, max
  drawdown, approximate Sharpe, average hold time, streak, best/worst coins —
  computed from the live trade log (`GET /api/analytics`).
- **Backtester**: replays the exact live strategy code (same indicator,
  bracket, sizing, and trailing functions) over the stored ~30 days of hourly
  candles for the top 40 coins by market cap, from the dashboard or
  `POST /api/backtest {"style": "...", "days": N}`. TA-only — historical news
  isn't available, and the regime overlay/circuit breaker aren't simulated —
  and 1h bars can't see intra-hour stop hits, so treat results as directional,
  not gospel.

## Trading styles

Switchable live from the dashboard header (persisted across restarts):

| | Conservative | Balanced | Aggressive (default) |
|---|---|---|---|
| Entry / exit threshold | +38 / −28 | +30 / −25 | +24 / −18 |
| Max positions (notional cap) | 4 (20%) | 5 (22%) | 8 (25%) |
| Risk per trade | 1.0% | 1.5% | 2.0% |
| Daily loss breaker | −2% | −3% | −4% |
| ATR stop / target mult | 2.5× / 3.5× | 2.0× / 3.0× | 1.8× / 2.8× |
| Max stop distance | −6% | −10% | −10% |
| Re-entry cooldown | 2h | 1h | 30min |
| TA / news weight | 60/40 | 65/35 | 70/30 |

The market regime then overlays on the chosen style: neutral adds +3 to the
entry threshold; risk-off adds +10, halves risk per trade, and reduces max
positions by 2.

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
| `indicators.py` | RSI / MACD / EMA / Bollinger / ADX / StochRSI / volume + composite TA score |
| `news.py` | RSS scanning + lexicon sentiment |
| `strategy.py` | Trading styles, regime overlay, thresholds |
| `regime.py` | Market regime: BTC trend + breadth |
| `trader.py` | Portfolio engine, risk sizing, circuit breaker (SQLite: `cryptopilot.db`) |
| `analytics.py` | Performance statistics from the trade log |
| `backtest.py` | Strategy replay over stored candle history |
| `static/` | Dashboard GUI |

## Notes

- Data sources are free public endpoints (Kraken public API, public RSS); if one is
  unreachable the bot degrades gracefully and shows the error in the header.
- State persists across restarts in `cryptopilot.db` (including trading style and
  paper/live mode); the **Reset** button starts over.
- This is an experimental trading bot and **not financial advice**. Live mode is
  strictly opt-in and guarded — see **Going live** above.
