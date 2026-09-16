"""Federal lobbying disclosures (Lobbying Disclosure Act, LD-2 quarterly reports).

Registrants -- the lobbying firms and in-house lobbyists -- file an LD-2 every
quarter naming their client, the issue areas they worked, a free-text description
that often names a bill, and which chambers/agencies they lobbied. It is public
and, unlike `committees.py`'s Congress.gov source, needs no key at all:

    https://lda.gov/api/v1/filings/

Verified 2026-09-16 with a bare, anonymous `curl` (no `api_key` param, no
`Authorization` header) -- 200 OK with full JSON on every request tried,
including deep pagination and a `page_size` above the server's own cap. The
public-facing docs still say `lda.senate.gov`; that hostname now 301s to
`lda.gov`, which is what this module talks to. No documented anonymous rate
limit was found, so this throttles anyway (`PAUSE`) out of the same courtesy
the other collectors extend to sec.gov and Wikipedia.

What this data CAN support:
  - which issue areas (a fixed, ~80-code LDA vocabulary) a client lobbied on,
    in a given quarter, and roughly how much the registrant billed that quarter.
  - which chambers (House/Senate) and federal agencies were the lobbying target.

What it CANNOT support, and this module never implies otherwise:
  - WHICH MEMBER was lobbied. LD-2 filings name the client, the registrant, the
    lobbyists, and the chambers/agencies -- never an individual member of
    Congress. Any per-member framing here would be invented.
  - a dollar figure per issue. `income`/`expenses` are reported once for the
    WHOLE filing, which commonly lists several issue codes at once. Splitting
    that total across issues would be inventing a number the filer never gave.
    Every issue-level rollup below is a count of activity, or the parent
    filing's income counted once per filing touching that issue -- never a
    per-issue dollar figure.
  - an official issue -> industry mapping. `ISSUE_SECTORS` below is an editorial
    guess, exactly like `legislators.sectors_for_committee`, and exists to
    prompt a look, not to assert a finding.
"""
from __future__ import annotations

import datetime as dt
import time
from collections import defaultdict

import requests

from . import db
from .config import CONFIG, user_agent

API = "https://lda.gov/api/v1/filings/"
PAGE_SIZE = 25      # the server's own ceiling; asking for more is silently capped
# No rate limit is documented, but one exists: ~15 requests in fast succession
# draws a 429 with a `Retry-After` header (observed 21s). PAUSE keeps a normal
# run under that; `_get` still honours Retry-After as a backstop.
PAUSE = 1.5

# Issue code -> the sectors.py vocabulary it plausibly touches. EDITORIAL, NOT
# OFFICIAL, matched exactly like legislators.COMMITTEE_SECTORS -- a prompt to
# look, never a finding. Deliberately omitted: codes too broad to mean anything
# (BUD, CON, ECN, GOV, LAW, FOR...) or with no clean sector fit (FIR, SPO, REL,
# TOR, VET, WEL...). The full fixed vocabulary is `constants/filing/
# lobbyingactivityissues` on the API; this covers the ones worth rolling up.
ISSUE_SECTORS: dict[str, tuple[str, ...]] = {
    "AER": ("Transportation Equipment",),
    "AGR": ("Agriculture", "Food & Beverage"),
    "AUT": ("Transportation Equipment",),
    "AVI": ("Transportation & Logistics", "Transportation Equipment"),
    "BAN": ("Banking & Finance",),
    "BEV": ("Food & Beverage",),
    "CAW": ("Utilities & Power",),
    "CHM": ("Pharma & Chemicals",),
    "COM": ("Communications",),
    "CPI": ("Software & IT Services", "Machinery & Computer Equipment"),
    "DEF": ("Transportation Equipment", "Electronics & Electrical Equipment",
            "Instruments & Medical Devices"),
    "ENG": ("Utilities & Power", "Mining & Energy Extraction"),
    "FIN": ("Banking & Finance", "Insurance", "Funds & Holding Companies"),
    "FOO": ("Food & Beverage",),
    "FUE": ("Petroleum Refining", "Mining & Energy Extraction"),
    "HCR": ("Healthcare Services", "Pharma & Chemicals", "Instruments & Medical Devices"),
    "HOU": ("Real Estate", "Real Estate (REIT)"),
    "INS": ("Insurance",),
    "MAN": ("Machinery & Computer Equipment", "Metals & Fabrication", "Misc Manufacturing"),
    "MAR": ("Transportation & Logistics",),
    "MED": ("Instruments & Medical Devices", "Pharma & Chemicals"),
    "MMM": ("Healthcare Services", "Insurance"),
    "NAT": ("Mining & Energy Extraction", "Petroleum Refining", "Utilities & Power"),
    "PHA": ("Pharma & Chemicals",),
    "RES": ("Real Estate", "Real Estate (REIT)"),
    "RET": ("Funds & Holding Companies", "Banking & Finance"),
    "ROD": ("Construction & Engineering", "Transportation & Logistics"),
    "RRR": ("Transportation & Logistics",),
    "SCI": ("Software & IT Services", "Semiconductors", "Electronics & Electrical Equipment"),
    "TEC": ("Communications", "Communications Equipment", "Software & IT Services"),
    "TOB": ("Food & Beverage",),
    "TRA": ("Transportation & Logistics", "Transportation Equipment"),
    "TRD": ("Wholesale", "Retail"),
    "TRU": ("Transportation & Logistics",),
    "URB": ("Construction & Engineering", "Real Estate"),
    "UTI": ("Utilities & Power",),
    "WAS": ("Utilities & Power",),
}

_CHAMBERS = {"HOUSE OF REPRESENTATIVES": "House", "SENATE": "Senate"}


def sectors_for_issue(code: str) -> tuple[str, ...]:
    return ISSUE_SECTORS.get((code or "").upper(), ())


def _default_period(today: dt.date | None = None) -> tuple[int, str]:
    """The most recently COMPLETED quarter, not the live one.

    LD-2 reports are due 20 days after quarter end, so the current quarter is
    still filling in; 'recent' should mean the last one that had time to.
    """
    today = today or dt.date.today()
    q = (today.month - 1) // 3 + 1
    y, q = (today.year, q - 1) if q > 1 else (today.year - 1, 4)
    return y, {1: "first_quarter", 2: "second_quarter",
               3: "third_quarter", 4: "fourth_quarter"}[q]


def _get(url: str, params: dict | None, cfg) -> dict:
    r = requests.get(url, params=params, timeout=30,
                     headers={"User-Agent": user_agent(cfg)})
    if r.status_code == 429:
        wait = int(r.headers.get("Retry-After", "30"))
        time.sleep(wait)
        r = requests.get(url, params=params, timeout=30,
                         headers={"User-Agent": user_agent(cfg)})
    r.raise_for_status()
    return r.json()


def fetch_quarter(year: int, period: str, cfg=CONFIG, max_filings: int = 500,
                  progress=None) -> list[dict]:
    """Raw filing records for one quarter, newest-posted first, capped at
    `max_filings`.

    Bounded on purpose -- a full quarter is ~25-30k filings, and this project
    develops and tests against a sample rather than the historical archive (the
    same posture `committees.py` takes with `CONGRESSES`). Call it again with a
    higher cap, or across periods, for fuller coverage.
    """
    say = progress or (lambda *_: None)
    url: str | None = API
    params = {"filing_year": year, "filing_period": period,
              "page_size": PAGE_SIZE, "ordering": "-dt_posted"}
    out: list[dict] = []
    while url and len(out) < max_filings:
        d = _get(url, params, cfg)
        out += d.get("results", [])
        say(f"  {period} {year}: {len(out)} filings fetched (of {d.get('count', '?')})")
        url, params = d.get("next"), None   # `next` is already a full querystring
        if url:
            time.sleep(PAUSE)
    return out[:max_filings]


def _money(v) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def parse_filing(rec: dict) -> tuple[dict, list[dict]]:
    """One API filing record -> (lobbying_filings row, [lobbying_activities rows])."""
    client = rec.get("client") or {}
    registrant = rec.get("registrant") or {}
    filing = {
        "filing_uuid": rec["filing_uuid"],
        "filing_type": rec.get("filing_type") or "",
        "filing_type_display": rec.get("filing_type_display") or "",
        "filing_period": rec.get("filing_period") or "",
        "filing_year": rec.get("filing_year"),
        "client_name": (client.get("name") or "").strip(),
        "client_id": client.get("client_id"),
        "client_state": client.get("state") or "",
        "client_desc": client.get("general_description") or "",
        "registrant_id": registrant.get("id"),
        "registrant_name": (registrant.get("name") or "").strip(),
        "income": _money(rec.get("income")),
        "expenses": _money(rec.get("expenses")),
        "dt_posted": rec.get("dt_posted") or "",
        "doc_url": rec.get("filing_document_url") or "",
    }
    activities = []
    for i, la in enumerate(rec.get("lobbying_activities") or []):
        ents = [(e.get("name") or "").strip() for e in (la.get("government_entities") or [])]
        chambers = sorted({_CHAMBERS[e] for e in ents if e in _CHAMBERS})
        agencies = sorted({e for e in ents if e not in _CHAMBERS})
        activities.append({
            "filing_uuid": rec["filing_uuid"], "activity_idx": i,
            "issue_code": (la.get("general_issue_code") or "").strip(),
            "issue_desc": la.get("general_issue_code_display") or "",
            # Trimmed, not summarized -- descriptions run to several thousand
            # characters on record (see the fixture), and this table is for
            # counting/rolling up issues, not for reproducing the filing text.
            "description": (la.get("description") or "").strip()[:2000],
            "chambers": ",".join(chambers),
            "agencies": ",".join(agencies),
        })
    return filing, activities


def run(cfg=CONFIG, year: int | None = None, period: str | None = None,
        max_filings: int = 500, quiet: bool = False) -> dict:
    """Fetch one quarter (bounded) and store it. Idempotent: filings are
    immutable once posted, so re-running just overwrites with the same data."""
    say = (lambda *_: None) if quiet else print
    if not (year and period):
        year, period = _default_period()
    say(f"lobbying: fetching {period} {year}, capped at {max_filings} filings")
    recs = fetch_quarter(year, period, cfg, max_filings, progress=say)
    stored = 0
    with db.connect(cfg.db_path) as conn:
        for rec in recs:
            filing, activities = parse_filing(rec)
            db.upsert_lobbying_filing(conn, filing)
            db.replace_lobbying_activities(conn, filing["filing_uuid"], activities)
            stored += 1
    say(f"lobbying: stored {stored} filings for {period} {year}")
    return {"year": year, "period": period, "filings": stored}


# ---------------------------------------------------------------- report
def money(n) -> str:
    n = n or 0
    return f"${n/1e6:.1f}M" if n >= 1e6 else f"${round(n/1e3)}k" if n >= 1e3 else f"${n:.0f}"


def _load(cfg=CONFIG):
    with db.connect(cfg.db_path) as conn:
        filings = {r["filing_uuid"]: dict(r) for r in
                   conn.execute("SELECT * FROM lobbying_filings")}
        activities = [dict(r) for r in conn.execute("SELECT * FROM lobbying_activities")]
        trade_sectors = conn.execute(
            """SELECT s.sector, t.tx_type, count(*) n
                 FROM congress_trades t JOIN ticker_sectors s ON s.ticker = t.ticker
                WHERE t.ticker != '' GROUP BY s.sector, t.tx_type""").fetchall()
    return filings, activities, trade_sectors


def sector_summary(filings: dict, activities: list[dict]) -> list[dict]:
    """Per sector: lobbying activity count, distinct clients, and the SUM of
    each touching filing's whole income counted ONCE per filing (never split
    across the filing's issues -- see the module docstring)."""
    by_sector: dict[str, dict] = defaultdict(
        lambda: {"activities": 0, "clients": set(), "filings": set(),
                 "income": 0.0, "house": 0, "senate": 0})
    for a in activities:
        for sec in sectors_for_issue(a["issue_code"]):
            o = by_sector[sec]
            o["activities"] += 1
            fu = a["filing_uuid"]
            f = filings.get(fu)
            if f:
                o["clients"].add(f["client_name"])
                if fu not in o["filings"]:
                    o["filings"].add(fu)
                    o["income"] += f["income"] or 0.0
            ch = (a["chambers"] or "").split(",")
            if "House" in ch:
                o["house"] += 1
            if "Senate" in ch:
                o["senate"] += 1
    out = [{"sector": s, "activities": v["activities"], "clients": len(v["clients"]),
            "filings": len(v["filings"]), "income": v["income"],
            "house": v["house"], "senate": v["senate"]}
           for s, v in by_sector.items()]
    out.sort(key=lambda o: -o["income"])
    return out


def build(cfg=CONFIG, year: int | None = None, period: str | None = None) -> dict:
    filings, activities, trade_sectors = _load(cfg)
    if year or period:
        filings = {k: f for k, f in filings.items()
                  if (not year or f["filing_year"] == year)
                  and (not period or f["filing_period"] == period)}
        activities = [a for a in activities if a["filing_uuid"] in filings]
    trades_by_sector: dict[str, dict] = defaultdict(lambda: {"buy": 0, "sell": 0})
    for r in trade_sectors:
        k = "buy" if r["tx_type"] == "buy" else "sell"
        trades_by_sector[r["sector"]][k] = r["n"]
    summary = sector_summary(filings, activities)
    for o in summary:
        t = trades_by_sector.get(o["sector"], {"buy": 0, "sell": 0})
        o["member_net"] = t["buy"] - t["sell"]
        o["member_trades"] = t["buy"] + t["sell"]
    unmapped = sum(1 for a in activities if not sectors_for_issue(a["issue_code"]))
    return {"filings": len(filings), "activities": len(activities),
            "clients": len({f["client_name"] for f in filings.values()}),
            "registrants": len({f["registrant_name"] for f in filings.values()}),
            "unmapped_activities": unmapped, "sectors": summary,
            "span": (min((f["filing_period"], f["filing_year"]) for f in filings.values())
                     if filings else None)}


def to_markdown(d: dict) -> str:
    L = ["# Federal lobbying disclosures (LDA) vs. traded sectors", "",
         f"{d['filings']:,} LD-2 filings from {d['clients']:,} clients through "
         f"{d['registrants']:,} registrants, {d['activities']:,} issue-level "
         "lobbying activities.", "",
         "**Read this before the table below.** LD-2 filings name the client, "
         "the registrant, the lobbyists, and the chambers/agencies lobbied -- "
         "**never an individual member of Congress.** Nothing here links a "
         "lobbyist to a member, and none should be inferred. Reported income/"
         "expenses cover the WHOLE quarterly filing, which typically lists "
         "several issues at once; the totals below sum each filing's income "
         "ONCE per sector it touches, not split across issues, because the "
         "filer never reported a per-issue figure to split. The issue -> sector "
         "mapping (`ISSUE_SECTORS`) is an editorial guess over a fixed, coarse "
         f"~80-code vocabulary, exactly like the committee -> sector map -- "
         f"{d['unmapped_activities']:,} of {d['activities']:,} activities use a "
         "code this module does not roll up and are excluded below.", "",
         "| sector | lobbying activities | clients | filings | income "
         "(1x/filing) | House | Senate | member trades (net) |",
         "|---|--:|--:|--:|--:|--:|--:|--:|"]
    for o in d["sectors"]:
        L.append(f"| {o['sector']} | {o['activities']:,} | {o['clients']:,} "
                 f"| {o['filings']:,} | {money(o['income'])} | {o['house']:,} "
                 f"| {o['senate']:,} | {o['member_net']:+d} "
                 f"({o['member_trades']:,} trades) |")
    L += ["",
          "**'member trades' is NOT evidence of anything about this lobbying.** "
          "It is the same disclosed-trade count this project already computes "
          "per sector (all history, no date match to these filings), placed "
          "next to lobbying volume only so a sector that is heavily lobbied AND "
          "heavily traded is easy to spot for a human to look into -- not "
          "because the two are shown to be connected. A high row on both "
          "columns says Congress is active in that sector's *stocks* while "
          "lobbyists are active in its *issues*; it says nothing about who "
          "talked to whom, or whether either caused the other.",
          "",
          "Source: lda.gov/api/v1/filings/, keyless. Verified 2026-09-16 with a "
          "bare anonymous request -- no api_key parameter accepted or required, "
          "200 OK on every page tried. The public docs still point at "
          "lda.senate.gov, which now 301s to lda.gov."]
    return "\n".join(L) + "\n"


def selftest(cfg=CONFIG):
    with db.connect(cfg.db_path) as conn:
        n = db.lobbying_filing_count(conn)
    if not n:
        print("selftest skipped: no lobbying filings stored "
              "(run `python -c \"from congress_trades import lobbying; "
              "lobbying.run()\"`)")
        return
    d = build(cfg)
    assert d["filings"] > 0 and d["activities"] > 0
    assert d["unmapped_activities"] <= d["activities"]
    for o in d["sectors"]:
        assert o["income"] >= 0
        assert o["clients"] <= o["filings"]  # one client per filing, so this can't invert
    txt = to_markdown(d)
    assert "never an individual member" in txt
    assert "lda.gov" in txt
    # sectors_for_issue is a pure lookup; a few known codes must resolve and an
    # unknown one must come back empty rather than raising.
    assert "Healthcare Services" in sectors_for_issue("HCR")
    assert sectors_for_issue("ZZZ") == ()
    print(f"selftest ok: {d['filings']:,} filings, {d['activities']:,} activities, "
          f"{len(d['sectors'])} sectors mapped")
