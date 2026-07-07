"""Crypto headline scanning: public RSS feeds + lexicon sentiment scoring."""
import re
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

import requests

FEEDS = [
    ("CoinDesk",      "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Cointelegraph", "https://cointelegraph.com/rss"),
    ("Decrypt",       "https://decrypt.co/feed"),
    ("CryptoSlate",   "https://cryptoslate.com/feed/"),
    ("Bitcoin Mag",   "https://bitcoinmagazine.com/feed"),
]

BULLISH = {
    "surge": 2, "surges": 2, "surging": 2, "soar": 3, "soars": 3, "rally": 2,
    "rallies": 2, "record high": 3, "all-time high": 3, "breakout": 2,
    "bullish": 2, "adoption": 2, "approval": 2, "approves": 2, "inflow": 2,
    "inflows": 2, "partnership": 1, "upgrade": 1, "institutional": 2,
    "accumulate": 2, "accumulating": 2, "gains": 1, "jumps": 2, "climbs": 1,
    "rebound": 2, "rebounds": 2, "recovers": 2, "recovery": 1, "outperform": 2,
    "milestone": 1, "greenlight": 2, "spike": 2, "spikes": 2, "buying": 1,
    "boosts": 1, "boost": 1, "tops": 1, "breaks above": 2, "pump": 1,
}
BEARISH = {
    "crash": 3, "crashes": 3, "plunge": 3, "plunges": 3, "hack": 3,
    "hacked": 3, "exploit": 3, "lawsuit": 2, "sues": 2, "sued": 2, "ban": 2,
    "bans": 2, "crackdown": 2, "selloff": 2, "sell-off": 2, "dump": 2,
    "dumps": 2, "bearish": 2, "liquidation": 2, "liquidations": 2, "fraud": 3,
    "scam": 2, "bankruptcy": 3, "outflow": 2, "outflows": 2, "falls": 1,
    "drops": 1, "tumbles": 2, "slumps": 2, "slides": 1, "fear": 1,
    "warning": 1, "warns": 1, "delays": 1, "rejected": 2, "rejects": 2,
    "halts": 2, "probe": 2, "investigation": 2, "theft": 3, "stolen": 3,
    "sinks": 2, "plummets": 3, "breaks below": 2, "losses": 1, "seized": 2,
}

COIN_PATTERNS = {
    "BTC":  r"bitcoin|\bbtc\b",
    "ETH":  r"ethereum|\bether\b|\beth\b",
    "SOL":  r"solana|\bsol\b",
    "XRP":  r"\bxrp\b|ripple",
    "ADA":  r"cardano|\bada\b",
    "DOGE": r"dogecoin|\bdoge\b",
    "DOT":  r"polkadot",
    "LINK": r"chainlink",
    "AVAX": r"avalanche|\bavax\b",
    "LTC":  r"litecoin|\bltc\b",
}
_coin_res = {sym: re.compile(pat, re.I) for sym, pat in COIN_PATTERNS.items()}
_term_res = {
    term: (re.compile(r"\b" + re.escape(term) + r"\b", re.I), w)
    for lex in (BULLISH, BEARISH) for term, w in lex.items()
}

_session = requests.Session()
_session.headers["User-Agent"] = "Mozilla/5.0 (CryptoPilot paper bot)"


def _score_text(text: str) -> float:
    raw = 0.0
    for term, (rx, w) in _term_res.items():
        if rx.search(text):
            raw += w if term in BULLISH else -w
    return max(-100.0, min(100.0, raw * 22.0))


def _parse_feed(source: str, xml_text: str) -> list[dict]:
    items = []
    root = ET.fromstring(xml_text)
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        link = (item.findtext("link") or "").strip()
        ts = time.time()
        pub = item.findtext("pubDate")
        if pub:
            try:
                ts = parsedate_to_datetime(pub).timestamp()
            except Exception:
                pass
        items.append({"title": title, "link": link, "source": source, "ts": ts})
    return items


def fetch_headlines(max_age_hours: float = 48.0) -> list[dict]:
    """Fetch + score all feeds. Individual feed failures are skipped."""
    now = time.time()
    seen: set[str] = set()
    headlines = []
    for source, url in FEEDS:
        try:
            r = _session.get(url, timeout=12)
            r.raise_for_status()
            items = _parse_feed(source, r.text)
        except Exception:
            continue
        for it in items:
            key = it["title"].lower()
            if key in seen or now - it["ts"] > max_age_hours * 3600:
                continue
            seen.add(key)
            sent = _score_text(it["title"])
            coins = [sym for sym, rx in _coin_res.items() if rx.search(it["title"])]
            it.update({
                "sentiment": sent,
                "coins": coins,
                "label": "bullish" if sent > 10 else ("bearish" if sent < -10 else "neutral"),
            })
            headlines.append(it)
    headlines.sort(key=lambda h: h["ts"], reverse=True)
    return headlines


def coin_sentiment(headlines: list[dict], symbol: str) -> dict:
    """Recency-weighted sentiment for one coin; market-wide news bleeds in at 30%."""
    now = time.time()

    def agg(items):
        num = den = 0.0
        for h in items:
            w = 0.5 ** ((now - h["ts"]) / 3600.0 / 12.0)  # 12h half-life
            num += h["sentiment"] * w
            den += w
        return (num / den) if den else 0.0

    specific = [h for h in headlines if symbol in h["coins"]]
    market = [h for h in headlines if not h["coins"]]
    spec_score, mkt_score = agg(specific), agg(market)
    score = 0.7 * spec_score + 0.3 * mkt_score if specific else 0.5 * mkt_score
    top = max(specific, key=lambda h: abs(h["sentiment"]), default=None)
    return {
        "score": max(-100.0, min(100.0, score)),
        "count": len(specific),
        "top_headline": top["title"] if top and abs(top["sentiment"]) > 10 else None,
    }
