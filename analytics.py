"""Performance analytics computed from the trade log and equity history.

Everything a trader actually judges a strategy by: win rate, profit factor,
expectancy, drawdown, risk-adjusted return — not just headline P&L.
"""
import math


def _pair_hold_times(trades: list[dict]) -> list[float]:
    """Hours held per round trip, pairing each SELL with the latest earlier
    BUY of the same symbol (trades arrive newest-first from recent_trades)."""
    holds = []
    chron = sorted(trades, key=lambda t: t["ts"])
    open_ts: dict[str, float] = {}
    for t in chron:
        if t["side"] == "BUY":
            open_ts[t["symbol"]] = t["ts"]
        elif t["side"] == "SELL" and t["symbol"] in open_ts:
            holds.append((t["ts"] - open_ts.pop(t["symbol"])) / 3600.0)
    return holds


def compute(trades: list[dict], equity_history: list[list[float]], start_cash: float) -> dict:
    sells = [t for t in trades if t["side"] == "SELL" and t["pnl"] is not None]
    wins = [t for t in sells if t["pnl"] > 0]
    losses = [t for t in sells if t["pnl"] <= 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t["pnl"] for t in losses)

    # current win/loss streak (from most recent sells backwards)
    streak = 0
    for t in sorted(sells, key=lambda t: t["ts"], reverse=True):
        won = t["pnl"] > 0
        if streak == 0:
            streak = 1 if won else -1
        elif (streak > 0) == won:
            streak += 1 if won else -1
        else:
            break

    # max drawdown over the equity curve
    max_dd = 0.0
    peak = start_cash
    for _, value in equity_history:
        peak = max(peak, value)
        if peak > 0:
            max_dd = min(max_dd, value / peak - 1.0)

    # Approximate annualized Sharpe from per-sample equity returns. Samples
    # are one cycle apart (~45s); we estimate the sampling rate from the
    # timestamps rather than assuming it. Labeled approximate on purpose.
    sharpe = None
    if len(equity_history) >= 20:
        rets = []
        for (t0, v0), (t1, v1) in zip(equity_history, equity_history[1:]):
            if v0 > 0 and t1 > t0:
                rets.append((v1 / v0 - 1.0, t1 - t0))
        if rets:
            mean_dt = sum(dt for _, dt in rets) / len(rets)
            samples_per_year = (365 * 24 * 3600) / mean_dt if mean_dt else 0
            vals = [r for r, _ in rets]
            mean = sum(vals) / len(vals)
            sd = (sum((r - mean) ** 2 for r in vals) / len(vals)) ** 0.5
            if sd > 0 and samples_per_year > 0:
                sharpe = (mean / sd) * math.sqrt(samples_per_year)

    # per-coin realized P&L leaders
    by_coin: dict[str, float] = {}
    for t in sells:
        by_coin[t["symbol"]] = by_coin.get(t["symbol"], 0.0) + t["pnl"]
    ranked = sorted(by_coin.items(), key=lambda kv: kv[1], reverse=True)

    holds = _pair_hold_times(trades)

    return {
        "closed_trades": len(sells),
        "win_rate": (len(wins) / len(sells) * 100.0) if sells else None,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else (None if not wins else float("inf")),
        "avg_win": (gross_win / len(wins)) if wins else None,
        "avg_loss": (-gross_loss / len(losses)) if losses else None,
        "expectancy": ((gross_win - gross_loss) / len(sells)) if sells else None,
        "current_streak": streak,
        "max_drawdown_pct": max_dd * 100.0,
        "sharpe_approx": sharpe,
        "avg_hold_hours": (sum(holds) / len(holds)) if holds else None,
        "best_coins": ranked[:3],
        "worst_coins": ranked[-3:][::-1] if ranked else [],
        "total_fees": sum(t.get("fee") or 0.0 for t in trades),
    }
