"""Combine technical + news scores into one signal and classify it."""

TA_WEIGHT = 0.65
NEWS_WEIGHT = 0.35
BUY_THRESHOLD = 30.0
SELL_THRESHOLD = -25.0


def combine(ta_score: float, news_score: float) -> float:
    return TA_WEIGHT * ta_score + NEWS_WEIGHT * news_score


def classify(total: float) -> str:
    if total >= 55:
        return "STRONG BUY"
    if total >= BUY_THRESHOLD:
        return "BUY"
    if total >= 12:
        return "WATCH"
    if total <= -50:
        return "STRONG SELL"
    if total <= SELL_THRESHOLD:
        return "SELL"
    if total <= -12:
        return "CAUTION"
    return "NEUTRAL"
