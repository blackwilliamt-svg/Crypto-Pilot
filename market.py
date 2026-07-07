"""Kraken public market-data client (no API key required)."""
import requests

BASE = "https://api.kraken.com/0/public"

COINS = {
    "BTC":  {"name": "Bitcoin",   "pair": "XBTUSD"},
    "ETH":  {"name": "Ethereum",  "pair": "ETHUSD"},
    "SOL":  {"name": "Solana",    "pair": "SOLUSD"},
    "XRP":  {"name": "XRP",       "pair": "XRPUSD"},
    "ADA":  {"name": "Cardano",   "pair": "ADAUSD"},
    "DOGE": {"name": "Dogecoin",  "pair": "XDGUSD"},
    "DOT":  {"name": "Polkadot",  "pair": "DOTUSD"},
    "LINK": {"name": "Chainlink", "pair": "LINKUSD"},
    "AVAX": {"name": "Avalanche", "pair": "AVAXUSD"},
    "LTC":  {"name": "Litecoin",  "pair": "LTCUSD"},
}

_session = requests.Session()
_session.headers["User-Agent"] = "CryptoPilot-paper-bot/1.0"


def fetch_ohlc(pair: str, interval: int = 60) -> list[dict]:
    """Hourly candles (oldest -> newest). Kraken returns up to ~720."""
    r = _session.get(f"{BASE}/OHLC", params={"pair": pair, "interval": interval}, timeout=15)
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        raise RuntimeError(f"Kraken error for {pair}: {data['error']}")
    result = data["result"]
    key = next(k for k in result if k != "last")
    return [
        {"t": int(row[0]), "o": float(row[1]), "h": float(row[2]),
         "l": float(row[3]), "c": float(row[4]), "v": float(row[6])}
        for row in result[key]
    ]
