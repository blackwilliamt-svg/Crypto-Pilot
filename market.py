"""Kraken public market-data client + CoinGecko universe discovery (no API keys required)."""
import time

import requests

KRAKEN_BASE = "https://api.kraken.com/0/public"
COINGECKO_BASE = "https://api.coingecko.com/api/v3"
MIN_MARKET_CAP = 100_000_000
MAX_COINS = 150

_session = requests.Session()
_session.headers["User-Agent"] = "CryptoPilot-paper-bot/1.0"


def _kraken_usd_pairs() -> dict[str, str]:
    """Map base-asset ticker (e.g. 'BTC') -> Kraken pair name (e.g. 'XBTUSD')."""
    r = _session.get(f"{KRAKEN_BASE}/AssetPairs", timeout=15)
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        raise RuntimeError(f"Kraken AssetPairs error: {data['error']}")
    pairs = {}
    for pair_name, info in data["result"].items():
        if info.get("quote") not in ("ZUSD", "USD"):
            continue
        wsname = info.get("wsname") or ""
        if not wsname.endswith("/USD"):
            continue
        base = wsname.split("/")[0]
        symbol = "BTC" if base == "XBT" else base  # Kraken's legacy BTC ticker
        pairs.setdefault(symbol, pair_name)
    return pairs


def _coingecko_stablecoin_ids() -> set[str]:
    r = _session.get(f"{COINGECKO_BASE}/coins/markets", params={
        "vs_currency": "usd", "category": "stablecoins", "per_page": 250, "page": 1,
    }, timeout=20)
    r.raise_for_status()
    return {row["id"] for row in r.json()}


def discover_universe(max_coins: int = MAX_COINS) -> dict[str, dict]:
    """Non-stablecoins with market cap > MIN_MARKET_CAP that are also spot-tradable
    against USD on Kraken (that's our only price/candle source). Returns
    {symbol: {"name": ..., "pair": ..., "market_cap": ...}}, sorted by market cap desc."""
    kraken_pairs = _kraken_usd_pairs()
    stable_ids = _coingecko_stablecoin_ids()

    coins: dict[str, dict] = {}
    page = 1
    while len(coins) < max_coins and page <= 5:
        r = _session.get(f"{COINGECKO_BASE}/coins/markets", params={
            "vs_currency": "usd", "order": "market_cap_desc",
            "per_page": 250, "page": page,
        }, timeout=20)
        r.raise_for_status()
        rows = r.json()
        if not rows:
            break
        below_threshold = False
        for row in rows:
            mcap = row.get("market_cap") or 0
            if mcap < MIN_MARKET_CAP:
                below_threshold = True  # results are market-cap-sorted; nothing further qualifies
                break
            if row["id"] in stable_ids:
                continue
            symbol = row["symbol"].upper()
            pair = kraken_pairs.get(symbol)
            if not pair or symbol in coins:
                continue
            coins[symbol] = {"name": row["name"], "pair": pair, "market_cap": mcap}
            if len(coins) >= max_coins:
                break
        if below_threshold:
            break
        page += 1
        time.sleep(1.0)  # stay under CoinGecko's free-tier rate limit
    return coins


def fetch_ohlc(pair: str, interval: int = 60, since: int | None = None) -> list[dict]:
    """Hourly candles (oldest -> newest). Kraken returns up to ~720 rows, or only
    rows newer than `since` (a unix timestamp) when given — used for cheap incremental refreshes."""
    params = {"pair": pair, "interval": interval}
    if since is not None:
        params["since"] = since
    r = _session.get(f"{KRAKEN_BASE}/OHLC", params=params, timeout=15)
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
