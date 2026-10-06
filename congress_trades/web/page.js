
const DATA = __DATA__, STATS = __STATS__;
const M = DATA.members, ROWS = DATA.rows, URLS = DATA.urls;
// row: [memberIdx, ticker, type, date, amtMin, amtRange, owner, asset, urlIdx]
const R_M=0,R_TICK=1,R_TYPE=2,R_DATE=3,R_AMT=4,R_RANGE=5,R_OWN=6,R_ASSET=7,R_URL=8,R_DISC=9;
const SECTORS = DATA.sectorNames, TICKERS = DATA.tickers;
const COMMITTEES = DATA.committeeNames;
const sectorOf = t => { const e = TICKERS[t]; return e ? SECTORS[e[0]] : "Unclassified"; };
const companyOf = t => { const e = TICKERS[t]; return e ? e[1] : ""; };
// Full name on hover: SEC's registered name, else the description filed most often.
const NAMES = DATA.names || {};
const tkName = t => NAMES[t] || companyOf(t);
const tk = t => `<b title="${esc(tkName(t))}">${esc(t)}</b>`;
// A member's name in their party's colour; unknown party leaves it as it was.
const BY_NAME = new Map(M.map(m => [m.n, m]));
const fmtUSD = n => fmtMoney(Math.round(n || 0));

const $ = s => document.querySelector(s);
const fmtMoney = n => n >= 1e6 ? "$"+(n/1e6).toFixed(1).replace(/\.0$/,"")+"M"
                    : n >= 1e3 ? "$"+Math.round(n/1e3)+"k" : "$"+n;

$("#stats").innerHTML = [
  [STATS.trades.toLocaleString(), "disclosed trades"],
  [STATS.members, "members"],
  [STATS.house.toLocaleString(), "House"],
  [STATS.senate.toLocaleString(), "Senate"],
  [STATS.span.replace(" to ", " → "), "coverage"],
  ['<span id="stat-shown">—</span>', "shown at this filter"],
  [STATS.updated, "last updated"],
].map(([b,s]) => `<div class="stat"><b>${b}</b><span>${s}</span></div>`).join("");

let selected = null;

function cutoff() {
  const d = +$("#window").value;
  if (!d) return "";
  const t = new Date(); t.setDate(t.getDate() - d);
  return t.toISOString().slice(0,10);
}

const floorVal = () => +$("#floor").value || 0;

function activeRows() {
  const since = cutoff(), ch = $("#chamber").value, fl = floorVal();
  return ROWS.filter(r => (!since || r[R_DATE] >= since)
                       && (!ch || M[r[R_M]].c === ch)
                       && r[R_AMT] >= fl);
}

function renderRoster() {
  const q = $("#q").value.trim().toLowerCase();
  const rows = activeRows();
  const counts = new Map();
  for (const r of rows) counts.set(r[R_M], (counts.get(r[R_M])||0) + 1);
  let list = [...counts.entries()].map(([mi,n]) => ({mi, n, m:M[mi]}));
  if (q) {
    const tickHit = new Set(rows.filter(r => r[R_TICK].toLowerCase().includes(q))
                                .map(r => r[R_M]));
    list = list.filter(x => x.m.n.toLowerCase().includes(q)
                         || x.m.full.toLowerCase().includes(q) || tickHit.has(x.mi));
  }
  list.sort((a,b) => b.n - a.n || a.m.full.localeCompare(b.m.full));
  $("#roster").innerHTML = list.length ? list.map(x =>
    `<button role="listitem" data-mi="${x.mi}" aria-current="${x.mi===selected}">
       <span class="who"><span class="${pc(x.m.party)}">${esc(x.m.full)}</span><i>${[x.m.party, x.m.c, x.m.s]
           .filter(Boolean).map(esc).join(" · ")}</i></span>
       <span class="n">${x.n}</span></button>`).join("")
    : `<p class="empty">No members match.</p>`;
  if (list.length && !list.some(x => x.mi === selected)) select(list[0].mi);
  else if (!list.length) { selected = null; $("#detail").innerHTML =
    `<p class="empty">Nothing to show for this filter.</p>`; }
}


let pick = null;       // {k: "2025-03", t: "buy"} from a click on the member's chart

function select(mi) {
  selected = mi;
  pick = null;
  document.querySelectorAll("#roster button").forEach(b =>
    b.setAttribute("aria-current", +b.dataset.mi === mi));
  renderDetail();
}

function renderDetail() {
  if (selected == null) return;
  const m = M[selected];
  // A ticker search scopes the timeline to that ticker; a name search shows everything.
  const q = $("#q").value.trim().toLowerCase();
  const byName = q && (m.n.toLowerCase().includes(q) || m.full.toLowerCase().includes(q));
  let rows = activeRows().filter(r => r[R_M] === selected);
  if (q && !byName) rows = rows.filter(r => r[R_TICK].toLowerCase().includes(q));
  rows.sort((a,b) => b[R_DATE].localeCompare(a[R_DATE]));
  const buys = rows.filter(r => r[R_TYPE]==="buy").length;
  const sells = rows.length - buys;
  const top = fmtMoney(Math.max(0, ...rows.map(r => r[R_AMT])));
  // most-traded tickers, for the "what have they actually been in" line
  const tally = new Map();
  for (const r of rows) if (r[R_TICK]) tally.set(r[R_TICK], (tally.get(r[R_TICK])||0)+1);
  const topTicks = [...tally.entries()].sort((a,b) => b[1]-a[1] || a[0].localeCompare(b[0]))
                    .slice(0,6);
  const partyName = {D:"Democrat", R:"Republican", I:"Independent"}[m.party] || m.party;
  const chips = [
    m.party ? `<span class="chip p-${m.party}">${m.party} · ${partyName}</span>` : "",
    m.c ? `<span class="chip">${esc(m.c)}</span>` : "",
    m.s ? `<span class="chip">${esc(m.s)}</span>` : "",
    m.cur ? "" : `<span class="chip former">former member</span>`,
  ].filter(Boolean).join("");
  const links = [
    m.wurl ? `<a href="${esc(m.wurl)}" target="_blank" rel="noopener">Wikipedia</a>` : "",
    m.ourl ? `<a href="${esc(m.ourl)}" target="_blank" rel="noopener">Official site</a>` : "",
  ].filter(Boolean).join("");

  $("#detail").innerHTML = `
    <div class="profile">
      ${m.thumb ? `<img src="${esc(m.thumb)}" alt="" loading="lazy">` : ""}
      <div class="who">
        <h2 class="${pc(m.party)}">${esc(m.full)}</h2>
        <div class="chips">${chips}</div>
        ${m.desc ? `<p class="meta" style="margin:.4rem 0 0">${esc(m.desc)}</p>` : ""}
        ${m.bio ? `<p class="bio">${esc(m.bio)}</p>` : ""}
        ${links ? `<p class="links">${links}</p>` : ""}
      </div>
    </div>
    ${recordBlock(m, rows, buys, sells, top)}
    ${beyondBlock(m)}
    ${topTicks.length ? `<p class="toptick">Most traded: ${topTicks
        .map(([t,n]) => `${tk(t)} (${n})`).join(" · ")}</p>` : ""}
    ${committeeBlock(m, rows)}
    ${rows.length ? chartSVG(rows) : ""}
    ${pick ? `<div class="pick">Showing ${esc(pick.t)}s in ${esc(pick.k)}
      <button id="unpick">show all</button></div>` : ""}
    <div class="tbl-scroll"><table>
      <thead><tr><th>Date</th><th>Ticker</th><th>Type</th><th>Amount</th>
        <th>Owner</th><th>Asset</th><th>Filing</th></tr></thead>
      <tbody>${(pick ? rows.filter(r => r[R_DATE].startsWith(pick.k) && r[R_TYPE] === pick.t)
                     : rows).map(r => `<tr>
        <td class="num">${r[R_DATE]}</td>
        <td class="num">${r[R_TICK] ? tk(r[R_TICK]) : "—"}</td>
        <td><span class="tag ${r[R_TYPE]}">${r[R_TYPE].toUpperCase()}</span>${
          isLate(r) ? `<span class="tag late" title="filed ${isLate(r)} days after the trade"
            >+${isLate(r)}d</span>` : ""}</td>
        <td class="num">${esc(r[R_RANGE]||"—")}</td>
        <td>${esc(r[R_OWN])}</td>
        <td>${esc(r[R_ASSET])}</td>
        <td>${URLS[r[R_URL]] ? `<a href="${esc(URLS[r[R_URL]])}" target="_blank"
              rel="noopener">PDF</a>` : "—"}</td></tr>`).join("")}
      </tbody></table></div>`;
  wireChart("#detail", (k, t) => { pick = {k, t}; renderDetail(); });
  const un = $("#unpick");
  if (un) un.onclick = () => { pick = null; renderDetail(); };
}

/* The disclosure streams beyond trades, each from the report that owns its rule. */
function beyondBlock(m) {
  const out = [];
  if (m.late) {
    const [n, total, worst, share] = m.late;
    out.push(n ? `Filed <b>${n}</b> of ${total} trades past the STOCK Act's 45-day
      deadline (${Math.round(share*100)}%), the latest ${worst} days after the trade.`
      : `Never filed late across ${total} trades.`);
  }
  if (m.pac) {
    const [dollars, total, sectors, trades] = m.pac;
    out.push(`Took <b>${fmtUSD(dollars)}</b> from PACs in sectors their committees
      oversee (${esc(sectors.join(", "))}), of ${fmtUSD(total)} in PAC money; made
      ${trades} trades in those sectors.`);
  }
  const a = m.ann;
  if (a) {
    if (a.debts) out.push(`Annual report: <b>${a.debts}</b> liabilities, at least ${fmtUSD(a.debt_min)}.`);
    if (a.income) out.push(`Outside earned income: <b>${fmtUSD(a.income)}</b>.`);
    if ((a.positions||[]).length) out.push(`Positions held: ${a.positions.map(esc).join("; ")}.`);
  }
  if (!out.length) return "";
  return `<div class="beyond"><p class="meta" style="margin:.8rem 0 .2rem;color:var(--white)">
      <b>Beyond trades</b></p>${out.map(x => `<p class="meta" style="margin:.15rem 0">${x}</p>`).join("")}
    <p class="meta" style="color:var(--ink-3);font-size:12px">Late filings, PAC money and
      annual reports come from their own reports; annual reports cover only the filings
      fetched so far.</p></div>`;
}

/* Monthly buy/sell activity. Diverging: buys above the zero line, sells below —
   two poles of one measure, so one axis, never two scales. */
function chartSVG(rows) {
  const by = new Map();
  for (const r of rows) {
    const k = r[R_DATE].slice(0,7);
    const o = by.get(k) || {buy:0, sell:0};
    o[r[R_TYPE]] = (o[r[R_TYPE]]||0) + 1;
    by.set(k, o);
  }
  const keys = [...by.keys()].sort();
  // fill month gaps so time reads linearly
  const months = [];
  if (keys.length) {
    let [y, mo] = keys[0].split("-").map(Number);
    const [ey, emo] = keys[keys.length-1].split("-").map(Number);
    // cap the span: one bad date in a filing must never spray thousands of bars
    while ((y < ey || (y === ey && mo <= emo)) && months.length < 180) {
      months.push(`${y}-${String(mo).padStart(2,"0")}`);
      if (++mo > 12) { mo = 1; y++; }
    }
  }
  const max = Math.max(1, ...months.map(k => Math.max(by.get(k)?.buy||0, by.get(k)?.sell||0)));
  const bw = Math.max(14, Math.min(38, Math.floor(760 / Math.max(months.length,1))));
  const W = Math.max(320, months.length * bw + 46), H = 168, mid = 84, unit = 60 / max;
  const bars = months.map((k, i) => {
    const d = by.get(k) || {buy:0, sell:0};
    const x = 40 + i * bw + 1;
    const w = bw - 4;                     // 2px surface gap between adjacent bars
    let s = "";
    if (d.buy)  { const h = Math.max(3, d.buy*unit);
      s += `<rect class="bar" x="${x}" y="${mid-h}" width="${w}" height="${h}" rx="3"
             fill="var(--buy)" data-t="buy" data-k="${k}" data-b="${d.buy}" data-s="${d.sell}"/>`; }
    if (d.sell) { const h = Math.max(3, d.sell*unit);
      s += `<rect class="bar" x="${x}" y="${mid+1}" width="${w}" height="${h}" rx="3"
             fill="var(--sell)" data-t="sell" data-k="${k}" data-b="${d.buy}" data-s="${d.sell}"/>`; }
    const showLbl = months.length <= 14 || i % Math.ceil(months.length/12) === 0;
    if (showLbl) s += `<text x="${x + w/2}" y="${H-4}" text-anchor="middle"
                        transform="rotate(-38 ${x+w/2} ${H-4})">${k.slice(2)}</text>`;
    return s;
  }).join("");
  return `<div class="legend">
      <span><i style="background:var(--buy)"></i>Buys</span>
      <span><i style="background:var(--sell)"></i>Sells</span>
      <span style="color:var(--ink-3)">trades per month &middot; click a bar to list them</span></div>
    <div class="chart-wrap"><svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}"
        role="img" aria-label="Monthly buy and sell disclosures; full detail in the table below">
      <line class="zero" x1="34" y1="${mid}" x2="${W-4}" y2="${mid}"/>
      <text x="30" y="${mid-52}" text-anchor="end">${max}</text>
      <text x="30" y="${mid+3}" text-anchor="end">0</text>
      <text x="30" y="${mid+60}" text-anchor="end">${max}</text>
      ${bars}</svg></div>`;
}

function wireChart(scope, onPick) {
  const tip = $("#tip");
  document.querySelectorAll(scope + " .bar").forEach(el => {
    el.addEventListener("click", () => { tip.style.opacity = 0; onPick(el.dataset.k, el.dataset.t); });
    el.addEventListener("mousemove", e => {
      tip.innerHTML = `<b>${el.dataset.k}</b><br>${el.dataset.b} buys · ${el.dataset.s} sells`;
      tip.style.opacity = 1;
      tip.style.left = Math.min(e.clientX + 12, innerWidth - 150) + "px";
      tip.style.top = (e.clientY - 44) + "px";
    });
    el.addEventListener("mouseleave", () => tip.style.opacity = 0);
  });
}

/* Filing lag. The STOCK Act gives members 45 days from the transaction to file a
   PTR, so anything past that is a late disclosure -- a plain, checkable fact rather
   than an inference. Median is used because a handful of years-late filings would
   drag a mean somewhere useless. */
/* Party unity is computed from Voteview's raw roll calls: the share of a member's
   yea/nay votes matching their own party's majority. The meter is scaled 75-100%
   because essentially every member lands in that band -- a 0-100 axis would render
   every bar nearly full and show nothing. */
/* The portal panel's layout: label / value pairs. Counts follow the filters above;
   the scoreboard figures are whole history at the default floor, said so in place. */
function recordBlock(m, rows, buys, sells, top) {
  const sc = SCORE.find(x => x.n === m.n), ls = lagStats(rows);
  const sg = x => x == null ? "—" : `<span class="${x > 0 ? "up" : x < 0 ? "down" : ""}">${
    x > 0 ? "+" : ""}${(x * 100).toFixed(1)}%</span>`;
  const lo = 75, w = m.unity == null ? 0 : Math.max(0, Math.min(100, (m.unity - lo) / (100 - lo) * 100));
  return `<div class="kv">
    <b>disclosed trades</b><span>${rows.length.toLocaleString()} · ${buys} buys / ${sells} sells${
      m.n !== m.full ? ` <span style="color:var(--ink-3)">· files as “${esc(m.n)}”</span>` : ""}</span>
    <b>largest bracket</b><span>from ${top}</span>
    ${sc ? `<b>alpha vs index</b><span>${sg(sc.med)} median over ${sc.t} scored trades
             <span style="color:var(--ink-3)">· all history</span></span>
           <b>vs sector</b><span>${sg(sc.smed)}</span>
           <b>beat index</b><span>${Math.round(sc.beat * 100)}% of trades</span>`
         : `<b>alpha</b><span style="color:var(--ink-3)">not scored: fewer than ${DATA.scoreMin}
             measurable trades</span>`}
    ${ls ? `<b>filing lag</b><span>median ${ls.median} days · ${ls.late
        ? `<span class="lateflag">${ls.late} of ${ls.n} (${ls.pct}%) past the ${LATE_DAYS}-day deadline</span>`
        : `never past the ${LATE_DAYS}-day deadline`}</span>` : ""}
    ${m.unity != null ? `<b>votes with party</b><span>${m.unity}%
        <span class="meter" title="${m.unity}% (scale ${lo}-100%)"><i style="width:${w}%;
          background:${m.party === "D" ? "var(--dem)" : m.party === "R" ? "var(--rep)" : "var(--ink-2)"}"></i></span>
        <span style="color:var(--ink-3)">${(m.nvotes || 0).toLocaleString()} roll calls</span></span>` : ""}
    ${m.nom != null ? `<b>DW-NOMINATE</b><span>${m.nom > 0 ? "+" : ""}${m.nom.toFixed(2)}</span>` : ""}
  </div>`;
}

const LATE_DAYS = __LATE_DAYS__;      // compliance.STATUTORY_DAYS, set at publish
const DAY = 864e5;

function lagStats(rows) {
  const lags = [];
  let late = 0;
  for (const r of rows) {
    if (!r[R_DISC] || !r[R_DATE]) continue;
    const d = Math.round((Date.parse(r[R_DISC]) - Date.parse(r[R_DATE])) / DAY);
    if (!isFinite(d) || d < 0) continue;          // a few filings carry bad dates
    lags.push(d);
    if (d > LATE_DAYS) late++;
  }
  if (!lags.length) return null;
  lags.sort((a,b) => a - b);
  const mid = Math.floor(lags.length / 2);
  const median = lags.length % 2 ? lags[mid] : Math.round((lags[mid-1] + lags[mid]) / 2);
  return {median, late, n: lags.length, pct: Math.round(late / lags.length * 100)};
}

function isLate(r) {
  if (!r[R_DISC] || !r[R_DATE]) return 0;
  const d = Math.round((Date.parse(r[R_DISC]) - Date.parse(r[R_DATE])) / DAY);
  return isFinite(d) && d > LATE_DAYS ? d : 0;
}

/* Committee seats, plus the overlap between what a member oversees and what they
   traded. The overlap is a prompt to look, not a finding: jurisdiction here is a
   keyword heuristic, and sitting on a committee is not evidence of anything. */
function committeeBlock(m, rows) {
  const seats = m.comm || [];
  if (!seats.length && !m.nsub) return "";
  const list = seats.map(([i, title]) => `<li>${esc(COMMITTEES[i])}${
      title ? ` — <span class="chair">${esc(title)}</span>` : ""}</li>`).join("");

  let overlap = "";
  if ((m.csec || []).length) {
    const covered = new Set(m.csec);
    const bySector = new Map();
    for (const r of rows) {
      if (!r[R_TICK]) continue;
      const sec = sectorOf(r[R_TICK]);
      if (!covered.has(sec)) continue;
      const o = bySector.get(sec) || {n: 0, ticks: new Set()};
      o.n++; o.ticks.add(r[R_TICK]);
      bySector.set(sec, o);
    }
    const hits = [...bySector.entries()].sort((a,b) => b[1].n - a[1].n);
    if (hits.length) {
      overlap = `<div class="overlap">
        <h4>Traded in sectors their committees cover</h4>
        <ul>${hits.map(([sec, o]) => `<li>${esc(sec)} —
          <span class="why">${o.n} ${o.n===1?"trade":"trades"}
          (${[...o.ticks].slice(0,5).map(esc).join(", ")})</span></li>`).join("")}</ul>
        <p class="note">Jurisdiction is matched by committee name, not official rules,
           and committee service is not itself evidence of anything. Starting point
           for a look, not a conclusion.</p></div>`;
    }
  }
  return `<div class="comm"><h4>Committees</h4><ul>${list}</ul>${
      m.nsub ? `<p class="toptick">+ ${m.nsub} subcommittee${m.nsub===1?"":"s"}</p>` : ""
    }${overlap}</div>`;
}

/* ---- Movers -------------------------------------------------------------------
   Ranked by how many DISTINCT members traded a ticker, not by dollars: several
   members independently landing on the same mid-cap is a far stronger signal than
   one large index buy. Dollar bracket breaks ties.
   Windowed on DISCLOSURE date, because that is when a move actually became public
   -- trades themselves are up to 45 days older than their filing.              */
function moverRows() {
  const since = cutoff(), ch = $("#chamber").value, q = $("#q").value.trim().toLowerCase();
  const fl = floorVal();
  const rows = ROWS.filter(r => r[R_TICK] && r[R_AMT] >= fl
      && (!since || (r[R_DISC] || r[R_DATE]) >= since)
      && (!ch || M[r[R_M]].c === ch)
      && (!q || r[R_TICK].toLowerCase().includes(q)
             || sectorOf(r[R_TICK]).toLowerCase().includes(q)
             || companyOf(r[R_TICK]).toLowerCase().includes(q)));
  const by = new Map();
  for (const r of rows) {
    const t = r[R_TICK];
    let o = by.get(t);
    if (!o) { o = {t, buys:0, sells:0, members:new Set(), max:0, last:""}; by.set(t, o); }
    r[R_TYPE] === "buy" ? o.buys++ : o.sells++;
    o.members.add(r[R_M]);
    o.max = Math.max(o.max, r[R_AMT]);
    const d = r[R_DISC] || r[R_DATE];
    if (d > o.last) o.last = d;
  }
  return [...by.values()]
    .map(o => ({...o, n: o.members.size, net: o.buys - o.sells}))
    .sort((a,b) => b.n - a.n || Math.abs(b.net) - Math.abs(a.net) || b.max - a.max);
}

function sectorTotals(movers) {
  const by = new Map();
  for (const m of movers) {
    const k = sectorOf(m.t);
    const o = by.get(k) || {buy:0, sell:0, tickers:0};
    o.buy += m.buys; o.sell += m.sells; o.tickers++;
    by.set(k, o);
  }
  return [...by.entries()].map(([k,v]) => ({sector:k, ...v, net:v.buy - v.sell}))
                          .sort((a,b) => Math.abs(b.net) - Math.abs(a.net));
}

/* Outliers: the opposite signal to the ranking above. That one rewards consensus --
   many members converging on one name -- which by construction hides the lone large
   bet nobody else is in. This finds tickers a single member traded, ranked by
   disclosed size, and notes when the position is far bigger than that member's own
   typical trade. */
function loneLargePositions(minAmount = 100001, limit = 15) {
  const since = cutoff(), ch = $("#chamber").value, fl = floorVal();
  const rows = ROWS.filter(r => r[R_TICK] && r[R_AMT] >= fl
      && (!since || (r[R_DISC] || r[R_DATE]) >= since)
      && (!ch || M[r[R_M]].c === ch));

  // each member's typical trade size in this window, to judge "unusual for them"
  const perMember = new Map();
  for (const r of rows) {
    if (!perMember.has(r[R_M])) perMember.set(r[R_M], []);
    perMember.get(r[R_M]).push(r[R_AMT]);
  }
  const medianOf = mi => {
    const a = (perMember.get(mi) || []).slice().sort((x,y) => x - y);
    if (!a.length) return 0;
    const m = Math.floor(a.length / 2);
    return a.length % 2 ? a[m] : Math.round((a[m-1] + a[m]) / 2);
  };

  const byTicker = new Map();
  for (const r of rows) {
    const t = r[R_TICK];
    let o = byTicker.get(t);
    if (!o) { o = {t, members: new Set(), best: null}; byTicker.set(t, o); }
    o.members.add(r[R_M]);
    if (!o.best || r[R_AMT] > o.best[R_AMT]) o.best = r;
  }

  return [...byTicker.values()]
    .filter(o => o.members.size === 1 && o.best[R_AMT] >= minAmount)
    .map(o => {
      const med = medianOf(o.best[R_M]) || 1;
      return {t: o.t, r: o.best, mult: o.best[R_AMT] / med};
    })
    .sort((a,b) => b.r[R_AMT] - a.r[R_AMT] || b.mult - a.mult)
    .slice(0, limit);
}

/* ---- Scoreboard ----------------------------------------------------------------
   Rows arrive precomputed from scorecard.py; this only sorts and draws them. The
   window and floor pickers do NOT apply here -- the record is the member's whole
   measurable history at the default floor, and a record recomputed per window
   would rank members by which quarter you happened to be looking at. Chamber
   filters, since that one cannot change a member's own numbers.             */
const SCORE = DATA.scorecard || [];
const PERSIST = DATA.persistence || {};
let scoreSort = "med";

function renderScore() {
  const ch = $("#chamber").value;
  const rows = SCORE.filter(r => !ch || r.c === ch)
                    .slice()
                    .sort((a, b) => (b[scoreSort] ?? -9) - (a[scoreSort] ?? -9));
  const el = $("#view-score");
  if (!rows.length) {
    el.innerHTML = `<div class="panel"><p class="empty">Nothing scored yet. Run
      <code>congress-trades prices</code> to attach returns.</p></div>`;
    return;
  }
  // Sign is a mark, never the buy/sell colours: see common.css.
  const p = x => x == null ? "—"
    : `<span class="${x > 0 ? "up" : x < 0 ? "down" : ""}">${(x*100 >= 0 ? "+" : "")
       }${(x*100).toFixed(1)}%</span>`;
  const r0 = x => x == null ? "—" : Math.round(x*100) + "%";
  const head = [["med","vs index"],["smed","vs sector"],["beat","Beat index"],
                ["bmed","Buys"],["sellmed","Sells"],["h2","2nd half"],
                ["omed","Open now"]];

  el.innerHTML = `
    <div class="panel">
      <h3>Member scoreboard</h3>
      ${PERSIST.r == null ? "" : `<p class="callout">
        <b>Past alpha vs future alpha: r = ${PERSIST.r >= 0 ? "+" : ""}${
          PERSIST.r.toFixed(2)}</b> across ${PERSIST.n} members,
        ${Math.round(PERSIST.same_sign*100)}% keeping the same sign.
        ${Math.abs(PERSIST.r) < 0.25
          ? `Near zero &mdash; a member's record says almost nothing about their next
             trade. Read this table as history, not as a tip sheet, and check the
             <b>top name</b> column before believing any row: a high share means the
             whole record is one bet.`
          : `Some persistence, though still one sample over overlapping windows.`}</p>`}
      <p class="hint">Excess return over SPY across the same 90-day window, measured
        from the <b>disclosure</b> date. A buy scores stock minus index; a sell scores
        index minus stock, so exiting a name that then lagged the market counts as a
        win. <b>vs sector</b> repeats the figure against the trade&rsquo;s own sector
        ETF: beating the index but not the sector means a sector was timed, not a
        stock picked. Members with fewer than ${DATA.scoreMin} measurable trades are
        left out. Whole history at the default floor &mdash; the window and floor
        pickers above do not apply to this view. Overlapping windows and reported
        brackets mean a median across trades is not a portfolio return.</p>
      <div class="tbl-scroll"><table>
        <thead><tr><th>#</th><th>Member</th><th class="num">Trades</th>
          ${head.map(([k,l]) => `<th class="num"><button class="sortby" data-k="${k}"
             style="all:unset; cursor:pointer; ${k === scoreSort
               ? "color:var(--white); font-weight:700" : ""}">${l}</button></th>`).join("")}
        </tr></thead>
        <tbody>${rows.map((r, i) => `<tr>
          <td class="num">${i+1}</td>
          <td><b class="${pc(BY_NAME.get(r.n)?.party)}">${esc(r.n)}</b> <span style="color:var(--ink-3)">${esc(r.c[0] || "")}</span>
            ${r.cs != null && r.cs >= 0.5 ? `<div style="color:var(--warn); font-size:11.5px;
              margin:.15rem 0 0">one bet: ${Math.round(r.cs*100)}% of scored trades are
              <span title="${esc(tkName(r.ct))}">${esc(r.ct)}</span></div>` : ""}
            ${r.best ? `<div class="hint" style="margin:.15rem 0 0">${esc(r.best)}</div>` : ""}
            ${r.worst ? `<div class="hint" style="margin:.1rem 0 0; opacity:.75">${esc(r.worst)}</div>` : ""}
          </td>
          <td class="num"><button class="jump" data-jump="${esc(r.n)}"
            title="See ${esc(r.n)}'s trades">${r.t}</button></td>
          <td class="num">${p(r.med)}</td>
          <td class="num">${p(r.smed)}</td>
          <td class="num">${r0(r.beat)}</td>
          <td class="num">${p(r.bmed)} <span style="color:var(--ink-3)">(${r.bn})</span></td>
          <td class="num">${p(r.sellmed)} <span style="color:var(--ink-3)">(${r.sn})</span></td>
          <td class="num">${p(r.h1)} <span style="color:var(--ink-3)">&rarr;</span> ${p(r.h2)}</td>
          <td class="num">${p(r.omed)} <span style="color:var(--ink-3)">(${r.on})</span></td>
        </tr>`).join("")}</tbody>
      </table></div>
    </div>`;
  el.querySelectorAll("button.sortby").forEach(b =>
    b.addEventListener("click", () => { scoreSort = b.dataset.k; renderScore(); }));
  el.querySelectorAll("button.jump").forEach(b =>
    b.addEventListener("click", () => jumpToMember(b.dataset.jump)));
}

/* The scoreboard scores a member's whole history at the default floor, so send the
   window picker back to All time on the way over -- otherwise the trade count you
   clicked lands on a detail panel filtered down to nothing. */
function jumpToMember(name) {
  const mi = M.findIndex(m => m.n === name);
  if (mi < 0) return;
  $("#window").value = "0";
  setView("members");
  select(mi);
  $("#view-members").scrollIntoView({behavior: "smooth", block: "start"});
}

function renderMovers() {
  const movers = moverRows();
  const el = $("#view-movers");
  if (!movers.length) {
    el.innerHTML = `<div class="panel"><p class="empty">No disclosures in this window.</p></div>`;
    return;
  }
  const secs = sectorTotals(movers).slice(0, 12);
  const span = Math.max(1, ...secs.map(s => Math.abs(s.net)));
  const bars = secs.map(s => {
    const w = Math.abs(s.net) / span * 50;              // half-width max, diverging
    const left = s.net >= 0 ? 50 : 50 - w;
    return `<div class="secrow">
      <span class="nm" title="${esc(s.sector)}">${esc(s.sector)}</span>
      <span class="track"><span class="axis" style="left:50%"></span>
        <i style="left:${left}%; width:${w}%;
           background:var(--${s.net >= 0 ? "buy" : "sell"})"></i></span>
      <span class="val">${s.net > 0 ? "+" : ""}${s.net}</span></div>`;
  }).join("");

  const top = movers.slice(0, 40);
  const all = activeRows();
  el.innerHTML = `
    <div class="panel" style="margin-bottom:1rem" id="mv-act">
      <h3>Activity</h3>
      <p class="hint">Every member's trades per month at this filter.</p>
      ${chartSVG(all)}<div id="mv-pick"></div>
    </div>
    <div class="panel" style="margin-bottom:1rem">
      <h3>Net buying by sector</h3>
      <p class="hint">Buys minus sells across disclosures in this window.
         Industry from the SEC's own SIC classification.</p>
      <div class="legend">
        <span><i style="background:var(--buy)"></i>Net buying</span>
        <span><i style="background:var(--sell)"></i>Net selling</span></div>
      ${bars}
    </div>
    ${outlierPanel()}
    <div class="panel">
      <h3>Most-traded names</h3>
      <p class="hint">Ranked by number of distinct members, then by net direction.</p>
      <div class="tbl-scroll"><table>
        <thead><tr><th>Ticker</th><th>Company</th><th>Sector</th><th>Members</th>
          <th>Buys</th><th>Sells</th><th>Net</th><th>Largest</th><th>Latest</th></tr></thead>
        <tbody>${top.map(m => `<tr>
          <td class="mv-tick" title="${esc(tkName(m.t))}">${esc(m.t)}</td>
          <td>${esc(companyOf(m.t) || "—")}</td>
          <td class="mv-mem">${esc(sectorOf(m.t))}</td>
          <td class="num">${m.n}</td>
          <td class="num">${m.buys || ""}</td>
          <td class="num">${m.sells || ""}</td>
          <td class="num" style="color:var(--${m.net >= 0 ? "buy" : "sell"})">
             ${m.net > 0 ? "+" : ""}${m.net}</td>
          <td class="num">${fmtMoney(m.max)}</td>
          <td class="num">${esc(m.last)}</td></tr>`).join("")}
        </tbody></table></div>
    </div>`;
  wireChart("#mv-act", (k, t) => monthList(all, k, t));
}

/* One month's buys or sells from the Movers activity chart, largest first. */
function monthList(all, k, t) {
  const rows = all.filter(r => r[R_DATE].startsWith(k) && r[R_TYPE] === t)
                  .sort((a, b) => b[R_AMT] - a[R_AMT]);
  const box = $("#mv-pick");
  box.innerHTML = `<div class="pick">${rows.length.toLocaleString()} ${esc(t)}s in ${esc(k)}${
      rows.length > 200 ? " (largest 200 shown)" : ""}<button id="mv-unpick">close</button></div>
    <div class="tbl-scroll"><table>
      <thead><tr><th>Date</th><th>Member</th><th>Ticker</th><th>Amount</th><th>Asset</th></tr></thead>
      <tbody>${rows.slice(0, 200).map(r => `<tr>
        <td class="num">${r[R_DATE]}</td>
        <td><button class="jump ${pc(M[r[R_M]].party)}" data-jump="${esc(M[r[R_M]].n)}"
          style="all:unset;cursor:pointer">${esc(M[r[R_M]].full)}</button></td>
        <td class="num">${r[R_TICK] ? tk(r[R_TICK]) : "—"}</td>
        <td class="num">${esc(r[R_RANGE] || fmtMoney(r[R_AMT]))}</td>
        <td>${esc(r[R_ASSET])}</td></tr>`).join("")}</tbody></table></div>`;
  $("#mv-unpick").onclick = () => { box.innerHTML = ""; };
  box.querySelectorAll("button.jump").forEach(b =>
    b.addEventListener("click", () => jumpToMember(b.dataset.jump)));
}

function outlierPanel() {
  const lone = loneLargePositions();
  if (!lone.length) return "";
  return `<div class="panel" style="margin-bottom:1rem">
    <h3>Lone large positions</h3>
    <p class="hint">Names only one member traded in this window, ranked by disclosed
       size — the bets the consensus ranking above cannot show. “vs typical” compares
       the position to that member's own median trade.</p>
    <div class="tbl-scroll"><table>
      <thead><tr><th>Ticker</th><th>Company</th><th>Sector</th><th>Member</th>
        <th>Type</th><th>Amount</th><th>vs typical</th><th>Date</th></tr></thead>
      <tbody>${lone.map(({t, r, mult}) => `<tr>
        <td class="mv-tick" title="${esc(tkName(t))}">${esc(t)}</td>
        <td>${esc(companyOf(t) || "—")}</td>
        <td class="mv-mem">${esc(sectorOf(t))}</td>
        <td class="${pc(M[r[R_M]].party)}">${esc(M[r[R_M]].full)}</td>
        <td><span class="tag ${r[R_TYPE]}">${r[R_TYPE].toUpperCase()}</span></td>
        <td class="num">${esc(r[R_RANGE] || fmtMoney(r[R_AMT]))}</td>
        <td class="num">${mult >= 3 ? `<span class="bigmult">${mult.toFixed(0)}×</span>`
                                    : mult >= 1.5 ? mult.toFixed(1) + "×" : "—"}</td>
        <td class="num">${esc(r[R_DATE])}</td></tr>`).join("")}
      </tbody></table></div></div>`;
}

let view = "members";
function setView(v) {
  view = v;
  document.querySelectorAll(".views button").forEach(b =>
    b.setAttribute("aria-current", b.dataset.view === v));
  $("#view-members").hidden = v !== "members";
  $("#view-movers").hidden = v !== "movers";
  $("#view-score").hidden = v !== "score";
  refresh();
}
document.querySelector(".views").addEventListener("click", e => {
  const b = e.target.closest("button[data-view]");
  if (b) setView(b.dataset.view);
});

$("#roster").addEventListener("click", e => {
  const b = e.target.closest("button[data-mi]");
  if (b) select(+b.dataset.mi);
});
function refresh() {
  const el = $("#stat-shown");
  if (el) el.textContent = activeRows().length.toLocaleString();
  if (view === "movers") { renderMovers(); return; }
  if (view === "score") { renderScore(); return; }
  renderRoster(); renderDetail();
}
$("#floor").value = "__DEFAULT_FLOOR__";
["#q","#chamber","#window","#floor"].forEach(s =>
  $(s).addEventListener("input", refresh));
refresh();
wireThemeToggle($("#theme"));
