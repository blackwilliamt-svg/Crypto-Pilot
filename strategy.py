"""Trading styles + signal combination/classification.

Three profiles trade off reaction speed and risk appetite. The active style
is switched at runtime from the dashboard and persisted in the database, so
thresholds/limits are read through PARAMS instead of module constants.
"""

STYLES: dict[str, dict] = {
    "conservative": {
        "ta_weight": 0.60, "news_weight": 0.40,
        "buy_threshold": 38.0, "sell_threshold": -28.0,
        "max_positions": 4, "position_fraction": 0.15,
        "atr_stop_mult": 2.5, "atr_target_mult": 3.5,
        "max_stop_pct": -0.06,
        "reentry_cooldown": 7200,
    },
    "balanced": {
        "ta_weight": 0.65, "news_weight": 0.35,
        "buy_threshold": 30.0, "sell_threshold": -25.0,
        "max_positions": 5, "position_fraction": 0.18,
        "atr_stop_mult": 2.0, "atr_target_mult": 3.0,
        "max_stop_pct": -0.10,
        "reentry_cooldown": 3600,
    },
    "aggressive": {
        "ta_weight": 0.70, "news_weight": 0.30,
        "buy_threshold": 24.0, "sell_threshold": -18.0,
        "max_positions": 8, "position_fraction": 0.12,
        "atr_stop_mult": 1.8, "atr_target_mult": 2.8,
        "max_stop_pct": -0.10,
        "reentry_cooldown": 1800,
    },
}

ACTIVE_STYLE = "aggressive"
PARAMS = STYLES[ACTIVE_STYLE]


def set_style(name: str) -> bool:
    global ACTIVE_STYLE, PARAMS
    if name not in STYLES:
        return False
    ACTIVE_STYLE = name
    PARAMS = STYLES[name]
    return True


def combine(ta_score: float, news_score: float) -> float:
    return PARAMS["ta_weight"] * ta_score + PARAMS["news_weight"] * news_score


def classify(total: float) -> str:
    buy, sell = PARAMS["buy_threshold"], PARAMS["sell_threshold"]
    if total >= buy * 2:
        return "STRONG BUY"
    if total >= buy:
        return "BUY"
    if total >= 10:
        return "WATCH"
    if total <= sell * 2.5:
        return "STRONG SELL"
    if total <= sell:
        return "SELL"
    if total <= -10:
        return "CAUTION"
    return "NEUTRAL"
