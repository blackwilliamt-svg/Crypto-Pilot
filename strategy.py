"""Combine technical + news scores into one signal and classify it.

Aggressive profile: lower entry bar, quicker bearish exits, TA weighted
heavier than news since the primary signal now runs on 15-minute candles.
"""

TA_WEIGHT = 0.70
NEWS_WEIGHT = 0.30
BUY_THRESHOLD = 24.0
SELL_THRESHOLD = -18.0


def combine(ta_score: float, news_score: float) -> float:
    return TA_WEIGHT * ta_score + NEWS_WEIGHT * news_score


def classify(total: float) -> str:
    if total >= 48:
        return "STRONG BUY"
    if total >= BUY_THRESHOLD:
        return "BUY"
    if total >= 10:
        return "WATCH"
    if total <= -45:
        return "STRONG SELL"
    if total <= SELL_THRESHOLD:
        return "SELL"
    if total <= -10:
        return "CAUTION"
    return "NEUTRAL"
