"""Kraken private (authenticated) API client for live order execution.

API credentials come ONLY from environment variables — never hardcode them,
never commit them, never paste them into chat or config files in the repo:

    KRAKEN_API_KEY      your Kraken API key
    KRAKEN_API_SECRET   the matching private key (base64, as Kraken provides it)

Create the key with ONLY these permissions: "Query funds" and
"Create & modify orders". Never grant withdrawal rights to a bot key.

Optional tuning (also env vars):
    LIVE_MAX_ORDER_USD   hard cap per buy order, default 100
    LIVE_BANKROLL_USD    cap on how much of the account the bot manages,
                         default = full available USD balance
"""
import base64
import hashlib
import hmac
import os
import time
import urllib.parse

import requests

API_BASE = "https://api.kraken.com"


class KrakenAuthError(RuntimeError):
    pass


class KrakenOrderError(RuntimeError):
    pass


def credentials_present() -> bool:
    return bool(os.environ.get("KRAKEN_API_KEY")) and bool(os.environ.get("KRAKEN_API_SECRET"))


class KrakenPrivate:
    def __init__(self):
        key = os.environ.get("KRAKEN_API_KEY", "")
        secret = os.environ.get("KRAKEN_API_SECRET", "")
        if not key or not secret:
            raise KrakenAuthError("KRAKEN_API_KEY / KRAKEN_API_SECRET environment variables not set")
        self._key = key
        self._secret = base64.b64decode(secret)
        self._session = requests.Session()

    def _call(self, path: str, data: dict) -> dict:
        data = {**data, "nonce": int(time.time() * 1000)}
        post = urllib.parse.urlencode(data)
        digest = hashlib.sha256((str(data["nonce"]) + post).encode()).digest()
        sig = hmac.new(self._secret, path.encode() + digest, hashlib.sha512)
        r = self._session.post(API_BASE + path, data=data, headers={
            "API-Key": self._key,
            "API-Sign": base64.b64encode(sig.digest()).decode(),
        }, timeout=15)
        r.raise_for_status()
        out = r.json()
        if out.get("error"):
            raise KrakenOrderError("; ".join(out["error"]))
        return out.get("result", {})

    def usd_balance(self) -> float:
        bal = self._call("/0/private/Balance", {})
        return float(bal.get("ZUSD", bal.get("USD", 0.0)))

    def market_order(self, pair: str, side: str, volume_str: str, validate: bool = False) -> dict:
        payload = {"pair": pair, "type": side, "ordertype": "market", "volume": volume_str}
        if validate:
            payload["validate"] = "true"
        return self._call("/0/private/AddOrder", payload)


class LiveExecutor:
    """Mirrors the bot's trades as real Kraken market orders.

    Buys are capped at LIVE_MAX_ORDER_USD notional. Sells are NEVER capped —
    an exit must always close the whole position, capping it would leave
    unmanaged risk on the book."""

    def __init__(self):
        self.api = KrakenPrivate()
        self.max_order_usd = float(os.environ.get("LIVE_MAX_ORDER_USD", "100"))

    @staticmethod
    def _format_volume(qty: float, lot_decimals: int) -> str:
        factor = 10 ** lot_decimals
        floored = int(qty * factor) / factor  # round DOWN so we never overspend/oversell
        return f"{floored:.{lot_decimals}f}"

    def execute(self, side: str, meta: dict, qty: float, price: float) -> float:
        """Place a market order; returns the actually-ordered qty (may be
        smaller than requested for buys due to the notional cap). Raises
        KrakenOrderError/KrakenAuthError on failure — caller must not record
        the trade in that case."""
        if side == "buy" and qty * price > self.max_order_usd:
            qty = self.max_order_usd / price
        volume_str = self._format_volume(qty, meta.get("lot_decimals", 8))
        final_qty = float(volume_str)
        if final_qty <= 0:
            raise KrakenOrderError(f"volume rounds to zero at {meta.get('lot_decimals', 8)} decimals")
        if final_qty < meta.get("ordermin", 0):
            raise KrakenOrderError(
                f"volume {volume_str} below Kraken minimum {meta.get('ordermin')} for {meta.get('pair')}")
        self.api.market_order(meta["pair"], side, volume_str)
        return final_qty
