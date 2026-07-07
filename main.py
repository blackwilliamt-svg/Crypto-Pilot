"""CryptoPilot — paper-trading crypto bot with TA + headline sentiment.

Run:  python main.py   (serves the dashboard at http://127.0.0.1:8899)
"""
import copy
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import indicators
import market
import news
import strategy
from trader import PaperTrader, STOP_LOSS, TAKE_PROFIT, MAX_POSITIONS, MIN_TRADE_CASH, START_CASH

ROOT = Path(__file__).parent
CYCLE_SECONDS = 75
NEWS_REFRESH_SECONDS = 300

@asynccontextmanager
async def _lifespan(app):
    threading.Thread(target=_loop, daemon=True).start()
    yield


app = FastAPI(title="CryptoPilot", lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

LOCK = threading.Lock()
STATE: dict = {"status": "starting", "error": None}
CANDLES: dict[str, list[dict]] = {}
HEADLINES: list[dict] = []
_last_news = 0.0
PAUSED = False
TRADER = PaperTrader(str(ROOT / "cryptopilot.db"))


def _refresh_candles():
    for sym, meta in market.COINS.items():
        try:
            CANDLES[sym] = market.fetch_ohlc(meta["pair"])
        except Exception:
            pass  # keep previous candles for this coin
        time.sleep(0.3)  # stay well under Kraken's public rate limit
    if not CANDLES:
        raise RuntimeError("Could not fetch market data from Kraken")


def _refresh_news():
    global HEADLINES, _last_news
    fetched = news.fetch_headlines()
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
    global STATE
    _refresh_candles()
    if time.time() - _last_news > NEWS_REFRESH_SECONDS:
        _refresh_news()

    headlines = HEADLINES
    prices, signals = {}, []
    for sym, meta in market.COINS.items():
        candles = CANDLES.get(sym)
        if not candles or len(candles) < 60:
            continue
        ta = indicators.analyze(candles)
        sent = news.coin_sentiment(headlines, sym)
        total = strategy.combine(ta["score"], sent["score"])
        reasons = list(ta["reasons"])
        if sent["top_headline"]:
            reasons.append(f"Headline: “{sent['top_headline'][:90]}”")
        prices[sym] = ta["price"]
        signals.append({
            "symbol": sym, "name": meta["name"], "price": ta["price"],
            "change24h": ta["change24h"], "rsi": ta["rsi"], "ta": ta["score"],
            "news": sent["score"], "news_count": sent["count"], "total": total,
            "label": strategy.classify(total), "reasons": reasons, "action": "",
        })
    if not signals:
        raise RuntimeError("No coins could be analyzed")
    signals.sort(key=lambda s: s["total"], reverse=True)

    actions = []
    if not PAUSED:
        actions.extend(TRADER.check_exits(prices))

        for s in signals:  # signal-driven exits
            sym = s["symbol"]
            if sym in TRADER.positions and s["total"] <= strategy.SELL_THRESHOLD:
                reason = f"Signal turned bearish ({s['total']:+.0f}): " + "; ".join(s["reasons"][:2])
                pnl = TRADER.sell(sym, s["price"], reason[:200])
                actions.append(f"Sold {sym} at ${s['price']:,.2f} on bearish signal (P&L {pnl:+,.2f} USD).")

        for s in signals:  # entries, best score first
            sym = s["symbol"]
            if s["total"] < strategy.BUY_THRESHOLD or sym in TRADER.positions:
                continue
            reason = (f"Score {s['total']:+.0f} (TA {s['ta']:+.0f} / news {s['news']:+.0f}): "
                      + "; ".join(s["reasons"][:3]))
            if TRADER.buy(sym, s["price"], reason[:220], prices):
                actions.append(f"Opened {sym} at ${s['price']:,.2f} (signal {s['total']:+.0f}).")

    for s in signals:
        sym = s["symbol"]
        pos = TRADER.positions.get(sym)
        if pos:
            ret = (s["price"] / pos["entry"] - 1.0) * 100.0
            s["action"] = f"Holding since ${pos['entry']:,.2f} ({ret:+.1f}%)"
        elif s["total"] >= strategy.BUY_THRESHOLD:
            if len(TRADER.positions) >= MAX_POSITIONS:
                s["action"] = "Buy signal — blocked (max positions)"
            elif TRADER.cash < MIN_TRADE_CASH:
                s["action"] = "Buy signal — blocked (low cash)"
            else:
                s["action"] = "Buy signal"
        elif s["total"] >= 12:
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
            "stop": p["entry"] * (1 + STOP_LOSS), "target": p["entry"] * (1 + TAKE_PROFIT),
        })

    snapshot = {
        "status": "paused" if PAUSED else "live",
        "error": None,
        "updated_at": time.time(),
        "paused": PAUSED,
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
        "name": market.COINS[symbol]["name"],
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
    TRADER.reset()
    return {"ok": True}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8899)
