"""Paper-trading engine with SQLite persistence. No real funds ever move."""
import json
import sqlite3
import time

START_CASH = 10_000.0
FEE_RATE = 0.001          # simulated 0.1% taker fee
MAX_POSITIONS = 5
POSITION_FRACTION = 0.18  # of equity per new position
MIN_TRADE_CASH = 200.0
STOP_LOSS = -0.05
TAKE_PROFIT = 0.12


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
        self._load()

    def _load(self):
        row = self.db.execute("SELECT value FROM kv WHERE key='state'").fetchone()
        if row:
            state = json.loads(row[0])
            self.cash = state["cash"]
            self.positions = state["positions"]

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

    def buy(self, symbol: str, price: float, reason: str, prices: dict[str, float]) -> bool:
        if symbol in self.positions or len(self.positions) >= MAX_POSITIONS:
            return False
        spend = min(self.cash, max(MIN_TRADE_CASH, self.equity(prices) * POSITION_FRACTION))
        if self.cash < MIN_TRADE_CASH:
            return False
        fee = spend * FEE_RATE
        qty = (spend - fee) / price
        self.cash -= spend
        self.positions[symbol] = {"qty": qty, "entry": price, "opened": time.time()}
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
        self.db.execute(
            "INSERT INTO trades (ts, symbol, side, qty, price, value, fee, pnl, pnl_pct, reason) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (time.time(), symbol, "SELL", pos["qty"], price, gross, fee, pnl, pnl_pct, reason))
        self._save()
        return pnl

    def check_exits(self, prices: dict[str, float]) -> list[str]:
        """Stop-loss / take-profit sweep. Returns human-readable actions."""
        actions = []
        for symbol in list(self.positions):
            price = prices.get(symbol)
            if not price:
                continue
            entry = self.positions[symbol]["entry"]
            ret = price / entry - 1.0
            if ret <= STOP_LOSS:
                self.sell(symbol, price, f"Stop-loss hit ({ret * 100:+.1f}% from entry)")
                actions.append(f"Stop-loss closed {symbol} at ${price:,.2f} ({ret * 100:+.1f}%)")
            elif ret >= TAKE_PROFIT:
                self.sell(symbol, price, f"Take-profit hit ({ret * 100:+.1f}% from entry)")
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
