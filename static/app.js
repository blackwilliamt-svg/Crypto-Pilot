/* CryptoPilot dashboard — polls /api/state and renders everything. */
const $ = (id) => document.getElementById(id);
let lastState = null;

const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function fmtMoney(n, sign = false) {
  const s = (sign && n > 0 ? "+" : "") + n.toLocaleString("en-US",
    { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return "$" + s.replace("$", "");
}
function fmtPrice(p) {
  const d = p >= 100 ? 2 : p >= 1 ? 3 : 5;
  return "$" + p.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
}
function fmtPct(p, sign = true) {
  return (sign && p > 0 ? "+" : "") + p.toFixed(2) + "%";
}
function fmtQty(q) {
  return q >= 100 ? q.toFixed(2) : q >= 1 ? q.toFixed(4) : q.toFixed(6);
}
function age(ts) {
  const m = Math.max(0, (Date.now() / 1000 - ts) / 60);
  if (m < 60) return Math.round(m) + "m ago";
  if (m < 1440) return Math.round(m / 60) + "h ago";
  return Math.round(m / 1440) + "d ago";
}
function pnlClass(v) { return v >= 0 ? "pos" : "neg"; }

function scoreBar(score) {
  const pct = Math.min(100, Math.abs(score)) / 2; // half-track each side of center
  const color = score >= 0 ? "var(--green)" : "var(--red)";
  const style = score >= 0
    ? `left:50%;width:${pct}%;background:${color}`
    : `right:50%;width:${pct}%;background:${color}`;
  return `<div class="scorebar"><div class="track"><div class="fill" style="${style}"></div></div>
    <span class="num ${pnlClass(score)}">${score >= 0 ? "+" : ""}${Math.round(score)}</span></div>`;
}

/* ---------- render sections ---------- */

function renderHeader(st) {
  const pill = $("status-pill");
  pill.className = "status-pill " + st.status;
  $("status-text").textContent = st.status.toUpperCase();
  $("btn-pause").textContent = st.paused ? "Resume" : "Pause";

  const live = st.mode === "live";
  const badge = $("mode-badge");
  badge.textContent = live ? "LIVE TRADING" : "PAPER TRADING";
  badge.className = "paper-badge" + (live ? " live" : "");
  $("btn-mode").textContent = live ? "Back to Paper" : "Go Live";
  $("footer-note").textContent = live
    ? "LIVE TRADING — real orders are being sent to Kraken with real funds. Signals are experimental and not financial advice."
    : "Paper trading — no real funds are used. Market data: Kraken public API · Headlines: public RSS feeds. Signals are experimental and not financial advice.";

  const sel = $("style-select");
  if (st.style && document.activeElement !== sel) sel.value = st.style;

  if (st.portfolio) {
    $("h-equity").textContent = fmtMoney(st.portfolio.equity);
    const pnl = st.portfolio.total_pnl;
    const el = $("h-pnl");
    el.textContent = `${fmtMoney(pnl, true)} (${fmtPct(st.portfolio.total_pnl_pct)})`;
    el.className = "hstat-value " + pnlClass(pnl);
  }
}

function renderSummary(st) {
  const s = st.summary;
  if (!s) return;
  const colors = { bullish: "var(--green)", bearish: "var(--red)", mixed: "var(--amber)" };
  $("summary-bias").innerHTML =
    `<span class="bias-dot" style="background:${colors[s.bias]}"></span>
     Market read: <span style="color:${colors[s.bias]}">${esc(s.bias.toUpperCase())}</span>
     <span class="muted">(avg signal ${s.bias_score >= 0 ? "+" : ""}${Math.round(s.bias_score)})</span>`;
  $("summary-time").textContent = "updated " + age(s.generated_at);
  $("summary-lines").innerHTML = s.lines.map((l) => `<li>${esc(l)}</li>`).join("");
}

function renderPortfolio(st) {
  const p = st.portfolio;
  if (!p) return;
  $("port-stats").innerHTML = `
    <div class="pstat"><div class="l">Cash</div><div class="v">${fmtMoney(p.cash)}</div></div>
    <div class="pstat"><div class="l">In positions</div><div class="v">${fmtMoney(p.positions_value)}</div></div>
    <div class="pstat"><div class="l">Realized P&L</div><div class="v ${pnlClass(p.realized_pnl)}">${fmtMoney(p.realized_pnl, true)}</div></div>
    <div class="pstat"><div class="l">Unrealized P&L</div><div class="v ${pnlClass(p.unrealized_pnl)}">${fmtMoney(p.unrealized_pnl, true)}</div></div>`;

  const tbody = $("positions-table").querySelector("tbody");
  tbody.innerHTML = (st.positions || []).map((pos) => `
    <tr>
      <td><b>${esc(pos.symbol)}</b></td>
      <td>${fmtQty(pos.qty)}</td>
      <td>${fmtPrice(pos.entry)}</td>
      <td>${fmtPrice(pos.price)}</td>
      <td>${fmtMoney(pos.value)}</td>
      <td class="${pnlClass(pos.pnl)}">${fmtMoney(pos.pnl, true)} (${fmtPct(pos.pnl_pct)})</td>
      <td class="muted">${fmtPrice(pos.stop)} / ${fmtPrice(pos.target)}</td>
      <td><button class="btn btn-close" data-close="${esc(pos.symbol)}">Close</button></td>
    </tr>`).join("");
  tbody.querySelectorAll("[data-close]").forEach((btn) =>
    btn.addEventListener("click", () => closePosition(btn.dataset.close)));
  $("positions-empty").classList.toggle("hidden", (st.positions || []).length > 0);
  drawSpark(p.equity_history || [], p.start_cash);
}

async function closePosition(sym) {
  if (!confirm(`Close the ${sym} position at the current market price?`)) return;
  const r = await fetch(`/api/position/${sym}/close`, { method: "POST" });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    alert(`Could not close ${sym}: ${err.detail || r.statusText}`);
  }
  poll();
}

function renderSignals(st) {
  const tbody = $("signals-table").querySelector("tbody");
  tbody.innerHTML = (st.signals || []).map((s) => `
    <tr data-sym="${esc(s.symbol)}">
      <td><div class="coin-cell"><span class="coin-sym">${esc(s.symbol)}</span>
          <span class="coin-name">${esc(s.name)}</span></div></td>
      <td>${fmtPrice(s.price)}</td>
      <td class="${pnlClass(s.change24h)}">${fmtPct(s.change24h)}</td>
      <td>${scoreBar(s.ta)}</td>
      <td>${scoreBar(s.news)}<span class="muted" style="font-size:10px">${s.news_count} stories</span></td>
      <td><b class="${pnlClass(s.total)}" style="font-size:15px">${s.total >= 0 ? "+" : ""}${Math.round(s.total)}</b></td>
      <td><span class="badge ${s.label.replace(" ", "_")}">${esc(s.label)}</span></td>
      <td class="action-cell">${esc(s.action)}</td>
      <td class="factors">${esc((s.reasons || []).slice(0, 2).join(" · "))}</td>
    </tr>`).join("");
  tbody.querySelectorAll("tr").forEach((tr) =>
    tr.addEventListener("click", () => openChart(tr.dataset.sym)));
}

function renderTrades(st) {
  const trades = st.trades || [];
  const tbody = $("trades-table").querySelector("tbody");
  tbody.innerHTML = trades.map((t) => `
    <tr title="${esc(t.reason)}">
      <td class="muted">${new Date(t.ts * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}</td>
      <td><span class="badge side-${esc(t.side)}">${esc(t.side)}</span></td>
      <td><b>${esc(t.symbol)}</b></td>
      <td>${fmtPrice(t.price)}</td>
      <td>${fmtMoney(t.value)}</td>
      <td class="${t.pnl == null ? "muted" : pnlClass(t.pnl)}">
        ${t.pnl == null ? "—" : fmtMoney(t.pnl, true) + " (" + fmtPct(t.pnl_pct) + ")"}</td>
    </tr>`).join("");
  $("trades-empty").classList.toggle("hidden", trades.length > 0);
}

function renderHeadlines(st) {
  const hs = st.headlines || [];
  $("news-count").textContent = hs.length + " scanned";
  $("headlines").innerHTML = hs.map((h) => {
    const chip = h.label === "neutral" ? "·" : (h.sentiment > 0 ? "+" : "") + Math.round(h.sentiment);
    return `<div class="headline">
      <div class="sent-chip ${esc(h.label)}">${chip}</div>
      <div>
        <a href="${esc(h.link)}" target="_blank" rel="noopener">${esc(h.title)}</a>
        <div class="meta"><span>${esc(h.source)}</span><span>${age(h.ts)}</span>
          ${(h.coins || []).map((c) => `<span class="cointag">${esc(c)}</span>`).join("")}</div>
      </div>
    </div>`;
  }).join("");
}

/* ---------- equity sparkline ---------- */

function drawSpark(hist, startCash) {
  const cv = $("spark"), ctx = cv.getContext("2d");
  ctx.clearRect(0, 0, cv.width, cv.height);
  if (hist.length < 2) return;
  const vals = hist.map((h) => h[1]);
  const min = Math.min(...vals, startCash), max = Math.max(...vals, startCash);
  const pad = (max - min) * 0.1 || 1;
  const y = (v) => cv.height - 4 - ((v - min + pad) / (max - min + 2 * pad)) * (cv.height - 8);
  const x = (i) => 2 + (i / (vals.length - 1)) * (cv.width - 4);

  ctx.strokeStyle = "#2c3a52"; ctx.setLineDash([3, 3]);
  ctx.beginPath(); ctx.moveTo(0, y(startCash)); ctx.lineTo(cv.width, y(startCash)); ctx.stroke();
  ctx.setLineDash([]);

  const up = vals[vals.length - 1] >= startCash;
  ctx.strokeStyle = up ? "#34d399" : "#f87171"; ctx.lineWidth = 1.6;
  ctx.beginPath();
  vals.forEach((v, i) => (i ? ctx.lineTo(x(i), y(v)) : ctx.moveTo(x(i), y(v))));
  ctx.stroke();
}

/* ---------- coin chart modal ---------- */

async function openChart(sym) {
  let data;
  try {
    const r = await fetch("/api/coin/" + sym);
    if (!r.ok) return;
    data = await r.json();
  } catch { return; }

  const sig = (lastState?.signals || []).find((s) => s.symbol === sym);
  $("modal-title").textContent = `${data.name} (${sym}) — 7d hourly`;
  $("modal-chips").innerHTML = sig ? [
    `RSI ${Math.round(sig.rsi)}`,
    `TA ${sig.ta >= 0 ? "+" : ""}${Math.round(sig.ta)}`,
    `News ${sig.news >= 0 ? "+" : ""}${Math.round(sig.news)}`,
    `Combined ${sig.total >= 0 ? "+" : ""}${Math.round(sig.total)}`,
    sig.label,
  ].map((c) => `<span class="chip">${esc(c)}</span>`).join("") : "";
  $("modal-reasons").innerHTML = sig
    ? sig.reasons.map((r) => `<li>${esc(r)}</li>`).join("") : "";
  $("modal").classList.remove("hidden");
  drawChart(data);
}

function drawChart(d) {
  const cv = $("chart"), ctx = cv.getContext("2d");
  const W = cv.width, H = cv.height, padL = 64, padR = 12, padT = 14, padB = 26;
  ctx.clearRect(0, 0, W, H);

  const closes = d.candles.map((c) => c.c);
  const lows = d.candles.map((c) => c.l), highs = d.candles.map((c) => c.h);
  const bbU = d.bb_upper.filter((v) => v != null), bbL = d.bb_lower.filter((v) => v != null);
  const min = Math.min(...lows, ...(bbL.length ? bbL : lows));
  const max = Math.max(...highs, ...(bbU.length ? bbU : highs));
  const n = closes.length;
  const x = (i) => padL + (i / (n - 1)) * (W - padL - padR);
  const y = (v) => padT + (1 - (v - min) / (max - min || 1)) * (H - padT - padB);

  ctx.font = "11px Segoe UI"; ctx.fillStyle = "#8b98ab"; ctx.strokeStyle = "#1a2333";
  for (let g = 0; g <= 4; g++) {
    const v = min + ((max - min) * g) / 4, yy = y(v);
    ctx.beginPath(); ctx.moveTo(padL, yy); ctx.lineTo(W - padR, yy); ctx.stroke();
    ctx.fillText(v >= 100 ? v.toLocaleString("en-US", { maximumFractionDigits: 0 }) : v.toPrecision(4), 6, yy + 4);
  }
  for (let g = 0; g <= 3; g++) {
    const i = Math.round(((n - 1) * g) / 3);
    const dt = new Date(d.candles[i].t * 1000);
    ctx.fillText(dt.toLocaleDateString([], { month: "short", day: "numeric" }), x(i) - 18, H - 8);
  }

  // Bollinger band fill
  ctx.beginPath();
  let started = false;
  d.bb_upper.forEach((v, i) => { if (v != null) { started ? ctx.lineTo(x(i), y(v)) : ctx.moveTo(x(i), y(v)); started = true; } });
  for (let i = n - 1; i >= 0; i--) if (d.bb_lower[i] != null) ctx.lineTo(x(i), y(d.bb_lower[i]));
  ctx.closePath(); ctx.fillStyle = "rgba(56,189,248,.08)"; ctx.fill();

  const line = (vals, color, width = 1.4) => {
    ctx.strokeStyle = color; ctx.lineWidth = width; ctx.beginPath();
    let first = true;
    vals.forEach((v, i) => { if (v == null) return; first ? ctx.moveTo(x(i), y(v)) : ctx.lineTo(x(i), y(v)); first = false; });
    ctx.stroke();
  };
  line(d.ema50, "#fbbf24", 1.2);
  line(d.ema20, "#a78bfa", 1.2);
  line(closes, "#38bdf8", 1.8);

  // trade markers
  const t0 = d.candles[0].t, t1 = d.candles[n - 1].t;
  (d.trades || []).forEach((t) => {
    const i = Math.round(((t.ts - t0) / (t1 - t0 || 1)) * (n - 1));
    if (i < 0 || i > n - 1) return;
    const xx = x(i), yy = y(t.price);
    ctx.beginPath();
    if (t.side === "BUY") {
      ctx.fillStyle = "#34d399";
      ctx.moveTo(xx, yy - 12); ctx.lineTo(xx - 6, yy - 2); ctx.lineTo(xx + 6, yy - 2);
    } else {
      ctx.fillStyle = "#f87171";
      ctx.moveTo(xx, yy + 12); ctx.lineTo(xx - 6, yy + 2); ctx.lineTo(xx + 6, yy + 2);
    }
    ctx.closePath(); ctx.fill();
  });
}

/* ---------- polling + controls ---------- */

async function poll() {
  try {
    const r = await fetch("/api/state");
    const st = await r.json();
    lastState = st;
    renderHeader(st);
    $("error-banner").classList.toggle("hidden", !st.error);
    if (st.error) $("error-banner").textContent = "Data issue: " + st.error + " — retrying automatically.";
    if (st.status === "starting") return;
    $("loading").classList.add("hidden");
    renderSummary(st);
    renderPortfolio(st);
    renderSignals(st);
    renderTrades(st);
    renderHeadlines(st);
  } catch {
    $("status-pill").className = "status-pill offline";
    $("status-text").textContent = "OFFLINE";
  }
}

$("btn-pause").addEventListener("click", async () => {
  await fetch("/api/bot/toggle", { method: "POST" });
  poll();
});
$("style-select").addEventListener("change", async (e) => {
  const r = await fetch("/api/style", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ style: e.target.value }),
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    alert("Could not switch style: " + (err.detail || r.statusText));
  }
  poll();
});
$("btn-mode").addEventListener("click", async () => {
  const goingLive = (lastState?.mode || "paper") === "paper";
  let body;
  if (goingLive) {
    const typed = prompt(
      "You are about to switch to LIVE TRADING.\n\n" +
      "Real market orders will be sent to Kraken using real funds from your account. " +
      "All open paper positions must be closed first, and KRAKEN_API_KEY / KRAKEN_API_SECRET " +
      "must be set in the bot's environment.\n\n" +
      "Type GO LIVE to confirm:");
    if (typed === null) return;
    body = { mode: "live", confirm: typed };
  } else {
    if (!confirm("Switch back to paper trading? (Close any open live positions first.)")) return;
    body = { mode: "paper" };
  }
  const r = await fetch("/api/mode", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    alert("Mode switch failed: " + (err.detail || r.statusText));
  }
  poll();
});
$("btn-reset").addEventListener("click", async () => {
  if (!confirm("Reset the paper portfolio back to $10,000 and clear all trades?")) return;
  await fetch("/api/reset", { method: "POST" });
  poll();
});
$("modal-close").addEventListener("click", () => $("modal").classList.add("hidden"));
$("modal").addEventListener("click", (e) => {
  if (e.target === $("modal")) $("modal").classList.add("hidden");
});

poll();
setInterval(poll, 5000);
