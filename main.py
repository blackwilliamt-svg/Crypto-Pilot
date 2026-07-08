"""CryptoPilot — crypto trading bot with TA + headline sentiment.

Runs in paper mode by default. Live mode (real Kraken orders) must be armed
explicitly from the dashboard and requires KRAKEN_API_KEY / KRAKEN_API_SECRET
environment variables — see exchange.py for the safety notes.

Run:  python main.py   (serves the dashboard at http://127.0.0.1:8899)
"""
import copy
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import exchange
import indicators
import market
import news
import strategy
from trader import PaperTrader, MIN_TRADE_CASH, START_CASH

ROOT = Path(__file__).parent
CYCLE_SECONDS = 45  # aggressive: re-evaluate faster now that the signal runs on 15m candles
NEWS_REFRESH_SECONDS = 300
UNIVERSE_REFRESH_SECONDS = 6 * 3600
CANDLE_HISTORY_LIMIT = 750  # per store: ~8 days of 15m, ~31 days of 1h, ~4 months of 4h

# The tactical signal runs on 15m candles (refreshed every cycle). The 1h/4h/1d
# stores provide trend context on progressively slower refresh cadences —
# higher timeframes change slowly, so refetching them every 45s cycle would
# multiply Kraken call volume for no benefit.
TIMEFRAME_1H_REFRESH_SECONDS = 300
TIMEFRAME_4H_REFRESH_SECONDS = 900
TIMEFRAME_1D_REFRESH_SECONDS = 3600

@asynccontextmanager
async def _lifespan(app):
    threading.Thread(target=_loop, daemon=True).start()
    yield


app = FastAPI(title="CryptoPilot", lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

LOCK = threading.Lock()
STATE: dict = {"status": "starting", "error": None}
UNIVERSE: dict[str, dict] = {}
CANDLES_15M: dict[str, list[dict]] = {}   # 15m — primary tactical signal
CANDLES: dict[str, list[dict]] = {}       # 1h — trend context + charts + ATR for stops
CANDLES_4H: dict[str, list[dict]] = {}    # 4h — medium-term trend context
CANDLES_1D: dict[str, list[dict]] = {}    # 1d — long-term trend context
HEADLINES: list[dict] = []
NEWS_PATTERNS: dict[str, tuple] = {}
_last_news = 0.0
_last_universe = 0.0
_last_1h = 0.0
_last_4h = 0.0
_last_1d = 0.0
PAUSED = False
MODE = "paper"  # "paper" | "live" — never defaults to live
TRADE_LOCK = threading.Lock()  # guards trader mutations (cycle thread vs API endpoints)
TRADER = PaperTrader(str(ROOT / "cryptopilot.db"))

# Restore persisted style; restore live mode only if credentials still present.
strategy.set_style(TRADER.kv_get("style", "aggressive"))
if TRADER.kv_get("mode") == "live":
    if exchange.credentials_present():
        try:
            TRADER.executor = exchange.LiveExecutor()
            MODE = "live"
        except Exception:
            pass
    if MODE != "live":
        TRADER.kv_set("mode", "paper")  # keys gone — fail safe back to paper


def _refresh_universe():
    global UNIVERSE, NEWS_PATTERNS, _last_universe
    fresh = market.discover_universe()
    if fresh:
        UNIVERSE = fresh
        NEWS_PATTERNS = news.build_coin_patterns(UNIVERSE)
        for store in (CANDLES_15M, CANDLES, CANDLES_4H, CANDLES_1D):
            for sym in list(store):
                if sym not in UNIVERSE:
                    del store[sym]
    _last_universe = time.time()


def _refresh_candle_store(store: dict[str, list[dict]], interval: int):
    """Incremental refresh: fetch only candles newer than what's already held,
    for every coin in the universe, at the given Kraken interval (minutes)."""
    for sym, meta in UNIVERSE.items():
        existing = store.get(sym, [])
        since = existing[-1]["t"] if existing else None
        try:
            new_rows = market.fetch_ohlc(meta["pair"], interval=interval, since=since)
        except Exception:
            new_rows = []
        if new_rows:
            if existing:
                existing_ts = {c["t"] for c in existing}
                merged = existing + [r for r in new_rows if r["t"] not in existing_ts]
            else:
                merged = new_rows
            store[sym] = merged[-CANDLE_HISTORY_LIMIT:]
        time.sleep(0.25)  # stay well under Kraken's public rate limit


def _refresh_candles():
    _refresh_candle_store(CANDLES_15M, 15)
    if not CANDLES_15M:
        raise RuntimeError("Could not fetch market data from Kraken")


def _refresh_news():
    global HEADLINES, _last_news
    fetched = news.fetch_headlines(NEWS_PATTERNS)
    if fetched:
        HEADLINES = fetched
    _last_news = time.time()


def _build_summary(signals, actions, prices, headlines):
    lines = []
    avg = sum(s["total"] for s in signals) / len(signals)
    bias = "bullish" if avg >= 12 else ("bearish" if avg <= -12 else "mixed")
    lines.append(f"Market bias: {bias} (average combined signal {avg:+.0f} across {len(signals)} coins).")

    best = max(signals, key=lambda s: s["total"])
    worst = min(signals, key=lambda s: s["total"])
    if best["total"] > 15:
        why = "; ".join(best["reasons"][:2]) or "multiple mild positives"
        lines.append(f"Strongest setup: {best['symbol']} {best['total']:+.0f} — {why}.")
    if worst["total"] < -15:
        why = "; ".join(worst["reasons"][:2]) or "multiple mild negatives"
        lines.append(f"Weakest: {worst['symbol']} {worst['total']:+.0f} — {why}.")

    day_ago = time.time() - 86400
    recent = [h for h in headlines if h["ts"] >= day_ago]
    bull = sum(1 for h in recent if h["label"] == "bullish")
    bear = sum(1 for h in recent if h["label"] == "bearish")
    lines.append(f"News flow: {len(recent)} headlines in 24h — {bull} bullish, {bear} bearish.")
    mover = max(recent, key=lambda h: abs(h["sentiment"]), default=None)
    if mover and abs(mover["sentiment"]) > 30:
        lines.append(f"Biggest headline: “{mover['title']}” ({mover['label']}).")

    lines.extend(actions if actions else ["No trades this cycle — no signal crossed the entry/exit thresholds."])

    n_pos = len(TRADER.positions)
    if n_pos:
        unreal = sum(
            p["qty"] * (prices.get(s, p["entry"]) - p["entry"]) for s, p in TRADER.positions.items())
        lines.append(f"Holding {n_pos} position(s); unrealized P&L {unreal:+,.2f} USD.")
    return {"generated_at": time.time(), "bias": bias, "bias_score": avg, "lines": lines}


def _cycle():
    global STATE, _last_1h, _last_4h, _last_1d
    if not UNIVERSE or time.time() - _last_universe > UNIVERSE_REFRESH_SECONDS:
        _refresh_universe()
    _refresh_candles()
    if time.time() - _last_1h > TIMEFRAME_1H_REFRESH_SECONDS:
        _refresh_candle_store(CANDLES, 60)
        _last_1h = time.time()
    if time.time() - _last_4h > TIMEFRAME_4H_REFRESH_SECONDS:
        _refresh_candle_store(CANDLES_4H, 240)
        _last_4h = time.time()
    if time.time() - _last_1d > TIMEFRAME_1D_REFRESH_SECONDS:
        _refresh_candle_store(CANDLES_1D, 1440)
        _last_1d = time.time()
    if time.time() - _last_news > NEWS_REFRESH_SECONDS:
        _refresh_news()

    headlines = HEADLINES
    prices, atrs, signals = {}, {}, []
    for sym, meta in UNIVERSE.items():
        candles = CANDLES_15M.get(sym)
        if not candles or len(candles) < 60:
            continue
        ta = indicators.analyze(candles, bars_per_hour=4)
        # Stops/targets are sized off hourly ATR — structural volatility —
        # not the noisy 15m bars the tactical signal runs on.
        atrs[sym] = indicators.atr(CANDLES.get(sym, []))

        # Trend context from 1h/4h/1d candles: confirms or penalizes the
        # primary 15m signal rather than standing alone.
        bias_1h = indicators.trend_bias(CANDLES.get(sym, []))
        bias_4h = indicators.trend_bias(CANDLES_4H.get(sym, []))
        bias_1d = indicators.trend_bias(CANDLES_1D.get(sym, []))
        higher_tf_bias = 0.5 * bias_1h + 0.3 * bias_4h + 0.2 * bias_1d
        alignment_adj = higher_tf_bias * 15.0
        ta_score = max(-100.0, min(100.0, ta["score"] + alignment_adj))

        sent = news.coin_sentiment(headlines, sym)
        total = strategy.combine(ta_score, sent["score"])
        reasons = list(ta["reasons"])
        if abs(alignment_adj) >= 5:
            direction = "supportive" if alignment_adj > 0 else "opposing"
            reasons.append(f"1h/4h/1d trend {direction} ({alignment_adj:+.0f})")
        if sent["top_headline"]:
            reasons.append(f"Headline: “{sent['top_headline'][:90]}”")
        prices[sym] = ta["price"]
        signals.append({
            "symbol": sym, "name": meta["name"], "price": ta["price"],
            "change24h": ta["change24h"], "rsi": ta["rsi"], "ta": ta_score,
            "news": sent["score"], "news_count": sent["count"], "total": total,
            "label": strategy.classify(total), "reasons": reasons, "action": "",
        })
    if not signals:
        raise RuntimeError("No coins could be analyzed")
    signals.sort(key=lambda s: s["total"], reverse=True)

    actions = []
    if not PAUSED:
        with TRADE_LOCK:
            for s in signals:  # adapt each open position's stop/target to latest price + volatility
                sym = s["symbol"]
                if sym in TRADER.positions:
                    TRADER.update_trailing(sym, s["price"], atrs.get(sym, 0.0), trend_ok=s["total"] >= 0)

            actions.extend(TRADER.check_exits(prices, UNIVERSE))

            for s in signals:  # signal-driven exits
                sym = s["symbol"]
                if sym in TRADER.positions and s["total"] <= strategy.PARAMS["sell_threshold"]:
                    reason = f"Signal turned bearish ({s['total']:+.0f}): " + "; ".join(s["reasons"][:2])
                    pnl = TRADER.sell(sym, s["price"], reason[:200], UNIVERSE.get(sym))
                    if pnl is not None:
                        actions.append(f"Sold {sym} at ${s['price']:,.2f} on bearish signal (P&L {pnl:+,.2f} USD).")

            for s in signals:  # entries, best score first
                sym = s["symbol"]
                if s["total"] < strategy.PARAMS["buy_threshold"] or sym in TRADER.positions:
                    continue
                reason = (f"Score {s['total']:+.0f} (TA {s['ta']:+.0f} / news {s['news']:+.0f}): "
                          + "; ".join(s["reasons"][:3]))
                if TRADER.buy(sym, s["price"], reason[:220], prices, atrs.get(sym, 0.0), UNIVERSE.get(sym)):
                    actions.append(f"Opened {sym} at ${s['price']:,.2f} (signal {s['total']:+.0f}).")

    for s in signals:
        sym = s["symbol"]
        pos = TRADER.positions.get(sym)
        if pos:
            ret = (s["price"] / pos["entry"] - 1.0) * 100.0
            s["action"] = f"Holding since ${pos['entry']:,.2f} ({ret:+.1f}%)"
        elif s["total"] >= strategy.PARAMS["buy_threshold"]:
            if len(TRADER.positions) >= strategy.PARAMS["max_positions"]:
                s["action"] = "Buy signal — blocked (max positions)"
            elif TRADER.cash < MIN_TRADE_CASH:
                s["action"] = "Buy signal — blocked (low cash)"
            elif TRADER.in_cooldown(sym):
                s["action"] = "Buy signal — cooling down after recent exit"
            else:
                s["action"] = "Buy signal"
        elif s["total"] >= 10:
            s["action"] = "Watching for entry"
        else:
            s["action"] = "Monitoring"

    TRADER.record_equity(prices)
    equity = TRADER.equity(prices)
    realized = TRADER.realized_pnl()
    unrealized = sum(
        p["qty"] * (prices.get(sym, p["entry"]) - p["entry"]) for sym, p in TRADER.positions.items())
    positions = []
    for sym, p in TRADER.positions.items():
        price = prices.get(sym, p["entry"])
        value = p["qty"] * price
        pnl = p["qty"] * (price - p["entry"])
        positions.append({
            "symbol": sym, "qty": p["qty"], "entry": p["entry"], "price": price,
            "value": value, "pnl": pnl,
            "pnl_pct": (price / p["entry"] - 1.0) * 100.0,
            "opened": p["opened"],
            "stop": p["stop"], "target": p["target"],
        })

    snapshot = {
        "status": "paused" if PAUSED else "live",
        "error": None,
        "updated_at": time.time(),
        "paused": PAUSED,
        "mode": MODE,
        "style": strategy.ACTIVE_STYLE,
        "live_ready": exchange.credentials_present(),
        "last_order_error": TRADER.last_order_error(),
        "portfolio": {
            "equity": equity, "cash": TRADER.cash, "start_cash": START_CASH,
            "positions_value": equity - TRADER.cash,
            "realized_pnl": realized, "unrealized_pnl": unrealized,
            "total_pnl": equity - START_CASH,
            "total_pnl_pct": (equity / START_CASH - 1.0) * 100.0,
            "equity_history": TRADER.equity_history(),
        },
        "positions": positions,
        "signals": signals,
        "trades": TRADER.recent_trades(),
        "headlines": [
            {"title": h["title"], "link": h["link"], "source": h["source"], "ts": h["ts"],
             "sentiment": h["sentiment"], "label": h["label"], "coins": h["coins"]}
            for h in headlines[:40]
        ],
        "summary": _build_summary(signals, actions, prices, headlines),
    }
    with LOCK:
        STATE = snapshot


def _loop():
    while True:
        try:
            _cycle()
        except Exception as e:
            with LOCK:
                STATE["status"] = "degraded"
                STATE["error"] = str(e)
        time.sleep(CYCLE_SECONDS)


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/state")
def api_state():
    with LOCK:
        return copy.deepcopy(STATE)


@app.get("/api/coin/{symbol}")
def api_coin(symbol: str):
    symbol = symbol.upper()
    candles = CANDLES.get(symbol)
    if not candles:
        raise HTTPException(404, "unknown or not-yet-loaded symbol")
    window = candles[-168:]  # last 7 days of hourly candles
    closes = [c["c"] for c in candles]
    e20 = indicators.ema_series(closes, 20)[-168:]
    e50 = indicators.ema_series(closes, 50)[-168:]
    upper, lower = indicators.rolling_bollinger(closes)
    return {
        "symbol": symbol,
        "name": UNIVERSE.get(symbol, {}).get("name", symbol),
        "candles": window,
        "ema20": e20,
        "ema50": e50,
        "bb_upper": upper[-168:],
        "bb_lower": lower[-168:],
        "trades": TRADER.trades_for(symbol, window[0]["t"]),
    }


@app.post("/api/bot/toggle")
def api_toggle():
    global PAUSED
    PAUSED = not PAUSED
    with LOCK:
        STATE["paused"] = PAUSED
        if STATE.get("status") in ("live", "paused"):
            STATE["status"] = "paused" if PAUSED else "live"
    return {"paused": PAUSED}


@app.post("/api/reset")
def api_reset():
    with TRADE_LOCK:
        TRADER.reset()
    return {"ok": True}


@app.post("/api/position/{symbol}/close")
def api_close_position(symbol: str):
    symbol = symbol.upper()
    with TRADE_LOCK:
        if symbol not in TRADER.positions:
            raise HTTPException(404, "no open position for that symbol")
        candles = CANDLES_15M.get(symbol)
        price = candles[-1]["c"] if candles else TRADER.positions[symbol]["entry"]
        pnl = TRADER.sell(symbol, price, "Manual close from dashboard", UNIVERSE.get(symbol))
    if pnl is None:
        err = TRADER.last_order_error() or {}
        raise HTTPException(502, f"exchange order failed: {err.get('error', 'unknown error')}")
    return {"ok": True, "symbol": symbol, "price": price, "pnl": pnl}


@app.post("/api/style")
def api_style(payload: dict = Body(...)):
    style = str(payload.get("style", "")).lower()
    if not strategy.set_style(style):
        raise HTTPException(400, f"unknown style; pick one of {list(strategy.STYLES)}")
    TRADER.kv_set("style", style)
    return {"ok": True, "style": style, "params": strategy.PARAMS}


@app.post("/api/mode")
def api_mode(payload: dict = Body(...)):
    """Switch paper <-> live. Live requires: API keys in the environment, no
    open positions (close them first so no position straddles the boundary),
    and the literal confirmation phrase typed by the user."""
    global MODE
    target = str(payload.get("mode", "")).lower()
    if target not in ("paper", "live"):
        raise HTTPException(400, "mode must be 'paper' or 'live'")
    if target == MODE:
        return {"ok": True, "mode": MODE}

    with TRADE_LOCK:
        if TRADER.positions:
            raise HTTPException(409, "close all open positions before switching modes")
        if target == "live":
            if payload.get("confirm") != "GO LIVE":
                raise HTTPException(400, "confirmation phrase mismatch — type GO LIVE to confirm")
            if not exchange.credentials_present():
                raise HTTPException(400, "KRAKEN_API_KEY / KRAKEN_API_SECRET not set in the environment")
            try:
                executor = exchange.LiveExecutor()
                balance = executor.api.usd_balance()
            except Exception as e:
                raise HTTPException(502, f"could not reach Kraken private API: {e}")
            bankroll = min(balance, float(os.environ.get("LIVE_BANKROLL_USD", balance or 0)))
            if bankroll < MIN_TRADE_CASH:
                raise HTTPException(400,
                    f"available USD balance ${balance:,.2f} is below the ${MIN_TRADE_CASH:,.0f} minimum trade size")
            TRADER.executor = executor
            TRADER.cash = bankroll
            TRADER._save()
            MODE = "live"
        else:
            TRADER.executor = None
            MODE = "paper"
        TRADER.kv_set("mode", MODE)
    return {"ok": True, "mode": MODE, "cash": TRADER.cash}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8899)
