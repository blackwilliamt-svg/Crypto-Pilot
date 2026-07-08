"""Trading engine with SQLite persistence.

Paper mode (default): trades are simulated bookkeeping only.
Live mode: an exchange.LiveExecutor is attached and every buy/sell is
mirrored as a real Kraken market order BEFORE it is recorded — if the
exchange rejects the order, the local book is not touched. The local book
remains the source of truth for stops/targets either way.

Style-dependent limits (max positions, sizing, thresholds, ATR multipliers,
cooldown) live in strategy.PARAMS so the dashboard can switch profiles at
runtime; only style-independent constants are defined here.
"""
import json
import sqlite3
import time

import strategy

START_CASH = 10_000.0
FEE_RATE = 0.001           # simulated fee; real Kraken taker fees differ (~0.25-0.4%)
MIN_TRADE_CASH = 200.0
MIN_STOP_DIST_PCT = -0.01  # keep at least a small cushion so tiny-ATR coins aren't stopped by noise
MIN_TARGET_PCT = 0.03
MAX_TARGET_PCT = 0.60


class PaperTrader:
    def __init__(self, db_path: str):
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
        row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def kv_set(self, key: str, value: str):
        self.db.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        self.db.commit()

    def _load(self):
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
        return self.cash + sum(
            p["qty"] * prices.get(sym, p["entry"]) for sym, p in self.positions.items())

    def in_cooldown(self, symbol: str) -> bool:
        return time.time() - self.last_exit.get(symbol, 0.0) < strategy.PARAMS["reentry_cooldown"]

    # ---------- trading ----------

    def buy(self, symbol: str, price: float, reason: str, prices: dict[str, float],
            atr: float = 0.0, meta: dict | None = None) -> bool:
        params = strategy.PARAMS
        if symbol in self.positions or len(self.positions) >= params["max_positions"]:
            return False
        if self.in_cooldown(symbol):
            return False
        if self.cash < MIN_TRADE_CASH:
            return False
        spend = min(self.cash, max(MIN_TRADE_CASH, self.equity(prices) * params["position_fraction"]))
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
        max_stop_pct = params["max_stop_pct"]
        stop = max(price - params["atr_stop_mult"] * atr, price * (1 + max_stop_pct))
        stop = min(stop, price * (1 + MIN_STOP_DIST_PCT))
        target = max(price + params["atr_target_mult"] * atr, price * (1 + MIN_TARGET_PCT))
        target = min(target, price * (1 + MAX_TARGET_PCT))
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

    def _log_order_failure(self, symbol: str, side: str, error: str):
        self.kv_set("last_order_error", json.dumps(
            {"ts": time.time(), "symbol": symbol, "side": side, "error": error[:300]}))

    def last_order_error(self) -> dict | None:
        raw = self.kv_get("last_order_error")
        return json.loads(raw) if raw else None

    # ---------- adaptive stops ----------

    def update_trailing(self, symbol: str, price: float, atr: float, trend_ok: bool):
        """Adapt the stop/target to the latest price and volatility. The stop
        only ever tightens (chandelier exit, locks in gains as price rises);
        the target only extends further while `trend_ok` (trend still holding),
        and otherwise freezes rather than pulling back."""
        pos = self.positions.get(symbol)
        if not pos or not atr:
            return
        params = strategy.PARAMS
        pos["high"] = max(pos["high"], price)
        floor_stop = pos["entry"] * (1 + params["max_stop_pct"])
        candidate_stop = pos["high"] - params["atr_stop_mult"] * atr
        ceiling_stop = price * (1 + MIN_STOP_DIST_PCT)
        pos["stop"] = min(max(pos["stop"], candidate_stop, floor_stop), ceiling_stop)
        if trend_ok:
            candidate_target = price + params["atr_target_mult"] * atr
            max_target = price * (1 + MAX_TARGET_PCT)
            pos["target"] = min(max(pos["target"], candidate_target), max_target)
        self._save()

    def check_exits(self, prices: dict[str, float], universe: dict[str, dict] | None = None) -> list[str]:
        """Adaptive stop-loss / take-profit sweep. Returns human-readable actions."""
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
        self.db.execute("INSERT INTO equity (ts, value) VALUES (?, ?)",
                        (time.time(), self.equity(prices)))
        self.db.commit()

    def equity_history(self, limit: int = 600) -> list[list[float]]:
        rows = self.db.execute(
            "SELECT ts, value FROM equity ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [[r[0], r[1]] for r in reversed(rows)]

    def recent_trades(self, limit: int = 60) -> list[dict]:
        rows = self.db.execute(
            "SELECT ts, symbol, side, qty, price, value, fee, pnl, pnl_pct, reason "
            "FROM trades ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        keys = ["ts", "symbol", "side", "qty", "price", "value", "fee", "pnl", "pnl_pct", "reason"]
        return [dict(zip(keys, r)) for r in rows]

    def realized_pnl(self) -> float:
        row = self.db.execute("SELECT COALESCE(SUM(pnl),0) FROM trades WHERE side='SELL'").fetchone()
        return row[0]

    def trades_for(self, symbol: str, since_ts: float) -> list[dict]:
        rows = self.db.execute(
            "SELECT ts, side, price FROM trades WHERE symbol=? AND ts>=? ORDER BY ts",
            (symbol, since_ts)).fetchall()
        return [{"ts": r[0], "side": r[1], "price": r[2]} for r in rows]
