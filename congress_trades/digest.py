"""Reduce the database to a prompt-sized brief an LLM can actually read.

The HTML page computes convergence, sector flow and lone-large-positions in JS
for its Movers view. This repeats those definitions in text, because a model
cannot open a browser and 13,000 rows will not fit in a prompt anyway. The
definitions are deliberately identical: convergence ranked by DISTINCT members
(several members independently landing on one name beats one large index buy),
windowed on DISCLOSURE date because that is when a move became public.

What it adds over the page: forward returns from trade_returns, and a per-member
track record. Convergence alone tells you what Congress is doing; only the track
record speaks to whether copying any of them has ever paid.
"""
from __future__ import annotations

import datetime as dt
import statistics
from collections import defaultdict

from . import db, legislators
from .config import CONFIG

HORIZON = "ret_90"          # the closed window the track record is scored on


def load(days: int, floor: int, cfg=CONFIG):
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    with db.connect(cfg.db_path) as conn:
        members = {r["member"]: dict(r) for r in db.all_members(conn)}
        seats = db.committees_by_member(conn)
        sectors = {r["ticker"]: dict(r) for r in
                   conn.execute("SELECT * FROM ticker_sectors")}
        rows = [dict(r) for r in conn.execute(
            """SELECT t.*, r.ret_30, r.ret_90, r.ret_now
                 FROM congress_trades t
                 LEFT JOIN trade_returns r ON r.trade_id = t.id
                WHERE t.ticker != '' AND t.amount_min >= ?
                  AND COALESCE(NULLIF(t.disclosed,''), t.tx_date) >= ?""",
            (floor, since))]
        # The track record must span every closed window, not just this window.
        scored = [dict(r) for r in conn.execute(
            f"""SELECT t.member, t.tx_type, r.{HORIZON} AS ret
                  FROM congress_trades t
                  JOIN trade_returns r ON r.trade_id = t.id
                 WHERE t.ticker != '' AND t.amount_min >= ?
                   AND r.{HORIZON} IS NOT NULL""", (floor,))]
    return rows, members, seats, sectors, scored, since


def directional(tx_type: str, ret: float | None) -> float | None:
    """A sell that preceded a fall was a good call, so score every trade in the
    direction the member took rather than in raw price terms."""
    if ret is None:
        return None
    return -ret if tx_type == "sell" else ret


def convergence(rows, limit=20):
    by = defaultdict(lambda: {"buys": 0, "sells": 0, "members": set(), "max": 0,
                              "rets": []})
    for r in rows:
        o = by[r["ticker"]]
        o["buys" if r["tx_type"] == "buy" else "sells"] += 1
        o["members"].add(r["member"])
        o["max"] = max(o["max"], r["amount_min"] or 0)
        d = directional(r["tx_type"], r.get("ret_90"))
        if d is not None:
            o["rets"].append(d)
    out = [{"t": t, "n": len(o["members"]), "net": o["buys"] - o["sells"],
            "med": statistics.median(o["rets"]) if o["rets"] else None, **o}
           for t, o in by.items()]
    out.sort(key=lambda o: (-o["n"], -abs(o["net"]), -o["max"]))
    return out[:limit]


def sector_flow(rows, sectors, limit=12):
    by = defaultdict(lambda: {"buy": 0, "sell": 0, "tickers": set()})
    for r in rows:
        k = (sectors.get(r["ticker"]) or {}).get("sector") or "Unclassified"
        o = by[k]
        o["buy" if r["tx_type"] == "buy" else "sell"] += 1
        o["tickers"].add(r["ticker"])
    out = [{"sector": k, "net": v["buy"] - v["sell"], **v} for k, v in by.items()]
    out.sort(key=lambda o: -abs(o["net"]))
    return out[:limit]


def lone_large(rows, min_amount=100001, limit=12):
    """The opposite signal to convergence, which by construction hides the bet
    nobody else is in. 'Large' is judged against that member's own median trade,
    so it means unusual for them rather than large in absolute terms."""
    per_member = defaultdict(list)
    for r in rows:
        per_member[r["member"]].append(r["amount_min"] or 0)
    by = defaultdict(lambda: {"members": set(), "best": None})
    for r in rows:
        o = by[r["ticker"]]
        o["members"].add(r["member"])
        if not o["best"] or (r["amount_min"] or 0) > (o["best"]["amount_min"] or 0):
            o["best"] = r
    out = []
    for t, o in by.items():
        if len(o["members"]) != 1 or (o["best"]["amount_min"] or 0) < min_amount:
            continue
        med = statistics.median(per_member[o["best"]["member"]]) or 1
        out.append({"t": t, "r": o["best"], "mult": (o["best"]["amount_min"] or 0) / med})
    out.sort(key=lambda o: (-(o["r"]["amount_min"] or 0), -o["mult"]))
    return out[:limit]


def committee_overlap(rows, members, seats, sectors, limit=20):
    """Trades in a sector the member's own committee has jurisdiction over."""
    out = []
    for r in rows:
        sec = (sectors.get(r["ticker"]) or {}).get("sector")
        if not sec:
            continue
        bio = (members.get(r["member"]) or {}).get("bioguide") or ""
        covered = set()
        for s in seats.get(bio, []):
            covered.update(legislators.sectors_for_committee(s.get("name") or ""))
        if sec in covered:
            out.append({"r": r, "sector": sec})
    out.sort(key=lambda p: -(p["r"]["amount_min"] or 0))
    return out[:limit]


def track_record(scored, min_trades=20, limit=15):
    """Per member: share of closed 90-day windows that moved the way they traded.
    Small samples are noise, so members under min_trades are dropped rather than
    shown with a flattering 100%."""
    by = defaultdict(list)
    for r in scored:
        d = directional(r["tx_type"], r["ret"])
        if d is not None:
            by[r["member"]].append(d)
    out = [{"member": m, "n": len(v),
            "hit": sum(1 for x in v if x > 0) / len(v),
            "med": statistics.median(v)}
           for m, v in by.items() if len(v) >= min_trades]
    out.sort(key=lambda o: (-o["hit"], -o["med"]))
    return out[:limit]


def money(n):
    n = n or 0
    return f"${n/1e6:.1f}M" if n >= 1e6 else f"${round(n/1e3)}k" if n >= 1e3 else f"${n}"


def pct(x):
    return "—" if x is None else f"{x*100:+.1f}%"


def build(days=90, floor=15001, cfg=CONFIG) -> dict:
    rows, members, seats, sectors, scored, since = load(days, floor, cfg)
    return {
        "since": since, "days": days, "floor": floor,
        "disclosures": len(rows),
        "members_active": len({r["member"] for r in rows}),
        "priced": sum(1 for r in rows if r.get("ret_now") is not None),
        "convergence": convergence(rows),
        "sectors": sector_flow(rows, sectors),
        "lone_large": lone_large(rows),
        "committee_overlap": committee_overlap(rows, members, seats, sectors),
        "track_record": track_record(scored),
        "_sectors_by_ticker": {t: (v.get("sector") or "?") for t, v in sectors.items()},
    }


def to_markdown(d: dict) -> str:
    sec = d["_sectors_by_ticker"]
    L = [f"# Congressional trade digest — disclosures since {d['since']} "
         f"(floor {money(d['floor'])})", "",
         f"{d['disclosures']} tickered disclosures from {d['members_active']} members; "
         f"{d['priced']} have price data.", "",
         "Read these limits before drawing a conclusion:",
         "- Filings lag the actual trade by up to 45 days. Every price here is measured",
         "  from the DISCLOSURE date, i.e. what a reader could actually have acted on.",
         "- Amounts are the brackets members report, not real position sizes.",
         "- Returns are directional: a sell scores positive when the price then fell.",
         "- Nothing here is adjusted for the market's own move over the same window,",
         "  so a high hit rate in a rising market is not skill.", ""]

    L += ["## Convergence — most distinct members on one name",
          "| ticker | members | net | largest | sector | median 90d |",
          "|---|--:|--:|--:|---|--:|"]
    for o in d["convergence"]:
        L.append(f"| {o['t']} | {o['n']} | {o['net']:+d} | {money(o['max'])} "
                 f"| {sec.get(o['t'],'?')} | {pct(o['med'])} |")

    L += ["", "## Sector net flow (buys − sells)",
          "| sector | net | buys | sells | names |", "|---|--:|--:|--:|--:|"]
    for o in d["sectors"]:
        L.append(f"| {o['sector']} | {o['net']:+d} | {o['buy']} | {o['sell']} "
                 f"| {len(o['tickers'])} |")

    L += ["", "## Lone large positions — one member, nobody else",
          "| ticker | member | type | amount | × their median | disclosed | 90d |",
          "|---|---|---|--:|--:|---|--:|"]
    for o in d["lone_large"]:
        r = o["r"]
        L.append(f"| {o['t']} | {r['member']} | {r['tx_type']} | {money(r['amount_min'])} "
                 f"| {o['mult']:.0f}× | {r['disclosed']} "
                 f"| {pct(directional(r['tx_type'], r.get('ret_90')))} |")

    L += ["", f"## Committee overlap — traded a sector their committee oversees "
              f"({len(d['committee_overlap'])} shown)",
          "| member | ticker | sector | type | amount | disclosed |",
          "|---|---|---|---|--:|---|"]
    for o in d["committee_overlap"]:
        r = o["r"]
        L.append(f"| {r['member']} | {r['ticker']} | {o['sector']} | {r['tx_type']} "
                 f"| {money(r['amount_min'])} | {r['disclosed']} |")

    L += ["", "## Track record — closed 90-day windows, all history, min 20 trades",
          "| member | trades | went their way | median |", "|---|--:|--:|--:|"]
    for o in d["track_record"]:
        L.append(f"| {o['member']} | {o['n']} | {o['hit']*100:.0f}% | {pct(o['med'])} |")
    return "\n".join(L) + "\n"


def selftest(cfg=CONFIG):
    rows, members, seats, sectors, scored, _ = load(3650, 1, cfg)
    assert rows, "no rows loaded from congress.db"
    conv = convergence(rows)
    assert [o["n"] for o in conv] == sorted((o["n"] for o in conv), reverse=True), \
        "convergence not ranked by distinct member count"
    assert all(len(o["members"]) == o["n"] for o in conv), "member count mismatch"
    assert all(o["r"]["ticker"] == o["t"] for o in lone_large(rows))
    assert directional("sell", -0.1) == 0.1 and directional("buy", -0.1) == -0.1
    assert directional("sell", None) is None
    assert all(0 <= o["hit"] <= 1 and o["n"] >= 20 for o in track_record(scored))
    txt = to_markdown(build(90, cfg=cfg))
    for head in ("Convergence", "Committee overlap", "Track record"):
        assert head in txt, head
    print(f"selftest ok: {len(rows)} rows, {len(conv)} convergence names, "
          f"{len(scored)} scored trades")
