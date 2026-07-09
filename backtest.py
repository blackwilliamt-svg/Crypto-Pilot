"""Backtester: replay the live strategy over the candle history already in
memory, using the SAME analyze/bracket/sizing/trailing code paths as live
trading (imported from indicators/trader/strategy — not reimplemented).

Honest limitations, stated in the result payload too:
  * TA-only. Historical headlines aren't available, so the news component is
    zero — combined score = ta_weight × TA score, which matches live behavior
    for coins with quiet news.
  * The market-regime overlay and daily circuit breaker are not simulated.
  * Runs on 1h candles (the deepest history we hold: ~31 days), so it can't
    see intra-hour stop hits the live 15m loop would catch.

Runs in a background thread; progress and results are polled via the module
JOB dict. Never touches live trading state (styles are read as plain dicts,
strategy globals are untouched).
"""
import bisect
import threading
import time

import analytics
import indicators
import strategy
from trader import FEE_RATE, START_CASH, compute_bracket, compute_spend, trail_stop_target

WARMUP_BARS = 200      # bars needed before the first tradable signal
MAX_COINS = 40         # top by market cap — keeps a full run under ~a minute

JOB: dict = {"status": "idle", "progress": 0.0, "result": None, "error": None}
_job_lock = threading.Lock()


def _bias_at(candles: list[dict], ts_index: list[int], ts: int) -> float:
    """Higher-timeframe trend bias using only candles closed at/before ts."""
    i = bisect.bisect_right(ts_index, ts)
    return indicators.trend_bias(candles[max(0, i - 60):i])


def _run(candles_1h, candles_4h, candles_1d, universe, style, days):
    params = dict(strategy.STYLES[style])
    ta_weight = params["ta_weight"]
    buy_thr, sell_thr = params["buy_threshold"], params["sell_threshold"]

    # top coins by market cap that have enough history
    coins = [s for s, m in sorted(universe.items(), key=lambda kv: -kv[1].get("market_cap", 0))
             if len(candles_1h.get(s, [])) >= WARMUP_BARS + 24][:MAX_COINS]
    if not coins:
        raise RuntimeError("no coins with enough 1h history to backtest")

    ts_4h = {s: [c["t"] for c in candles_4h.get(s, [])] for s in coins}
    ts_1d = {s: [c["t"] for c in candles_1d.get(s, [])] for s in coins}

    n_bars = min(len(candles_1h[s]) for s in coins)
    steps = min(days * 24, n_bars - WARMUP_BARS)
    if steps < 24:
        raise RuntimeError("not enough history for the requested window")

    cash = START_CASH
    positions: dict[str, dict] = {}
    last_exit: dict[str, float] = {}
    trades: list[dict] = []
    equity_curve: list[list[float]] = []

    def equity(prices):
        return cash + sum(p["qty"] * prices.get(s, p["entry"]) for s, p in positions.items())

    def sell(sym, price, ts, reason):
        nonlocal cash
        pos = positions.pop(sym)
        gross = pos["qty"] * price
        fee = gross * FEE_RATE
        cost = pos["qty"] * pos["entry"]
        pnl = gross - fee - cost
        cash += gross - fee
        last_exit[sym] = ts
        trades.append({"ts": ts, "symbol": sym, "side": "SELL", "qty": pos["qty"],
                       "price": price, "value": gross, "fee": fee, "pnl": pnl,
                       "pnl_pct": (pnl / cost * 100.0) if cost else 0.0, "reason": reason})

    start_offset = n_bars - steps
    btc_start = None

    for step in range(steps):
        idx = start_offset + step
        prices, scores, atrs = {}, {}, {}
        ts = None
        for sym in coins:
            window = candles_1h[sym][max(0, idx - WARMUP_BARS):idx + 1]
            if len(window) < 60:
                continue
            ts = window[-1]["t"]
            ta = indicators.analyze(window, bars_per_hour=1)
            bias = (0.5 * indicators.trend_bias(window[-220:])
                    + 0.3 * _bias_at(candles_4h.get(sym, []), ts_4h[sym], ts)
                    + 0.2 * _bias_at(candles_1d.get(sym, []), ts_1d[sym], ts))
            ta_score = max(-100.0, min(100.0, ta["score"] + bias * 15.0))
            prices[sym] = ta["price"]
            scores[sym] = ta_weight * ta_score  # news component = 0 (see module docstring)
            atrs[sym] = indicators.atr(window)
        if ts is None:
            continue
        if btc_start is None:
            btc_start = prices.get("BTC")

        # trailing updates + bracket exits (same shared code as live)
        for sym in list(positions):
            price = prices.get(sym)
            if not price:
                continue
            trail_stop_target(positions[sym], price, atrs.get(sym, 0.0),
                              scores.get(sym, 0) >= 0, params)
            pos = positions[sym]
            if price <= pos["stop"]:
                sell(sym, price, ts, "trailing stop")
            elif price >= pos["target"]:
                sell(sym, price, ts, "take profit")

        # signal exits
        for sym in list(positions):
            if scores.get(sym, 0) <= sell_thr and sym in prices:
                sell(sym, prices[sym], ts, "bearish signal")

        # entries (best first, throttled like live)
        opened = 0
        for sym, sc in sorted(scores.items(), key=lambda kv: -kv[1]):
            if opened >= 2 or sc < buy_thr or sym in positions:
                continue
            if len(positions) >= params["max_positions"] or cash < 200.0:
                break
            if ts - last_exit.get(sym, -1e12) < params["reentry_cooldown"]:
                continue
            price = prices[sym]
            stop, target = compute_bracket(price, atrs.get(sym, 0.0), params)
            spend = compute_spend(equity(prices), cash, price, stop, params)
            if spend > cash or spend <= 0:
                continue
            fee = spend * FEE_RATE
            qty = (spend - fee) / price
            cash -= spend
            positions[sym] = {"qty": qty, "entry": price, "opened": ts,
                              "high": price, "stop": stop, "target": target}
            trades.append({"ts": ts, "symbol": sym, "side": "BUY", "qty": qty,
                           "price": price, "value": spend, "fee": fee,
                           "pnl": None, "pnl_pct": None, "reason": f"signal {sc:+.0f}"})
            opened += 1

        eq = equity(prices)
        equity_curve.append([ts, eq])
        if step % 10 == 0:
            with _job_lock:
                JOB["progress"] = step / steps

    final_prices = prices
    final_equity = equity(final_prices)
    stats = analytics.compute(trades, equity_curve, START_CASH)
    btc_end = final_prices.get("BTC")
    btc_hold_pct = ((btc_end / btc_start - 1.0) * 100.0) if btc_start and btc_end else None

    # thin the curve for the chart
    stride = max(1, len(equity_curve) // 300)
    return {
        "style": style,
        "days": steps // 24,
        "coins_tested": len(coins),
        "start_cash": START_CASH,
        "final_equity": final_equity,
        "return_pct": (final_equity / START_CASH - 1.0) * 100.0,
        "btc_hold_return_pct": btc_hold_pct,
        "open_positions_at_end": len(positions),
        "stats": stats,
        "equity_curve": equity_curve[::stride],
        "trades": trades[-200:],
        "notes": "TA-only (no historical news); regime overlay and circuit breaker not simulated; 1h bars, so intra-hour stop hits are invisible.",
    }


def start(candles_1h, candles_4h, candles_1d, universe, style: str, days: int) -> bool:
    """Kick off a backtest in a background thread. Snapshots the candle dicts
    so the live refresh loop can't mutate data mid-run. Returns False if a
    run is already in progress."""
    with _job_lock:
        if JOB["status"] == "running":
            return False
        JOB.update({"status": "running", "progress": 0.0, "result": None, "error": None})

    snap_1h = {k: v[:] for k, v in candles_1h.items()}
    snap_4h = {k: v[:] for k, v in candles_4h.items()}
    snap_1d = {k: v[:] for k, v in candles_1d.items()}
    uni = dict(universe)

    def worker():
        try:
            result = _run(snap_1h, snap_4h, snap_1d, uni, style, days)
            with _job_lock:
                JOB.update({"status": "done", "progress": 1.0, "result": result})
        except Exception as e:
            with _job_lock:
                JOB.update({"status": "error", "error": str(e)})

    threading.Thread(target=worker, daemon=True).start()
    return True


def status() -> dict:
    with _job_lock:
        return dict(JOB)
