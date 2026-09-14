"""
Render the collected disclosures into a single self-contained HTML page.

Everything -- data, styles, behaviour -- is inlined, so the output is one file you
can open locally, drop on a static host, or serve from a private network. There is
no API and no server to keep running.
"""
from __future__ import annotations

import datetime as dt
import html
import json
from collections import Counter

from . import db, legislators, scorecard
from .config import CONFIG


def build_payload(conn) -> dict:
    profiles = {r["member"]: dict(r) for r in db.all_members(conn)}
    seats_by_bio = db.committees_by_member(conn)
    cnames: dict[str, int] = {}
    rows = db.all_trades(conn)
    members: dict[str, int] = {}
    order: list[str] = []
    urls: dict[str, int] = {}
    out = []
    for r in rows:
        name = r["member"]
        if name not in members:
            members[name] = len(order)
            order.append(name)
        ui = urls.setdefault(r["doc_url"] or "", len(urls))
        out.append([members[name], r["ticker"] or "", r["tx_type"] or "", r["tx_date"] or "",
                    r["amount_min"] or 0, r["amount_range"] or "", r["owner"] or "",
                    (r["asset_name"] or "")[:90], ui, r["disclosed"] or ""])

    mlist = []
    for name in order:
        p = profiles.get(name, {})
        seat = p.get("state") or ""
        if seat and p.get("district"):
            seat = f"{seat}-{str(p['district']).zfill(2)}"
        seats = seats_by_bio.get(p.get("bioguide") or "", [])
        full_committees = [x for x in seats if not x.get("parent")]
        covered: set[str] = set()
        for x in seats:                       # subcommittees carry jurisdiction too
            covered.update(legislators.sectors_for_committee(x.get("name") or ""))
        mlist.append({
            "comm": [[cnames.setdefault(x["name"], len(cnames)), x.get("title") or ""]
                     for x in full_committees],
            "nsub": len(seats) - len(full_committees),
            "csec": sorted(covered),
            "n": name,
            "full": p.get("full_name") or name,
            "c": p.get("chamber") or "",
            "s": seat,
            "party": (p.get("party") or "")[:1],       # D / R / I
            "desc": p.get("wiki_desc") or "",
            "bio": p.get("wiki_extract") or "",
            "thumb": p.get("wiki_thumb") or "",
            "wurl": p.get("wiki_url") or "",
            "ourl": p.get("official_url") or "",
            "cur": int(p.get("current") or 0),
            "unity": p.get("party_unity"),
            "nvotes": p.get("votes_cast"),
            "nom": p.get("nominate"),
        })
    # ticker -> [sector index, company]; sectors interned since they repeat heavily
    secs = db.all_sectors(conn)
    snames: dict[str, int] = {}
    tmap = {}
    for t, rec in secs.items():
        sec = rec.get("sector") or "Unclassified"
        si = snames.setdefault(sec, len(snames))
        tmap[t] = [si, (rec.get("company") or "")[:60]]
    return {"members": mlist, "urls": list(urls), "rows": out,
            "sectorNames": list(snames), "tickers": tmap,
            "committeeNames": list(cnames)}


def scorecard_payload(cfg) -> list[dict]:
    """Flattened scorecard rows, with the best/worst calls already worded.

    Deliberately precomputed: the alpha definition lives in scorecard.py, and a
    second copy in JavaScript would be a second answer to "did this trade work".
    """
    out = []
    for m in scorecard.members(cfg.default_floor, "90", cfg=cfg):
        out.append({
            "n": m["member"], "c": m["chamber"], "t": m["overall"]["n"],
            "med": m["overall"]["med"], "beat": m["overall"]["beat"],
            "bmed": m["buys"]["med"], "bn": m["buys"]["n"],
            "smed": m["sells"]["med"], "sn": m["sells"]["n"],
            "omed": m["open"]["med"], "on": m["open_n"],
            "best": scorecard.describe(m["best"], m["best"]["alpha"]) if m["best"] else "",
            "worst": (scorecard.describe(m["worst"], m["worst"]["alpha"])
                      if m["worst"] and m["worst"] is not m["best"] else ""),
            "h1": m["split"]["first"]["med"], "h2": m["split"]["second"]["med"],
            "ct": m["conc"]["top"], "cs": m["conc"]["share"],
        })
    return out


def render(cfg=CONFIG) -> int:
    with db.connect(cfg.db_path) as conn:
        payload = build_payload(conn)
    payload["scorecard"] = scorecard_payload(cfg)
    payload["scoreMin"] = scorecard.MIN_TRADES
    payload["persistence"] = scorecard.persistence(
        scorecard.members(cfg.default_floor, "90", cfg=cfg))
    rows = payload["rows"]
    if not rows:
        print("no congress trades in db; run congress_backfill.py first")
        return 1

    # Filings occasionally carry a typo'd transaction year (seen: a 12/26/2026 trade
    # notified 01/21/2026). The row is kept as filed, but one bad date must not define
    # the stated coverage range.
    today = dt.date.today().isoformat()
    dates = [r[3] for r in rows if r[3] and r[3] <= today]
    span = f"{min(dates)} to {max(dates)}" if dates else "—"
    chambers = Counter(payload["members"][r[0]]["c"] for r in rows)
    stats = {
        "trades": len(rows),
        "members": len(payload["members"]),
        "span": span,
        "house": chambers.get("House", 0),
        "senate": chambers.get("Senate", 0),
        "floor": cfg.default_floor,
        "updated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    page = TEMPLATE.replace("__DATA__", json.dumps(payload, separators=(",", ":")))
    page = page.replace("__STATS__", json.dumps(stats))
    page = page.replace("__DEFAULT_FLOOR__", str(cfg.default_floor))
    page = page.replace("__GENERATED__", html.escape(
        dt.datetime.now().strftime("%Y-%m-%d %H:%M")))
    cfg.out_html.parent.mkdir(parents=True, exist_ok=True)
    cfg.out_html.write_text(page, encoding="utf-8")
    print(f"wrote {cfg.out_html} ({len(page):,} bytes, {stats['trades']:,} trades, "
          f"{stats['members']} members)")
    return 0


TEMPLATE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Congress Trades</title>
<style>
  :root {
    color-scheme: dark;
    --bg:#0e1116; --panel:#12161c; --line:#222831; --line-2:#2a3038;
    --ink:#d6dae0; --ink-2:#9aa4b2; --ink-3:#6b7480; --white:#fff;
    --buy:#3987e5; --sell:#e66767; --link:#6cb6ff;
  }
  * { box-sizing:border-box; }
  /* .wrap sets display:grid, which ties on specificity with the UA [hidden] rule and
     wins on source order -- so hiding a view needs this to actually take effect. */
  [hidden] { display:none !important; }
  body { background:var(--bg); color:var(--ink); margin:0;
         font:15px/1.55 -apple-system,Segoe UI,Roboto,sans-serif;
         padding:1.6rem 1.2rem 4rem; max-width:1240px; margin:0 auto; }
  a { color:var(--link); }
  h1 { color:var(--white); font-size:1.6rem; margin:.2rem 0 .3rem;
       border-bottom:2px solid var(--line-2); padding-bottom:.4rem; }
  .site-nav { font-size:14px; margin:0 0 1rem; }
  .sub { color:var(--ink-3); font-size:13px; margin:.5rem 0 1.2rem; }
  .stats { display:flex; flex-wrap:wrap; gap:1.6rem; margin:0 0 1.4rem;
           padding:.9rem 1.1rem; background:var(--panel);
           border:1px solid var(--line); border-radius:8px; }
  .stat b { display:block; color:var(--white); font-size:1.35rem; font-weight:600;
            line-height:1.2; font-variant-numeric:tabular-nums; }
  .stat span { color:var(--ink-3); font-size:12px; text-transform:uppercase;
               letter-spacing:.04em; }
  .filters { display:flex; flex-wrap:wrap; gap:.6rem; margin-bottom:1rem; }
  input[type=search], select {
    background:var(--panel); color:var(--ink); border:1px solid var(--line-2);
    border-radius:6px; padding:.45rem .6rem; font:inherit; font-size:14px; }
  input[type=search] { min-width:230px; }
  input:focus-visible, select:focus-visible, button:focus-visible {
    outline:2px solid var(--link); outline-offset:1px; }
  .wrap { display:grid; grid-template-columns:290px 1fr; gap:1.2rem; align-items:start; }
  @media (max-width:820px){ .wrap { grid-template-columns:1fr; } }
  .roster { background:var(--panel); border:1px solid var(--line); border-radius:8px;
            max-height:76vh; overflow-y:auto; }
  .roster button { display:flex; justify-content:space-between; gap:.5rem; width:100%;
    background:none; border:0; border-bottom:1px solid var(--line); color:var(--ink);
    font:inherit; text-align:left; padding:.55rem .75rem; cursor:pointer; }
  .roster button:hover { background:#171c23; }
  .roster button[aria-current=true] { background:#1b2430; color:var(--white);
    box-shadow:inset 3px 0 0 var(--link); }
  .roster .who { min-width:0; }
  .roster .who i { display:block; font-style:normal; color:var(--ink-3); font-size:11.5px; }
  .roster .n { color:var(--ink-2); font-variant-numeric:tabular-nums; font-size:13px; }
  .detail { background:var(--panel); border:1px solid var(--line); border-radius:8px;
            padding:1.1rem 1.2rem; min-height:340px; }
  .detail h2 { color:var(--white); margin:0 0 .15rem; font-size:1.25rem; }
  .detail .meta { color:var(--ink-3); font-size:13px; margin-bottom:1rem; }
  .profile { display:flex; gap:1rem; align-items:flex-start; margin-bottom:1rem;
             padding-bottom:1rem; border-bottom:1px solid var(--line); }
  .profile img { width:74px; height:92px; object-fit:cover; border-radius:6px;
                 border:1px solid var(--line-2); flex:none; background:#0c0f14; }
  .profile .who { min-width:0; }
  .profile .bio { color:var(--ink-2); font-size:13px; margin:.45rem 0 .5rem;
                  max-width:72ch; }
  .profile .links a { font-size:12.5px; margin-right:.9rem; }
  .chips { display:flex; flex-wrap:wrap; gap:.4rem; align-items:center; margin-top:.2rem; }
  .chip { font-size:11.5px; padding:.1rem .45rem; border-radius:4px; border:1px solid var(--line-2);
          color:var(--ink-2); }
  /* Party is a bordered tint + letter, never a solid fill: the solid blue/red in this
     panel belong to buy/sell, and identity must not ride on colour alone. */
  .chip.p-D { color:#8fb8ea; border-color:#2f4a6d; background:#141d2a; }
  .chip.p-R { color:#eb9a9a; border-color:#6d3434; background:#2a1616; }
  .chip.p-I { color:var(--ink-2); }
  .chip.former { color:#d8b06a; border-color:#5c4622; background:#251c0e; }
  .toptick { color:var(--ink-2); font-size:12.5px; margin-top:.35rem; }
  .comm { margin:.9rem 0 0; }
  .comm h4 { color:var(--ink-2); font-size:11.5px; text-transform:uppercase;
             letter-spacing:.04em; margin:0 0 .3rem; font-weight:600; }
  .comm ul { margin:0; padding:0; list-style:none; font-size:12.5px; color:var(--ink-2); }
  .comm li { padding:.1rem 0; }
  .comm .chair { color:#d8b06a; font-weight:600; }
  .overlap { margin-top:.7rem; padding:.6rem .75rem; border-radius:6px;
             border:1px solid #5c4622; background:#1d1710; }
  .overlap h4 { color:#d8b06a; }
  .overlap li { color:var(--ink); }
  .overlap .why { color:var(--ink-3); }
  .overlap .note { color:var(--ink-3); font-size:11.5px; margin:.4rem 0 0; }
  .toptick b { color:var(--ink); }
  .legend { display:flex; gap:1rem; font-size:12.5px; color:var(--ink-2); margin:.2rem 0 .3rem; }
  .legend i { display:inline-block; width:10px; height:10px; border-radius:2px;
              margin-right:.35rem; vertical-align:baseline; }
  table { border-collapse:collapse; width:100%; font-size:13px; margin-top:.4rem; }
  th, td { border-bottom:1px solid var(--line); padding:.4rem .5rem; text-align:left;
           vertical-align:top; }
  th { color:var(--ink-3); font-weight:600; font-size:11.5px; text-transform:uppercase;
       letter-spacing:.03em; position:sticky; top:0; background:var(--panel); }
  td.num { font-variant-numeric:tabular-nums; white-space:nowrap; }
  .tag { font-size:11px; padding:.1rem .4rem; border-radius:3px; font-weight:600; }
  .tag.buy { color:var(--buy); background:#12233a; }
  .tag.sell { color:var(--sell); background:#3a1c1c; }
  .tag.late { color:#d8b06a; background:#251c0e; border:1px solid #5c4622; margin-left:.3rem; }
  .lag { color:var(--ink-2); font-size:12.5px; margin-top:.3rem; }
  .lag b { color:var(--ink); }
  .lag .bad { color:#d8b06a; }
  .unity { color:var(--ink-2); font-size:12.5px; margin-top:.3rem; }
  .unity b { color:var(--ink); }
  .meter { display:inline-block; width:110px; height:7px; border-radius:4px;
           background:#1b2027; vertical-align:middle; margin:0 .4rem; position:relative;
           border:1px solid var(--line-2); }
  .meter i { position:absolute; left:0; top:0; bottom:0; border-radius:3px; display:block; }
  .tbl-scroll { max-height:52vh; overflow:auto; }
  .empty { color:var(--ink-3); padding:2.5rem 0; text-align:center; }
  .chart-wrap { overflow-x:auto; }
  svg text { fill:var(--ink-3); font-size:10px; }
  svg .zero { stroke:var(--line-2); }
  .tip { position:fixed; pointer-events:none; background:#1b2027; color:var(--ink);
    border:1px solid var(--line-2); border-radius:6px; padding:.4rem .55rem; font-size:12px;
    box-shadow:0 4px 14px #0009; opacity:0; transition:opacity .1s; z-index:9; }
  .foot { color:var(--ink-3); font-size:12.5px; margin-top:2rem; border-top:1px solid var(--line);
          padding-top:.9rem; }
  .views { display:flex; gap:.3rem; margin:0 0 1rem; }
  .views button { background:var(--panel); border:1px solid var(--line-2); color:var(--ink-2);
    font:inherit; font-size:14px; padding:.4rem .9rem; border-radius:6px; cursor:pointer; }
  .views button[aria-current=true] { background:#1b2430; color:var(--white);
    border-color:#365070; }
  .panel { background:var(--panel); border:1px solid var(--line); border-radius:8px;
           padding:1.1rem 1.2rem; }
  .panel h3 { color:var(--white); margin:0 0 .2rem; font-size:1.05rem; }
  .panel .hint { color:var(--ink-3); font-size:12.5px; margin:0 0 1rem; }
  button.jump { all:unset; cursor:pointer; color:var(--link);
                border-bottom:1px dotted currentColor; font-variant-numeric:tabular-nums; }
  button.jump:hover, button.jump:focus-visible { color:var(--white); }
  .secrow { display:grid; grid-template-columns:220px 1fr 54px; gap:.6rem; align-items:center;
            font-size:13px; margin-bottom:.28rem; }
  .secrow .nm { color:var(--ink-2); text-align:right; overflow:hidden; text-overflow:ellipsis;
                white-space:nowrap; }
  .secrow .val { color:var(--ink); font-variant-numeric:tabular-nums; font-size:12.5px; }
  .track { position:relative; height:17px; }
  .track i { position:absolute; top:1px; height:15px; border-radius:3px; display:block; }
  .track .axis { position:absolute; top:0; bottom:0; width:1px; background:var(--line-2); }
  .mv-tick { font-weight:600; color:var(--white); }
  .mv-mem { color:var(--ink-3); font-size:12px; }
  .bigmult { color:#d8b06a; font-weight:600; }
</style></head><body>
<h1>Congress Trades</h1>
<p class="sub">STOCK Act periodic transaction reports, straight from the House Clerk bulk feed
   and Senate eFD. Disclosures lag trades by up to 45 days. Amounts are the ranges members
   report, not exact values.</p>

<div class="stats" id="stats"></div>

<div class="filters">
  <input type="search" id="q" placeholder="Filter members or tickers…" aria-label="Filter">
  <select id="chamber" aria-label="Chamber">
    <option value="">Both chambers</option><option>House</option><option>Senate</option>
  </select>
  <select id="floor" aria-label="Minimum disclosed amount">
    <option value="0">Any amount</option>
    <option value="15001">&ge; $15k</option>
    <option value="50001">&ge; $50k</option>
    <option value="100001">&ge; $100k</option>
    <option value="250001">&ge; $250k</option>
    <option value="1000001">&ge; $1M</option>
  </select>
  <select id="window" aria-label="Time window">
    <option value="1">Last day</option>
    <option value="7">Last week</option>
    <option value="30">Last month</option>
    <option value="90">Last 90 days</option>
    <option value="365">Last year</option>
    <option value="0" selected>All time</option>
  </select>
</div>

<div class="views" role="tablist">
  <button data-view="members" aria-current="true">Members</button>
  <button data-view="movers" aria-current="false">Movers</button>
  <button data-view="score" aria-current="false">Scoreboard</button>
</div>

<div class="wrap" id="view-members">
  <div class="roster" id="roster" role="list"></div>
  <div class="detail" id="detail"></div>
</div>

<div id="view-movers" hidden></div>

<div id="view-score" hidden></div>

<p class="foot">Generated __GENERATED__ &middot;
  disclosures under the reporting floor are excluded &middot; this is public disclosure data,
  not investment advice.</p>
<div class="tip" id="tip" role="status"></div>

<script>
const DATA = __DATA__, STATS = __STATS__;
const M = DATA.members, ROWS = DATA.rows, URLS = DATA.urls;
// row: [memberIdx, ticker, type, date, amtMin, amtRange, owner, asset, urlIdx]
const R_M=0,R_TICK=1,R_TYPE=2,R_DATE=3,R_AMT=4,R_RANGE=5,R_OWN=6,R_ASSET=7,R_URL=8,R_DISC=9;
const SECTORS = DATA.sectorNames, TICKERS = DATA.tickers;
const COMMITTEES = DATA.committeeNames;
const sectorOf = t => { const e = TICKERS[t]; return e ? SECTORS[e[0]] : "Unclassified"; };
const companyOf = t => { const e = TICKERS[t]; return e ? e[1] : ""; };

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
       <span class="who">${esc(x.m.full)}<i>${[x.m.party, x.m.c, x.m.s]
           .filter(Boolean).map(esc).join(" · ")}</i></span>
       <span class="n">${x.n}</span></button>`).join("")
    : `<p class="empty">No members match.</p>`;
  if (list.length && !list.some(x => x.mi === selected)) select(list[0].mi);
  else if (!list.length) { selected = null; $("#detail").innerHTML =
    `<p class="empty">Nothing to show for this filter.</p>`; }
}

const esc = s => String(s).replace(/[&<>"]/g, c =>
  ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

function select(mi) {
  selected = mi;
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
        <h2>${esc(m.full)}</h2>
        <div class="chips">${chips}</div>
        ${m.desc ? `<p class="meta" style="margin:.4rem 0 0">${esc(m.desc)}</p>` : ""}
        ${m.bio ? `<p class="bio">${esc(m.bio)}</p>` : ""}
        ${links ? `<p class="links">${links}</p>` : ""}
      </div>
    </div>
    <p class="meta">${rows.length} disclosed ${rows.length===1?"trade":"trades"} ·
       ${buys} buys / ${sells} sells · largest bracket from ${top}
       ${m.n !== m.full ? `<br><span style="color:var(--ink-3)">files as “${esc(m.n)}”</span>` : ""}</p>
    ${lagLine(rows)}
    ${unityLine(m)}
    ${topTicks.length ? `<p class="toptick">Most traded: ${topTicks
        .map(([t,n]) => `<b>${esc(t)}</b> (${n})`).join(" · ")}</p>` : ""}
    ${committeeBlock(m, rows)}
    ${rows.length ? chartSVG(rows) : ""}
    <div class="tbl-scroll"><table>
      <thead><tr><th>Date</th><th>Ticker</th><th>Type</th><th>Amount</th>
        <th>Owner</th><th>Asset</th><th>Filing</th></tr></thead>
      <tbody>${rows.map(r => `<tr>
        <td class="num">${r[R_DATE]}</td>
        <td class="num">${r[R_TICK] ? "<b>"+esc(r[R_TICK])+"</b>" : "—"}</td>
        <td><span class="tag ${r[R_TYPE]}">${r[R_TYPE].toUpperCase()}</span>${
          isLate(r) ? `<span class="tag late" title="filed ${isLate(r)} days after the trade"
            >+${isLate(r)}d</span>` : ""}</td>
        <td class="num">${esc(r[R_RANGE]||"—")}</td>
        <td>${esc(r[R_OWN])}</td>
        <td>${esc(r[R_ASSET])}</td>
        <td>${URLS[r[R_URL]] ? `<a href="${esc(URLS[r[R_URL]])}" target="_blank"
              rel="noopener">PDF</a>` : "—"}</td></tr>`).join("")}
      </tbody></table></div>`;
  wireChart();
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
             fill="var(--buy)" data-k="${k}" data-b="${d.buy}" data-s="${d.sell}"/>`; }
    if (d.sell) { const h = Math.max(3, d.sell*unit);
      s += `<rect class="bar" x="${x}" y="${mid+1}" width="${w}" height="${h}" rx="3"
             fill="var(--sell)" data-k="${k}" data-b="${d.buy}" data-s="${d.sell}"/>`; }
    const showLbl = months.length <= 14 || i % Math.ceil(months.length/12) === 0;
    if (showLbl) s += `<text x="${x + w/2}" y="${H-4}" text-anchor="middle"
                        transform="rotate(-38 ${x+w/2} ${H-4})">${k.slice(2)}</text>`;
    return s;
  }).join("");
  return `<div class="legend">
      <span><i style="background:var(--buy)"></i>Buys</span>
      <span><i style="background:var(--sell)"></i>Sells</span>
      <span style="color:var(--ink-3)">disclosures per month</span></div>
    <div class="chart-wrap"><svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}"
        role="img" aria-label="Monthly buy and sell disclosures; full detail in the table below">
      <line class="zero" x1="34" y1="${mid}" x2="${W-4}" y2="${mid}"/>
      <text x="30" y="${mid-52}" text-anchor="end">${max}</text>
      <text x="30" y="${mid+3}" text-anchor="end">0</text>
      <text x="30" y="${mid+60}" text-anchor="end">${max}</text>
      ${bars}</svg></div>`;
}

function wireChart() {
  const tip = $("#tip");
  document.querySelectorAll("#detail .bar").forEach(el => {
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
function unityLine(m) {
  if (m.unity == null) return "";
  const lo = 75, pct = Math.max(0, Math.min(100, (m.unity - lo) / (100 - lo) * 100));
  const col = m.party === "D" ? "#8fb8ea" : m.party === "R" ? "#eb9a9a" : "var(--ink-2)";
  return `<p class="unity">Votes with own party <b>${m.unity}%</b>
    <span class="meter" title="${m.unity}% (scale ${lo}-100%)">
      <i style="width:${pct}%; background:${col}"></i></span>
    <span style="color:var(--ink-3)">${(m.nvotes||0).toLocaleString()} roll calls,
      119th Congress${m.nom != null ? ` · DW-NOMINATE ${m.nom > 0 ? "+" : ""}${m.nom.toFixed(2)}` : ""}</span></p>`;
}

const LATE_DAYS = 45;
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

function lagLine(rows) {
  const s = lagStats(rows);
  if (!s) return "";
  return `<p class="lag">Files a median of <b>${s.median} days</b> after trading` +
    (s.late ? ` · <span class="bad">${s.late} of ${s.n} (${s.pct}%) past the
       ${LATE_DAYS}-day deadline</span>` : ` · all within the ${LATE_DAYS}-day deadline`) +
    `</p>`;
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
  const p = x => x == null ? "—"
    : `<span style="color:var(--${x >= 0 ? "buy" : "sell"})">${(x*100 >= 0 ? "+" : "")
       }${(x*100).toFixed(1)}%</span>`;
  const r0 = x => x == null ? "—" : Math.round(x*100) + "%";
  const head = [["med","Median alpha"],["beat","Beat index"],
                ["bmed","Buys"],["smed","Sells"],["h2","2nd half"],
                ["omed","Open now"]];

  el.innerHTML = `
    <div class="panel">
      <h3>Member scoreboard</h3>
      ${PERSIST.r == null ? "" : `<p style="border-left:3px solid var(--${
        Math.abs(PERSIST.r) < 0.25 ? "sell" : "line-2"}); padding:.5rem .8rem;
        margin:0 0 1rem; background:var(--bg); font-size:13px">
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
        win. Members with fewer than ${DATA.scoreMin} measurable trades are
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
          <td><b>${esc(r.n)}</b> <span style="color:var(--ink-3)">${esc(r.c[0] || "")}</span>
            ${r.cs != null && r.cs >= 0.5 ? `<div style="color:var(--sell); font-size:11.5px;
              margin:.15rem 0 0">one bet: ${Math.round(r.cs*100)}% of scored trades are
              ${esc(r.ct)}</div>` : ""}
            ${r.best ? `<div class="hint" style="margin:.15rem 0 0">${esc(r.best)}</div>` : ""}
            ${r.worst ? `<div class="hint" style="margin:.1rem 0 0; opacity:.75">${esc(r.worst)}</div>` : ""}
          </td>
          <td class="num"><button class="jump" data-jump="${esc(r.n)}"
            title="See ${esc(r.n)}'s trades">${r.t}</button></td>
          <td class="num">${p(r.med)}</td>
          <td class="num">${r0(r.beat)}</td>
          <td class="num">${p(r.bmed)} <span style="color:var(--ink-3)">(${r.bn})</span></td>
          <td class="num">${p(r.smed)} <span style="color:var(--ink-3)">(${r.sn})</span></td>
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
  el.innerHTML = `
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
          <td class="mv-tick">${esc(m.t)}</td>
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
        <td class="mv-tick">${esc(t)}</td>
        <td>${esc(companyOf(t) || "—")}</td>
        <td class="mv-mem">${esc(sectorOf(t))}</td>
        <td>${esc(M[r[R_M]].full)}</td>
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
</script>
</body></html>
"""
