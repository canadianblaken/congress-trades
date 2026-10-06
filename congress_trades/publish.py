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
from pathlib import Path

from . import annual, compliance, db, finance, jurisdiction, scorecard
from .config import CONFIG


def _extras(cfg) -> tuple[dict, dict]:
    """(late filings by member, in-jurisdiction PAC overlap by bioguide), from the
    modules that own those rules so the page cannot disagree with their reports.
    A stream that cannot build is left out rather than failing the page."""
    try:
        late = compliance.build(cfg).get("members") or {}
    except Exception:
        late = {}
    try:
        pac = {o["bioguide"]: o for o in finance.run(None, cfg).get("overlaps") or []}
    except Exception:
        pac = {}
    return late, pac


def build_payload(conn, cfg=CONFIG) -> dict:
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

    late, pac = _extras(cfg)
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
            covered.update(jurisdiction.sectors_for_seat(x))
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
            **_beyond(name, p.get("bioguide"), late, pac, conn),
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
            "names": db.ticker_names(conn),
            "sectorNames": list(snames), "tickers": tmap,
            "committeeNames": list(cnames)}


def _beyond(name, bioguide, late, pac, conn) -> dict:
    """The "Beyond trades" fields for one member, only those that exist."""
    out = {}
    c = late.get(name)
    if c:
        out["late"] = [c.get("late_count") or 0, c.get("total_trades") or 0,
                       c.get("worst_lag"), c.get("late_share") or 0]
    o = pac.get(bioguide or "")
    if o:
        out["pac"] = [o.get("pac_dollars") or 0, o.get("pac_total") or 0,
                      o.get("sectors") or [], o.get("trade_count") or 0]
    a = annual.member_summary(conn, name)
    if a:
        out["ann"] = a
    return out


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
            "smed": m["overall"]["smed"],
            "bmed": m["buys"]["med"], "bn": m["buys"]["n"],
            "sellmed": m["sells"]["med"], "sn": m["sells"]["n"],
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
        payload = build_payload(conn, cfg)
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
    page = _template().replace("__DATA__", json.dumps(payload, separators=(",", ":")))
    page = page.replace("__STATS__", json.dumps(stats))
    page = page.replace("__DEFAULT_FLOOR__", str(cfg.default_floor))
    page = page.replace("__LATE_DAYS__", str(compliance.STATUTORY_DAYS))
    page = page.replace("__GENERATED__", html.escape(
        dt.datetime.now().strftime("%Y-%m-%d %H:%M")))
    cfg.out_html.parent.mkdir(parents=True, exist_ok=True)
    cfg.out_html.write_text(page, encoding="utf-8")
    print(f"wrote {cfg.out_html} ({len(page):,} bytes, {stats['trades']:,} trades, "
          f"{stats['members']} members)")
    return 0


# The page's HTML, CSS and JavaScript live in web/ as ordinary files; publish
# inlines them, with the helpers it shares with the portal, into one file.
WEB = Path(__file__).resolve().parent / "web"


def _template() -> str:
    read = lambda n: (WEB / n).read_text(encoding="utf-8")
    return (read("page.html").replace("__COMMON_JS__", read("common.js"))
            .replace("__CSS__", read("common.css") + read("page.css"))
            .replace("__JS__", read("page.js")))

