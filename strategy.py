"""Trading styles + signal combination/classification + regime adjustment.

Three style profiles trade off reaction speed and risk appetite; the market
regime (regime.py) then overlays defensive adjustments on top. PARAMS is
always the fully-resolved dict (style x regime) that the rest of the app
reads — call set_style()/set_regime() to change either input.

risk_per_trade is the fraction of equity a position may LOSE if its initial
stop is hit (equal-risk sizing) — not the position's notional size.
"""

STYLES: dict[str, dict] = {
    "conservative": {
        "ta_weight": 0.60, "news_weight": 0.40,
        "buy_threshold": 38.0, "sell_threshold": -28.0,
        "max_positions": 4, "position_fraction": 0.20,
        "risk_per_trade": 0.010,
        "daily_max_loss": 0.02,
        "atr_stop_mult": 2.5, "atr_target_mult": 3.5,
        "max_stop_pct": -0.06,
        "reentry_cooldown": 7200,
        "breakeven_trigger": 0.015, "ratchet_trigger": 0.03,
        "weak_loss_cut": -0.02, "max_hold_hours": 96,
    },
    "balanced": {
        "ta_weight": 0.65, "news_weight": 0.35,
        "buy_threshold": 30.0, "sell_threshold": -25.0,
        "max_positions": 5, "position_fraction": 0.22,
        "risk_per_trade": 0.015,
        "daily_max_loss": 0.03,
        "atr_stop_mult": 2.0, "atr_target_mult": 3.0,
        "max_stop_pct": -0.10,
        "reentry_cooldown": 3600,
        "breakeven_trigger": 0.02, "ratchet_trigger": 0.04,
        "weak_loss_cut": -0.025, "max_hold_hours": 60,
    },
    "aggressive": {
        "ta_weight": 0.70, "news_weight": 0.30,
        "buy_threshold": 24.0, "sell_threshold": -18.0,
        "max_positions": 8, "position_fraction": 0.25,
        "risk_per_trade": 0.020,
        "daily_max_loss": 0.04,
        "atr_stop_mult": 1.8, "atr_target_mult": 2.8,
        "max_stop_pct": -0.10,
        "reentry_cooldown": 1800,
        "breakeven_trigger": 0.025, "ratchet_trigger": 0.05,
        "weak_loss_cut": -0.03, "max_hold_hours": 36,
    },
}

ACTIVE_STYLE = "aggressive"
ACTIVE_REGIME = "neutral"
PARAMS: dict = {}


def _rebuild():
    global PARAMS
    p = dict(STYLES[ACTIVE_STYLE])
    if ACTIVE_REGIME == "neutral":
        p["buy_threshold"] += 3.0
    elif ACTIVE_REGIME == "risk_off":
        # Defensive: much stricter entries, half the risk budget, fewer
        # concurrent bets. Exits/stops are untouched — de-risking never
        # blocks getting OUT of a position.
        p["buy_threshold"] += 10.0
        p["risk_per_trade"] *= 0.5
        p["max_positions"] = max(2, p["max_positions"] - 2)
    PARAMS = p


def set_style(name: str) -> bool:
    global ACTIVE_STYLE
    if name not in STYLES:
        return False
    ACTIVE_STYLE = name
    _rebuild()
    return True


def set_regime(name: str):
    global ACTIVE_REGIME
    if name in ("risk_on", "neutral", "risk_off"):
        ACTIVE_REGIME = name
        _rebuild()


_rebuild()


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
