"""Trading engine with SQLite persistence.

Paper mode (default): trades are simulated bookkeeping only.
Live mode: an exchange.LiveExecutor is attached and every buy/sell is
mirrored as a real Kraken market order BEFORE it is recorded — if the
exchange rejects the order, the local book is not touched. The local book
remains the source of truth for stops/targets either way.

Style-dependent limits (max positions, sizing, thresholds, ATR multipliers,
cooldown) live in strategy.PARAMS so the dashboard can switch profiles at
runtime; only style-independent constants are defined here.

Every method that touches self.db/self.positions/self.cash holds self._lock.
sqlite3's check_same_thread=False only lets multiple threads use the
connection at all — it does NOT make concurrent use safe. Without this lock,
the background trading loop and API-triggered calls (manual close, reset,
mode switch) can interleave statements on the same connection and corrupt
its transaction state, silently losing a sell/buy that appeared to succeed.
The lock is reentrant (RLock) because some methods call other locking
methods internally (e.g. check_exits -> sell -> _save).
"""
import json
import sqlite3
import threading
import time

import strategy

START_CASH = 10_000.0
FEE_RATE = 0.001           # simulated fee; real Kraken taker fees differ (~0.25-0.4%)
MIN_TRADE_CASH = 200.0
MIN_STOP_DIST_PCT = -0.01  # keep at least a small cushion so tiny-ATR coins aren't stopped by noise
MIN_TARGET_PCT = 0.03
MAX_TARGET_PCT = 0.60


def compute_bracket(price: float, atr: float, params: dict) -> tuple[float, float]:
    """Initial stop/target for an entry at `price` — shared by live trading
    and the backtester so both always run identical bracket math."""
    stop = max(price - params["atr_stop_mult"] * atr, price * (1 + params["max_stop_pct"]))
    stop = min(stop, price * (1 + MIN_STOP_DIST_PCT))
    target = max(price + params["atr_target_mult"] * atr, price * (1 + MIN_TARGET_PCT))
    target = min(target, price * (1 + MAX_TARGET_PCT))
    return stop, target


def compute_spend(equity: float, cash: float, price: float, stop: float, params: dict) -> float:
    """Equal-risk position sizing (see buy() for rationale) — shared with the
    backtester."""
    stop_distance = max(price - stop, price * 0.001)
    risk_budget = equity * params["risk_per_trade"]
    spend = (risk_budget / stop_distance) * price
    spend = min(spend, equity * params["position_fraction"], cash)
    if spend < MIN_TRADE_CASH:
        spend = min(MIN_TRADE_CASH, cash)
    return spend


def trail_stop_target(pos: dict, price: float, atr: float, trend_ok: bool, params: dict):
    """Chandelier trailing update applied to a position dict in place —
    shared by live trading and the backtester. Stop only tightens; target
    only extends while the trend holds.

    The stop is MONOTONIC — max(old, new) — never lowered. The 1%-below-price
    ceiling applies only to the candidate, not the held stop: clamping the
    held stop to the falling price would drag it down 1% under the market
    every cycle and the stop-loss could never fire."""
    pos["high"] = max(pos["high"], price)
    floor_stop = pos["entry"] * (1 + params["max_stop_pct"])
    candidate_stop = pos["high"] - params["atr_stop_mult"] * atr
    ceiling_stop = price * (1 + MIN_STOP_DIST_PCT)
    pos["stop"] = max(pos["stop"], min(max(candidate_stop, floor_stop), ceiling_stop))
    if trend_ok:
        candidate_target = price + params["atr_target_mult"] * atr
        max_target = price * (1 + MAX_TARGET_PCT)
        pos["target"] = min(max(pos["target"], candidate_target), max_target)


BREAKEVEN_FLOOR_PCT = 0.004      # breakeven stop sits here: covers round-trip fees + slip
RATCHET_LOCK_FRACTION = 0.5      # once ratchet triggers, lock this share of the peak gain


def assess_position(pos: dict, price: float, atr: float, score: float,
                    params: dict, now: float) -> str | None:
    """Per-cycle position management, shared by live trading and the
    backtester. Mutates the stop in place; returns an exit reason string if
    the position should be closed now, else None.

    1. Trailing (chandelier) stop/target update.
    2. Breakeven promotion — once a trade is up breakeven_trigger, the stop
       moves to entry + fees so a winner can no longer become a loser.
    3. Profit ratchet — once the PEAK gain exceeds ratchet_trigger, the stop
       locks in RATCHET_LOCK_FRACTION of that peak, however wide the ATR is.
    4. Weakness cut — a position down weak_loss_cut whose signal has gone
       half-way to the sell threshold is dead capital with negative
       expectancy; cut it instead of riding it to the max stop.
    5. Time stop — held past max_hold_hours with nothing to show for it and
       no bullish signal: free the capital.
    """
    if atr:
        trail_stop_target(pos, price, atr, score >= 0, params)
    entry = pos["entry"]
    gain = price / entry - 1.0
    peak_gain = pos["high"] / entry - 1.0
    ceiling = price * (1 + MIN_STOP_DIST_PCT)

    if gain >= params["breakeven_trigger"]:
        pos["stop"] = max(pos["stop"], min(entry * (1 + BREAKEVEN_FLOOR_PCT), ceiling))
    if peak_gain >= params["ratchet_trigger"]:
        lock = entry * (1 + RATCHET_LOCK_FRACTION * peak_gain)
        pos["stop"] = max(pos["stop"], min(lock, ceiling))

    if gain <= params["weak_loss_cut"] and score <= params["sell_threshold"] * 0.5:
        return (f"Cut loser: {gain * 100:+.1f}% and signal weakening ({score:+.0f}) — "
                f"not waiting for the max stop")
    held_hours = (now - pos["opened"]) / 3600.0
    if (held_hours >= params["max_hold_hours"] and gain < 0.005
            and score < params["buy_threshold"] * 0.5):
        return (f"Time stop: {held_hours:.0f}h held with {gain * 100:+.1f}% "
                f"and signal only {score:+.0f} — freeing the capital")
    return None


class PaperTrader:
    def __init__(self, db_path: str):
        self._lock = threading.RLock()
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.execute("""CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, symbol TEXT,
            side TEXT, qty REAL, price REAL, value REAL, fee REAL,
            pnl REAL, pnl_pct REAL, reason TEXT)""")
        self.db.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS equity (ts REAL, value REAL)")
        self.db.commit()
        self.cash = START_CASH
        self.positions: dict[str, dict] = {}
        self.last_exit: dict[str, float] = {}  # symbol -> ts of last sell (re-entry cooldown)
        self.executor = None                   # exchange.LiveExecutor when live, None when paper
        self._load()

    # ---------- persistence ----------

    def kv_get(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
            return row[0] if row else default

    def kv_set(self, key: str, value: str):
        with self._lock:
            self.db.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
            self.db.commit()

    def _load(self):
        with self._lock:
            row = self.db.execute("SELECT value FROM kv WHERE key='state'").fetchone()
            if row:
                state = json.loads(row[0])
                self.cash = state["cash"]
                self.positions = state["positions"]
                for pos in self.positions.values():  # backfill if loading a pre-trailing-stop save
                    pos.setdefault("high", pos["entry"])
                    pos.setdefault("stop", pos["entry"] * (1 + strategy.PARAMS["max_stop_pct"]))
                    pos.setdefault("target", pos["entry"] * (1 + MIN_TARGET_PCT))

    def _save(self):
        blob = json.dumps({"cash": self.cash, "positions": self.positions})
        self.kv_set("state", blob)

    def reset(self):
        with self._lock:
            self.db.execute("DELETE FROM trades")
            self.db.execute("DELETE FROM equity")
            self.db.execute("DELETE FROM kv")
            self.db.commit()
            self.cash = START_CASH
            self.positions = {}
            self.last_exit = {}
            self._save()

    # ---------- portfolio math ----------

    def equity(self, prices: dict[str, float]) -> float:
        with self._lock:
            return self.cash + sum(
                p["qty"] * prices.get(sym, p["entry"]) for sym, p in self.positions.items())

    def in_cooldown(self, symbol: str) -> bool:
        with self._lock:
            return time.time() - self.last_exit.get(symbol, 0.0) < strategy.PARAMS["reentry_cooldown"]

    # ---------- trading ----------

    def buy(self, symbol: str, price: float, reason: str, prices: dict[str, float],
            atr: float = 0.0, meta: dict | None = None) -> bool:
        with self._lock:
            params = strategy.PARAMS
            if symbol in self.positions or len(self.positions) >= params["max_positions"]:
                return False
            if self.in_cooldown(symbol):
                return False
            if self.cash < MIN_TRADE_CASH:
                return False

            equity = self.equity(prices)
            stop, target = compute_bracket(price, atr, params)

            # Equal-risk sizing: every position stands to lose the same
            # fraction of equity if its initial stop is hit. Volatile coins
            # (wide stops) get small positions, calm coins get larger ones —
            # capped by position_fraction so one calm coin can't dominate.
            spend = compute_spend(equity, self.cash, price, stop, params)
            fee = spend * FEE_RATE
            qty = (spend - fee) / price

            if self.executor is not None:  # live: place the real order first
                try:
                    qty = self.executor.execute("buy", meta or {}, qty, price)
                except Exception as e:
                    self._log_order_failure(symbol, "BUY", str(e))
                    return False
                fee = qty * price * FEE_RATE
                spend = qty * price + fee

            self.cash -= spend
            self.positions[symbol] = {
                "qty": qty, "entry": price, "opened": time.time(),
                "high": price, "stop": stop, "target": target,
            }
            prefix = "[LIVE] " if self.executor is not None else ""
            self.db.execute(
                "INSERT INTO trades (ts, symbol, side, qty, price, value, fee, pnl, pnl_pct, reason) "
                "VALUES (?,?,?,?,?,?,?,NULL,NULL,?)",
                (time.time(), symbol, "BUY", qty, price, spend, fee, prefix + reason))
            self._save()
            return True

    def sell(self, symbol: str, price: float, reason: str, meta: dict | None = None) -> float | None:
        with self._lock:
            pos = self.positions.get(symbol)
            if not pos:
                return None

            if self.executor is not None:  # live: the real order must succeed before we close the book
                try:
                    self.executor.execute("sell", meta or {}, pos["qty"], price)
                except Exception as e:
                    self._log_order_failure(symbol, "SELL", str(e))
                    return None  # position stays open; exit re-triggers next cycle

            self.positions.pop(symbol)
            gross = pos["qty"] * price
            fee = gross * FEE_RATE
            proceeds = gross - fee
            cost = pos["qty"] * pos["entry"]
            pnl = proceeds - cost
            pnl_pct = (pnl / cost) * 100.0 if cost else 0.0
            self.cash += proceeds
            self.last_exit[symbol] = time.time()
            prefix = "[LIVE] " if self.executor is not None else ""
            self.db.execute(
                "INSERT INTO trades (ts, symbol, side, qty, price, value, fee, pnl, pnl_pct, reason) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (time.time(), symbol, "SELL", pos["qty"], price, gross, fee, pnl, pnl_pct, prefix + reason))
            self._save()
            return pnl

    # ---------- daily circuit breaker ----------

    def circuit_breaker(self, equity: float, max_daily_loss: float) -> dict:
        """Daily drawdown kill switch. Anchors equity at the first check of
        each UTC day; if equity falls more than max_daily_loss below the
        anchor, new BUYS are blocked until the next UTC day. Exits, stops,
        and manual closes are never blocked — the breaker only stops the bot
        from digging the hole deeper."""
        with self._lock:
            today = time.strftime("%Y-%m-%d", time.gmtime())
            raw = self.kv_get("day_anchor")
            anchor = json.loads(raw) if raw else None
            if not anchor or anchor.get("date") != today:
                anchor = {"date": today, "equity": equity}
                self.kv_set("day_anchor", json.dumps(anchor))
            day_start = anchor["equity"] or equity
            drawdown = (equity / day_start - 1.0) if day_start else 0.0

            tripped_day = self.kv_get("breaker_day")
            tripped = tripped_day == today
            if not tripped and drawdown <= -max_daily_loss:
                self.kv_set("breaker_day", today)
                tripped = True
            return {
                "tripped": tripped,
                "day_start_equity": day_start,
                "daily_drawdown_pct": drawdown * 100.0,
                "limit_pct": -max_daily_loss * 100.0,
            }

    def _log_order_failure(self, symbol: str, side: str, error: str):
        self.kv_set("last_order_error", json.dumps(
            {"ts": time.time(), "symbol": symbol, "side": side, "error": error[:300]}))

    def last_order_error(self) -> dict | None:
        raw = self.kv_get("last_order_error")
        return json.loads(raw) if raw else None

    # ---------- adaptive stops / position management ----------

    def manage(self, symbol: str, price: float, atr: float, score: float,
               meta: dict | None = None) -> str | None:
        """Run the per-cycle position-management brain (assess_position) on
        one position: trail the stop/target, promote to breakeven, ratchet in
        profits, and exit weak or stale positions. Returns a human-readable
        action string if it closed the position."""
        with self._lock:
            pos = self.positions.get(symbol)
            if not pos:
                return None
            reason = assess_position(pos, price, atr, score, strategy.PARAMS, time.time())
            if reason:
                pnl = self.sell(symbol, price, reason, meta)
                if pnl is not None:
                    return f"Closed {symbol} at ${price:,.4f}: {reason} (P&L {pnl:+,.2f} USD)"
                return None
            self._save()
            return None

    def check_exits(self, prices: dict[str, float], universe: dict[str, dict] | None = None) -> list[str]:
        """Adaptive stop-loss / take-profit sweep. Returns human-readable actions."""
        with self._lock:
            actions = []
            universe = universe or {}
            for symbol in list(self.positions):
                price = prices.get(symbol)
                if not price:
                    continue
                pos = self.positions[symbol]
                ret = price / pos["entry"] - 1.0
                if price <= pos["stop"]:
                    if self.sell(symbol, price, f"Trailing stop hit at ${pos['stop']:,.4f} ({ret * 100:+.1f}% from entry)",
                                 universe.get(symbol)) is not None:
                        actions.append(f"Stop-loss closed {symbol} at ${price:,.2f} ({ret * 100:+.1f}%)")
                elif price >= pos["target"]:
                    if self.sell(symbol, price, f"Take-profit target hit at ${pos['target']:,.4f} ({ret * 100:+.1f}% from entry)",
                                 universe.get(symbol)) is not None:
                        actions.append(f"Take-profit closed {symbol} at ${price:,.2f} ({ret * 100:+.1f}%)")
            return actions

    # ---------- reporting ----------

    def record_equity(self, prices: dict[str, float]):
        with self._lock:
            self.db.execute("INSERT INTO equity (ts, value) VALUES (?, ?)",
                            (time.time(), self.equity(prices)))
            self.db.commit()

    def equity_history(self, limit: int = 600) -> list[list[float]]:
        with self._lock:
            rows = self.db.execute(
                "SELECT ts, value FROM equity ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
            return [[r[0], r[1]] for r in reversed(rows)]

    def recent_trades(self, limit: int = 60) -> list[dict]:
        with self._lock:
            rows = self.db.execute(
                "SELECT ts, symbol, side, qty, price, value, fee, pnl, pnl_pct, reason "
                "FROM trades ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
            keys = ["ts", "symbol", "side", "qty", "price", "value", "fee", "pnl", "pnl_pct", "reason"]
            return [dict(zip(keys, r)) for r in rows]

    def realized_pnl(self) -> float:
        with self._lock:
            row = self.db.execute("SELECT COALESCE(SUM(pnl),0) FROM trades WHERE side='SELL'").fetchone()
            return row[0]

    def trades_for(self, symbol: str, since_ts: float) -> list[dict]:
        with self._lock:
            rows = self.db.execute(
                "SELECT ts, side, price FROM trades WHERE symbol=? AND ts>=? ORDER BY ts",
                (symbol, since_ts)).fetchall()
            return [{"ts": r[0], "side": r[1], "price": r[2]} for r in rows]
