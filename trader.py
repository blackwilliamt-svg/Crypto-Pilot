"""Paper-trading engine with SQLite persistence. No real funds ever move."""
import json
import sqlite3
import time

START_CASH = 10_000.0
FEE_RATE = 0.001          # simulated 0.1% taker fee
MAX_POSITIONS = 8         # aggressive: more concurrent bets, smaller slices
POSITION_FRACTION = 0.12  # of equity per new position
MIN_TRADE_CASH = 200.0
REENTRY_COOLDOWN = 1800   # sec after exiting a coin before it can be re-bought —
                          # stops fast 15m signals from churning buy/stop-out/rebuy loops

# Adaptive stop-loss / take-profit: sized off each coin's own ATR (volatility)
# instead of one fixed percentage for every coin. The stop trails up as price
# rises (chandelier exit) and only ever tightens; the target extends further
# while the trend holds and freezes once it weakens. ATR here comes from the
# hourly candles (structural volatility), not the noisy 15m signal timeframe.
ATR_STOP_MULT = 1.8
ATR_TARGET_MULT = 2.8
MAX_STOP_PCT = -0.10       # aggressive: cut losers faster, never risk more than 10%
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
        self._load()

    def _load(self):
        row = self.db.execute("SELECT value FROM kv WHERE key='state'").fetchone()
        if row:
            state = json.loads(row[0])
            self.cash = state["cash"]
            self.positions = state["positions"]
            for pos in self.positions.values():  # backfill if loading a pre-trailing-stop save
                pos.setdefault("high", pos["entry"])
                pos.setdefault("stop", pos["entry"] * (1 + MAX_STOP_PCT))
                pos.setdefault("target", pos["entry"] * (1 + MIN_TARGET_PCT))

    def _save(self):
        blob = json.dumps({"cash": self.cash, "positions": self.positions})
        self.db.execute(
            "INSERT INTO kv (key, value) VALUES ('state', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (blob,))
        self.db.commit()

    def reset(self):
        self.db.execute("DELETE FROM trades")
        self.db.execute("DELETE FROM equity")
        self.db.execute("DELETE FROM kv")
        self.db.commit()
        self.cash = START_CASH
        self.positions = {}
        self._save()

    def equity(self, prices: dict[str, float]) -> float:
        return self.cash + sum(
            p["qty"] * prices.get(sym, p["entry"]) for sym, p in self.positions.items())

    def in_cooldown(self, symbol: str) -> bool:
        return time.time() - self.last_exit.get(symbol, 0.0) < REENTRY_COOLDOWN

    def buy(self, symbol: str, price: float, reason: str, prices: dict[str, float], atr: float = 0.0) -> bool:
        if symbol in self.positions or len(self.positions) >= MAX_POSITIONS:
            return False
        if self.in_cooldown(symbol):
            return False
        spend = min(self.cash, max(MIN_TRADE_CASH, self.equity(prices) * POSITION_FRACTION))
        if self.cash < MIN_TRADE_CASH:
            return False
        fee = spend * FEE_RATE
        qty = (spend - fee) / price
        self.cash -= spend
        stop = max(price - ATR_STOP_MULT * atr, price * (1 + MAX_STOP_PCT))
        stop = min(stop, price * (1 + MIN_STOP_DIST_PCT))
        target = max(price + ATR_TARGET_MULT * atr, price * (1 + MIN_TARGET_PCT))
        target = min(target, price * (1 + MAX_TARGET_PCT))
        self.positions[symbol] = {
            "qty": qty, "entry": price, "opened": time.time(),
            "high": price, "stop": stop, "target": target,
        }
        self.db.execute(
            "INSERT INTO trades (ts, symbol, side, qty, price, value, fee, pnl, pnl_pct, reason) "
            "VALUES (?,?,?,?,?,?,?,NULL,NULL,?)",
            (time.time(), symbol, "BUY", qty, price, spend, fee, reason))
        self._save()
        return True

    def sell(self, symbol: str, price: float, reason: str) -> float | None:
        pos = self.positions.pop(symbol, None)
        if not pos:
            return None
        gross = pos["qty"] * price
        fee = gross * FEE_RATE
        proceeds = gross - fee
        cost = pos["qty"] * pos["entry"]
        pnl = proceeds - cost
        pnl_pct = (pnl / cost) * 100.0 if cost else 0.0
        self.cash += proceeds
        self.last_exit[symbol] = time.time()
        self.db.execute(
            "INSERT INTO trades (ts, symbol, side, qty, price, value, fee, pnl, pnl_pct, reason) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (time.time(), symbol, "SELL", pos["qty"], price, gross, fee, pnl, pnl_pct, reason))
        self._save()
        return pnl

    def update_trailing(self, symbol: str, price: float, atr: float, trend_ok: bool):
        """Adapt the stop/target to the latest price and volatility. The stop
        only ever tightens (chandelier exit, locks in gains as price rises);
        the target only extends further while `trend_ok` (trend still holding),
        and otherwise freezes rather than pulling back."""
        pos = self.positions.get(symbol)
        if not pos or not atr:
            return
        pos["high"] = max(pos["high"], price)
        floor_stop = pos["entry"] * (1 + MAX_STOP_PCT)
        candidate_stop = pos["high"] - ATR_STOP_MULT * atr
        ceiling_stop = price * (1 + MIN_STOP_DIST_PCT)
        pos["stop"] = min(max(pos["stop"], candidate_stop, floor_stop), ceiling_stop)
        if trend_ok:
            candidate_target = price + ATR_TARGET_MULT * atr
            max_target = price * (1 + MAX_TARGET_PCT)
            pos["target"] = min(max(pos["target"], candidate_target), max_target)
        self._save()

    def check_exits(self, prices: dict[str, float]) -> list[str]:
        """Adaptive stop-loss / take-profit sweep. Returns human-readable actions."""
        actions = []
        for symbol in list(self.positions):
            price = prices.get(symbol)
            if not price:
                continue
            pos = self.positions[symbol]
            ret = price / pos["entry"] - 1.0
            if price <= pos["stop"]:
                self.sell(symbol, price, f"Trailing stop hit at ${pos['stop']:,.4f} ({ret * 100:+.1f}% from entry)")
                actions.append(f"Stop-loss closed {symbol} at ${price:,.2f} ({ret * 100:+.1f}%)")
            elif price >= pos["target"]:
                self.sell(symbol, price, f"Take-profit target hit at ${pos['target']:,.4f} ({ret * 100:+.1f}% from entry)")
                actions.append(f"Take-profit closed {symbol} at ${price:,.2f} ({ret * 100:+.1f}%)")
        return actions

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
