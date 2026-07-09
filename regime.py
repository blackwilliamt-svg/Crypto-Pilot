"""Market regime detection: is this a market to be aggressive in, or defensive?

Crypto alts are heavily correlated with BTC — most losing streaks come from
buying alt breakouts while the whole market rolls over. Two inputs:

  * BTC trend bias on 4h and 1d candles (the market's gravitational field)
  * Breadth: what fraction of the tradable universe is above its 1h EMA50
    (is strength broad, or is everything already underwater?)

Output regimes and their effect (applied in strategy.effective_params):
  risk_on   — trade the style as configured
  neutral   — slightly stricter entries
  risk_off  — much stricter entries, half risk per trade; exits unaffected
"""
import indicators

BREADTH_BULL = 0.55
BREADTH_BEAR = 0.35


def market_breadth(candles_1h: dict[str, list[dict]]) -> float:
    """Fraction of coins trading above their 1h EMA50. 0..1."""
    above = total = 0
    for candles in candles_1h.values():
        closes = [c["c"] for c in candles]
        if len(closes) < 55:
            continue
        total += 1
        if closes[-1] > indicators.ema_series(closes, 50)[-1]:
            above += 1
    return above / total if total else 0.5


def compute(btc_4h: list[dict], btc_1d: list[dict], candles_1h: dict[str, list[dict]]) -> dict:
    btc_bias = 0.6 * indicators.trend_bias(btc_4h) + 0.4 * indicators.trend_bias(btc_1d)
    breadth = market_breadth(candles_1h)

    if btc_bias <= -0.3 or breadth <= BREADTH_BEAR:
        name = "risk_off"
    elif btc_bias >= 0.3 and breadth >= BREADTH_BULL:
        name = "risk_on"
    else:
        name = "neutral"

    return {"name": name, "btc_bias": btc_bias, "breadth": breadth}
