"""Pure-python technical indicators and a composite TA score (-100..+100)."""


def ema_series(values: list[float], period: int) -> list[float]:
    k = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1.0 - k))
    return out


def rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) <= period:
        return 50.0
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    avg_g, avg_l = gains / period, losses / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_g = (avg_g * (period - 1) + max(d, 0.0)) / period
        avg_l = (avg_l * (period - 1) + max(-d, 0.0)) / period
    if avg_l == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_g / avg_l)


def macd_series(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9):
    ef, es = ema_series(closes, fast), ema_series(closes, slow)
    line = [a - b for a, b in zip(ef, es)]
    sig = ema_series(line, signal)
    hist = [a - b for a, b in zip(line, sig)]
    return line, sig, hist


def bollinger(closes: list[float], period: int = 20, mult: float = 2.0):
    window = closes[-period:]
    mid = sum(window) / len(window)
    sd = (sum((c - mid) ** 2 for c in window) / len(window)) ** 0.5
    upper, lower = mid + mult * sd, mid - mult * sd
    rng = upper - lower
    pct_b = 0.5 if rng == 0 else (closes[-1] - lower) / rng
    return mid, upper, lower, pct_b


def rolling_bollinger(closes: list[float], period: int = 20, mult: float = 2.0):
    """Upper/lower band series for charting; None-padded to align with closes."""
    upper, lower = [None] * len(closes), [None] * len(closes)
    for i in range(period - 1, len(closes)):
        window = closes[i - period + 1:i + 1]
        mid = sum(window) / period
        sd = (sum((c - mid) ** 2 for c in window) / period) ** 0.5
        upper[i], lower[i] = mid + mult * sd, mid - mult * sd
    return upper, lower


def roc(closes: list[float], n: int = 10) -> float:
    if len(closes) <= n or closes[-1 - n] == 0:
        return 0.0
    return (closes[-1] / closes[-1 - n] - 1.0) * 100.0


def adx(candles: list[dict], period: int = 14) -> float:
    """Average Directional Index (Wilder) — trend STRENGTH regardless of
    direction. <18 means chop (trend signals unreliable); >25 means a real
    trend is in force."""
    if len(candles) < period * 2 + 1:
        return 0.0
    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, len(candles)):
        h, l = candles[i]["h"], candles[i]["l"]
        ph, pl, pc = candles[i - 1]["h"], candles[i - 1]["l"], candles[i - 1]["c"]
        up, dn = h - ph, pl - l
        plus_dm.append(up if up > dn and up > 0 else 0.0)
        minus_dm.append(dn if dn > up and dn > 0 else 0.0)
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))

    def wilder_smooth(vals):
        s = sum(vals[:period])
        out = [s]
        for v in vals[period:]:
            s = s - s / period + v
            out.append(s)
        return out

    str_, spd, smd = wilder_smooth(trs), wilder_smooth(plus_dm), wilder_smooth(minus_dm)
    dxs = []
    for t, p, m in zip(str_, spd, smd):
        if t == 0:
            continue
        pdi, mdi = 100.0 * p / t, 100.0 * m / t
        denom = pdi + mdi
        if denom:
            dxs.append(100.0 * abs(pdi - mdi) / denom)
    if len(dxs) < period:
        return 0.0
    a = sum(dxs[:period]) / period
    for d in dxs[period:]:
        a = (a * (period - 1) + d) / period
    return a


def stoch_rsi(closes: list[float], period: int = 14) -> float:
    """Stochastic RSI (0..1): where the current RSI sits inside its own
    recent range. More sensitive than raw RSI at catching turns."""
    if len(closes) < period * 2 + 1:
        return 0.5
    rsis = [rsi(closes[: i + 1][-period * 3:], period) for i in range(len(closes) - period, len(closes))]
    lo, hi = min(rsis), max(rsis)
    return 0.5 if hi == lo else (rsis[-1] - lo) / (hi - lo)


def volume_zscore(candles: list[dict], lookback: int = 20) -> float:
    """How unusual the latest bar's volume is vs the prior `lookback` bars."""
    if len(candles) < lookback + 1:
        return 0.0
    vols = [c["v"] for c in candles[-(lookback + 1):-1]]
    mean = sum(vols) / len(vols)
    sd = (sum((v - mean) ** 2 for v in vols) / len(vols)) ** 0.5
    return 0.0 if sd == 0 else (candles[-1]["v"] - mean) / sd


def atr(candles: list[dict], period: int = 14) -> float:
    """Average True Range (Wilder-smoothed) — used to size stops/targets to
    each coin's own volatility instead of a one-size-fits-all percentage."""
    if len(candles) <= period:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["h"], candles[i]["l"], candles[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    avg = sum(trs[:period]) / period
    for tr in trs[period:]:
        avg = (avg * (period - 1) + tr) / period
    return avg


def trend_bias(candles: list[dict]) -> float:
    """Directional bias (-1..+1) for a higher timeframe (4h/1d), from EMA20 vs
    EMA50 and RSI regime. Cheaper than a full analyze() — used only to
    confirm/veto the primary hourly signal, not as a standalone score."""
    closes = [c["c"] for c in candles]
    if len(closes) < 55:
        return 0.0
    e20, e50 = ema_series(closes, 20), ema_series(closes, 50)
    r = rsi(closes)
    bias = 0.6 if e20[-1] > e50[-1] else -0.6
    if r >= 55:
        bias += 0.4
    elif r <= 45:
        bias -= 0.4
    return max(-1.0, min(1.0, bias))


def analyze(candles: list[dict], bars_per_hour: int = 1) -> dict:
    """Score a coin from its candles (any timeframe; pass bars_per_hour so
    fixed-hour stats like 24h change stay correct). Positive = bullish."""
    closes = [c["c"] for c in candles]
    price = closes[-1]
    reasons: list[str] = []
    score = 0.0

    r = rsi(closes)
    _, _, hist = macd_series(closes)
    e20, e50 = ema_series(closes, 20), ema_series(closes, 50)
    _, _, _, pct_b = bollinger(closes)
    momentum = roc(closes, 10)
    trend_strength = adx(candles)
    srsi = stoch_rsi(closes)
    vol_z = volume_zscore(candles)
    n24 = 24 * bars_per_hour
    change24 = (price / closes[-(n24 + 1)] - 1.0) * 100.0 if len(closes) > n24 else 0.0

    # Trend-following components get scaled by ADX: full weight in a real
    # trend, halved in chop where EMA/MACD signals mostly whipsaw.
    if trend_strength >= 25:
        trend_w = 1.0
    elif trend_strength >= 18:
        trend_w = 0.75
    else:
        trend_w = 0.5
        reasons.append(f"Choppy market (ADX {trend_strength:.0f}) — trend signals discounted")

    if r <= 30:
        score += 25; reasons.append(f"RSI {r:.0f} oversold — bounce setup")
    elif r <= 40:
        score += 10; reasons.append(f"RSI {r:.0f} nearing oversold")
    elif r >= 70:
        score -= 25; reasons.append(f"RSI {r:.0f} overbought — pullback risk")
    elif r >= 60:
        score -= 10; reasons.append(f"RSI {r:.0f} nearing overbought")

    cross_up = any(hist[i] <= 0 < hist[i + 1] for i in range(-4, -1))
    cross_dn = any(hist[i] >= 0 > hist[i + 1] for i in range(-4, -1))
    if cross_up:
        score += 20 * trend_w; reasons.append("MACD bullish crossover (recent bars)")
    elif cross_dn:
        score -= 20 * trend_w; reasons.append("MACD bearish crossover (recent bars)")
    elif hist[-1] > 0 and hist[-1] > hist[-2]:
        score += 12 * trend_w; reasons.append("MACD momentum positive and building")
    elif hist[-1] > 0:
        score += 6 * trend_w
    elif hist[-1] < 0 and hist[-1] < hist[-2]:
        score -= 12 * trend_w; reasons.append("MACD momentum negative and worsening")
    else:
        score -= 6 * trend_w

    if e20[-1] > e50[-1]:
        score += 12 * trend_w
        if trend_strength >= 25:
            reasons.append(f"Uptrend with strength (EMA20>EMA50, ADX {trend_strength:.0f})")
        else:
            reasons.append("Uptrend: EMA20 above EMA50")
    else:
        score -= 12 * trend_w
        if trend_strength >= 25:
            reasons.append(f"Downtrend with strength (EMA20<EMA50, ADX {trend_strength:.0f})")
        else:
            reasons.append("Downtrend: EMA20 below EMA50")
    score += (5 if price > e20[-1] else -5) * trend_w

    if pct_b < 0.05:
        score += 14; reasons.append("Price at lower Bollinger band")
    elif pct_b > 0.95:
        score -= 14; reasons.append("Price stretched above upper Bollinger band")

    # StochRSI catches turns earlier than raw RSI: only score the extremes.
    if srsi <= 0.10 and closes[-1] > closes[-2]:
        score += 8; reasons.append("StochRSI bottomed and turning up")
    elif srsi >= 0.90 and closes[-1] < closes[-2]:
        score -= 8; reasons.append("StochRSI topped and turning down")

    # Volume confirmation: an unusual-volume bar in the direction of the move
    # is conviction; without it, moves are easier to fade.
    if vol_z >= 2.0:
        if closes[-1] >= candles[-1]["o"]:
            score += 6; reasons.append(f"Volume surge confirms buying ({vol_z:.1f}σ)")
        else:
            score -= 6; reasons.append(f"Volume surge confirms selling ({vol_z:.1f}σ)")

    score += max(-12.0, min(12.0, momentum * 1.5))
    if abs(momentum) >= 2:
        mom_hours = 10 / bars_per_hour
        reasons.append(f"{mom_hours:g}h momentum {momentum:+.1f}%")

    return {
        "price": price,
        "change24h": change24,
        "rsi": r,
        "macd_hist": hist[-1],
        "ema20": e20[-1],
        "ema50": e50[-1],
        "pct_b": pct_b,
        "momentum": momentum,
        "adx": trend_strength,
        "stoch_rsi": srsi,
        "vol_z": vol_z,
        "score": max(-100.0, min(100.0, score * 1.1)),
        "reasons": reasons,
    }
