"""Trade-report PDF generator — no third-party dependencies.

Writes a minimal but valid PDF 1.4 by hand (text-only: Helvetica for
headings, Courier for aligned tables). Good enough for reviewing how trades
went; not a typesetting engine.
"""
import time

PAGE_W, PAGE_H = 612, 792   # US Letter, points
MARGIN = 46


def _esc(s: str) -> str:
    return (s.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)"))


class _Pdf:
    """Tiny PDF builder: pages of absolutely-positioned text runs."""

    def __init__(self):
        self.pages: list[list[bytes]] = []
        self.new_page()

    def new_page(self):
        self.pages.append([])

    def text(self, x: float, y_top: float, s: str, size: float = 10,
             font: str = "F1", rgb: tuple = (0.1, 0.1, 0.1)):
        """(x, y_top) measured from the page's top-left corner."""
        y = PAGE_H - y_top
        s = s.encode("latin-1", "replace").decode("latin-1")
        r, g, b = rgb
        self.pages[-1].append(
            f"BT /{font} {size:g} Tf {r:g} {g:g} {b:g} rg "
            f"{x:g} {y:g} Td ({_esc(s)}) Tj ET\n".encode("latin-1"))

    def hline(self, y_top: float, x1: float = MARGIN, x2: float = PAGE_W - MARGIN,
              rgb: tuple = (0.75, 0.78, 0.82)):
        y = PAGE_H - y_top
        r, g, b = rgb
        self.pages[-1].append(
            f"{r:g} {g:g} {b:g} RG 0.7 w {x1:g} {y:g} m {x2:g} {y:g} l S\n".encode("latin-1"))

    def render(self) -> bytes:
        fonts = {"F1": "Helvetica", "F2": "Helvetica-Bold", "F3": "Courier",
                 "F4": "Courier-Bold"}
        objs: list[bytes] = []

        def add(body: str | bytes) -> int:
            objs.append(body if isinstance(body, bytes) else body.encode("latin-1"))
            return len(objs)  # 1-based object number

        font_ids = {tag: add(f"<< /Type /Font /Subtype /Type1 /BaseFont /{name} >>")
                    for tag, name in fonts.items()}
        font_res = " ".join(f"/{t} {i} 0 R" for t, i in font_ids.items())

        page_ids = []
        pages_obj_num = len(objs) + 2 * len(self.pages) + 1
        for chunks in self.pages:
            stream = b"".join(chunks)
            cid = add(b"<< /Length " + str(len(stream)).encode()
                      + b" >>\nstream\n" + stream + b"endstream")
            pid = add(f"<< /Type /Page /Parent {pages_obj_num} 0 R "
                      f"/MediaBox [0 0 {PAGE_W} {PAGE_H}] "
                      f"/Resources << /Font << {font_res} >> >> "
                      f"/Contents {cid} 0 R >>")
            page_ids.append(pid)

        kids = " ".join(f"{p} 0 R" for p in page_ids)
        pages_id = add(f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>")
        assert pages_id == pages_obj_num
        catalog_id = add(f"<< /Type /Catalog /Pages {pages_id} 0 R >>")

        out = bytearray(b"%PDF-1.4\n")
        offsets = [0]
        for i, body in enumerate(objs, start=1):
            offsets.append(len(out))
            out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
        xref_at = len(out)
        out += f"xref\n0 {len(objs) + 1}\n".encode()
        out += b"0000000000 65535 f \n"
        for off in offsets[1:]:
            out += f"{off:010d} 00000 n \n".encode()
        out += (f"trailer\n<< /Size {len(objs) + 1} /Root {catalog_id} 0 R >>\n"
                f"startxref\n{xref_at}\n%%EOF\n").encode()
        return bytes(out)


def _fmt_money(v) -> str:
    return f"${v:,.2f}" if v is not None else "-"


def _fmt(v, spec=".2f", suffix="") -> str:
    return f"{v:{spec}}{suffix}" if v is not None else "-"


def build(trades: list[dict], positions: list[dict], stats: dict,
          portfolio: dict, meta: dict) -> bytes:
    """meta: {mode, style, regime_name, start_cash}"""
    pdf = _Pdf()
    y = MARGIN + 8

    pdf.text(MARGIN, y, "CryptoPilot - Trade Report", 19, "F2", (0.05, 0.1, 0.2)); y += 20
    pdf.text(MARGIN, y,
             f"Generated {time.strftime('%Y-%m-%d %H:%M %Z')}   |   "
             f"mode: {meta.get('mode', '?').upper()}   |   style: {meta.get('style', '?')}   |   "
             f"regime: {meta.get('regime_name', '?')}", 9, "F1", (0.35, 0.4, 0.45))
    y += 14
    pdf.hline(y); y += 18

    pdf.text(MARGIN, y, "Portfolio", 13, "F2"); y += 16
    p = portfolio
    for label, val in [
        ("Equity", _fmt_money(p.get("equity"))),
        ("Cash", _fmt_money(p.get("cash"))),
        ("Realized P&L", _fmt_money(p.get("realized_pnl"))),
        ("Unrealized P&L", _fmt_money(p.get("unrealized_pnl"))),
        ("Total P&L", f"{_fmt_money(p.get('total_pnl'))} ({_fmt(p.get('total_pnl_pct'), '+.2f', '%')})"),
    ]:
        pdf.text(MARGIN + 8, y, f"{label:<16}", 9, "F3", (0.35, 0.4, 0.45))
        pdf.text(MARGIN + 118, y, val, 9, "F4"); y += 13
    y += 8

    pdf.text(MARGIN, y, "Performance", 13, "F2"); y += 16
    s = stats
    pf = s.get("profit_factor")
    pf_txt = "-" if pf is None else ("inf" if pf == float("inf") else f"{pf:.2f}")
    for label, val in [
        ("Closed trades", str(s.get("closed_trades", 0))),
        ("Win rate", _fmt(s.get("win_rate"), ".0f", "%")),
        ("Profit factor", pf_txt),
        ("Expectancy", f"{_fmt_money(s.get('expectancy'))} / trade"),
        ("Avg win / loss", f"{_fmt_money(s.get('avg_win'))} / {_fmt_money(s.get('avg_loss'))}"),
        ("Max drawdown", _fmt(s.get("max_drawdown_pct"), ".1f", "%")),
        ("Sharpe (approx)", _fmt(s.get("sharpe_approx"), ".2f")),
        ("Avg hold", _fmt(s.get("avg_hold_hours"), ".1f", "h")),
        ("Fees paid", _fmt_money(s.get("total_fees"))),
    ]:
        pdf.text(MARGIN + 8, y, f"{label:<16}", 9, "F3", (0.35, 0.4, 0.45))
        pdf.text(MARGIN + 118, y, val, 9, "F4"); y += 13
    y += 8

    pdf.text(MARGIN, y, f"Open positions ({len(positions)})", 13, "F2"); y += 15
    hdr = f"{'SYM':<7}{'TF':<5}{'ENTRY':>11}{'NOW':>11}{'P&L%':>8}{'STOP':>11}{'TARGET':>11}{'PEAK%':>8}  HELD"
    pdf.text(MARGIN + 8, y, hdr, 7.5, "F4", (0.3, 0.35, 0.4)); y += 11
    now = time.time()
    for pos in positions:
        peak = (pos.get("high", pos["entry"]) / pos["entry"] - 1) * 100
        held_h = (now - pos.get("opened", now)) / 3600
        row = (f"{pos['symbol']:<7}{pos.get('tf', '15m'):<5}{pos['entry']:>11.5g}"
               f"{pos.get('price', pos['entry']):>11.5g}{pos.get('pnl_pct', 0):>+8.1f}"
               f"{pos['stop']:>11.5g}{pos['target']:>11.5g}{peak:>+8.1f}  {held_h:.0f}h")
        pdf.text(MARGIN + 8, y, row, 7.5, "F3"); y += 10.5
        if pos.get("reason"):
            pdf.text(MARGIN + 8, y, "  why: " + pos["reason"][:108], 6.8, "F1", (0.45, 0.5, 0.55))
            y += 10
    if not positions:
        pdf.text(MARGIN + 8, y, "(none)", 8, "F1", (0.5, 0.5, 0.5)); y += 12
    y += 8

    # ---- trade log, newest first, paginated ----
    pdf.text(MARGIN, y, f"Trade log ({len(trades)} most recent)", 13, "F2"); y += 15
    hdr = f"{'DATE':<12}{'TIME':<7}{'SIDE':<5}{'SYM':<7}{'PRICE':>11}{'VALUE':>11}{'P&L':>10}{'P&L%':>8}"
    pdf.text(MARGIN + 8, y, hdr, 7.5, "F4", (0.3, 0.35, 0.4)); y += 11

    for t in trades:
        if y > PAGE_H - MARGIN - 24:
            pdf.new_page()
            y = MARGIN + 8
            pdf.text(MARGIN + 8, y, hdr, 7.5, "F4", (0.3, 0.35, 0.4)); y += 11
        lt = time.localtime(t["ts"])
        pnl = t.get("pnl")
        pnl_s = "" if pnl is None else f"{pnl:+.2f}"
        pct = t.get("pnl_pct")
        pct_s = "" if pct is None else f"{pct:+.1f}"
        row = (f"{time.strftime('%Y-%m-%d', lt):<12}{time.strftime('%H:%M', lt):<7}"
               f"{t['side']:<5}{t['symbol']:<7}{t['price']:>11.6g}{t['value']:>11.2f}"
               f"{pnl_s:>10}{pct_s:>8}")
        color = (0.1, 0.1, 0.1)
        if pnl is not None:
            color = (0.05, 0.45, 0.25) if pnl > 0 else (0.65, 0.15, 0.15)
        pdf.text(MARGIN + 8, y, row, 7.5, "F3", color); y += 10
        if t.get("reason"):
            pdf.text(MARGIN + 8, y, "  " + t["reason"][:112], 6.6, "F1", (0.45, 0.5, 0.55))
            y += 9.5

    pdf.text(MARGIN, PAGE_H - 28,
             "CryptoPilot - experimental trading bot. Not financial advice.",
             7, "F1", (0.55, 0.6, 0.65))
    return pdf.render()
