"""The steps, as plain functions. `__main__` wraps these in a CLI."""
from __future__ import annotations

import logging

from . import collect, db, legislators, sectors, votes
from .config import CONFIG, Config

log = logging.getLogger("congress_trades")


def backfill(cfg: Config = CONFIG, chamber: str = "both",
             years: list[str] | None = None, quiet: bool = False) -> int:
    """Collect filings into the database. Idempotent: cached filings are not refetched."""
    say = (lambda m: None) if quiet else (lambda m: print(f"  {m}", flush=True))
    years = years or list(cfg.years)
    rows: list[dict] = []
    if chamber in ("house", "both"):
        rows += collect.house_transactions(cfg, years, progress=say)
    if chamber in ("senate", "both"):
        rows += collect.senate_transactions(cfg, f"01/01/{min(years)}", progress=say)

    kept = collect.normalize(rows, cfg.min_amount)
    with db.connect(cfg.db_path) as conn:
        new = db.upsert_trades(conn, kept)
        total = conn.execute("SELECT COUNT(*) FROM congress_trades").fetchone()[0]
        members = conn.execute(
            "SELECT COUNT(DISTINCT member) FROM congress_trades").fetchone()[0]
    print(f"parsed {len(rows)} transactions, {len(kept)} stored above "
          f"${cfg.min_amount:,}; inserted {new} new -> {total} rows, {members} members")
    return 0


def enrich(cfg: Config = CONFIG, rotate: int = 0, no_wiki: bool = False,
           refresh_roster: bool = False, quiet: bool = False) -> int:
    """Resolve filers to real legislators and attach bios and committee seats.

    Without --rotate this fills in anyone unresolved and makes no network calls for
    members already known. With --rotate N it re-checks the N stalest, which is how
    you keep it current on a daily schedule without tripping Wikipedia's rate limit.
    """
    say = (lambda m: None) if quiet else (lambda m: print(f"  {m}", flush=True))
    idx = legislators.build_index(cfg, force=refresh_roster)
    try:
        comms = legislators.committees(cfg, force=refresh_roster)
    except Exception as e:                       # committees are optional enrichment
        log.warning("committee data unavailable: %s", e)
        comms = {}

    matched = unmatched = wikis = kept = seats = 0
    misses: list[str] = []
    refreshed = refresh_roster
    with db.connect(cfg.db_path) as conn:
        rows = db.stale_members(conn, rotate) if rotate else db.distinct_filers(conn)
        stored = {r["member"]: dict(r) for r in db.all_members(conn)}
        for i, r in enumerate(rows):
            leg = legislators.match(idx, r["member"], r["chamber"], r["state"] or "")
            if not leg and not refreshed:
                # An unknown name is the one reliable sign the roster is stale: a
                # special election or appointment seated someone since we last looked.
                say("unknown member, refreshing roster")
                idx = legislators.build_index(cfg, force=True)
                refreshed = True
                leg = legislators.match(idx, r["member"], r["chamber"], r["state"] or "")
            if not leg:
                unmatched += 1
                misses.append(r["member"])
                continue
            matched += 1
            prev = stored.get(r["member"], {})
            wiki = {} if no_wiki else legislators.wiki_summary(
                cfg, leg["wikipedia"], max_age=0 if rotate else None)
            if wiki:
                wikis += 1
            elif prev.get("wiki_extract"):
                # A failed re-check must never blank a bio we already have.
                wiki = {"title": prev.get("wiki_title", ""), "description": prev.get("wiki_desc", ""),
                        "extract": prev.get("wiki_extract", ""), "thumb": prev.get("wiki_thumb", ""),
                        "url": prev.get("wiki_url", "")}
                kept += 1
            db.upsert_member(conn, r["member"], {
                "bioguide": leg["bioguide"], "full_name": leg["full"],
                "chamber": leg["chamber"], "state": leg["state"],
                "district": str(leg["district"]) if leg["district"] is not None else "",
                "party": leg["party"], "official_url": leg["url"],
                "birthday": leg["birthday"], "current": int(leg["current"]),
                "wiki_title": wiki.get("title", leg["wikipedia"]),
                "wiki_desc": wiki.get("description", ""),
                "wiki_extract": wiki.get("extract", ""),
                "wiki_thumb": wiki.get("thumb", ""),
                "wiki_url": wiki.get("url", "")})
            if comms.get(leg["bioguide"]):
                db.replace_committees(conn, leg["bioguide"], comms[leg["bioguide"]])
                seats += 1
            # the Senate filing index carries no state; backfill it onto the trades
            if leg["state"]:
                conn.execute(
                    "UPDATE congress_trades SET state=? WHERE member=? AND (state='' OR state IS NULL)",
                    (leg["state"], r["member"]))
            if i % 25 == 0:
                say(f"{i}/{len(rows)} members")
            conn.commit()

    mode = f"rotate {rotate}" if rotate else "fill"
    print(f"[{mode}] matched {matched}/{matched + unmatched}, {wikis} bios fetched"
          + (f", {kept} kept after a failed re-check" if kept else "")
          + (f", {seats} with committee seats" if seats else ""))
    if misses:
        print("unmatched:", ", ".join(misses))
    return 0


def tag_sectors(cfg: Config = CONFIG, limit: int = 0, quiet: bool = False) -> int:
    """Attach an SEC industry classification to each traded ticker."""
    say = (lambda m: None) if quiet else (lambda m: print(f"  {m}", flush=True))
    with db.connect(cfg.db_path) as conn:
        todo = db.untagged_tickers(conn, limit)
        if not todo:
            print("all tickers already classified")
            return 0
        tmap = sectors.ticker_map(cfg)
        found = missing = 0
        for i, t in enumerate(todo):
            rec = sectors.lookup(cfg, t, tmap)
            if rec:
                db.upsert_sector(conn, rec)
                found += 1
            else:
                # Most misses are ETFs, foreign listings and delisted names: the SEC
                # ticker file only covers domestic registrants. Record them so we do
                # not retry every run.
                db.upsert_sector(conn, {"ticker": t, "sector": "Unclassified"})
                missing += 1
            if i % 50 == 0:
                say(f"{i}/{len(todo)} tickers")
            conn.commit()
        total = conn.execute("SELECT COUNT(*) FROM ticker_sectors").fetchone()[0]
    print(f"classified {found}, unmatched {missing} -> {total} tagged")
    return 0


def score_votes(cfg: Config = CONFIG, congress: int = 0) -> int:
    """Party-unity and DW-NOMINATE scores from Voteview roll calls."""
    congress = congress or cfg.congress_number
    scores, meta = votes.unity_scores(cfg, congress)
    hit = 0
    with db.connect(cfg.db_path) as conn:
        for r in db.all_members(conn):
            sc = scores.get(r["bioguide"] or "")
            if not sc:
                continue
            conn.execute(
                """UPDATE congress_members SET party_unity=?, votes_cast=?, nominate=?,
                   votes_congress=? WHERE member=?""",
                (sc["party_unity"], sc["votes_cast"], sc["nominate"],
                 meta["congress"], r["member"]))
            hit += 1
        conn.commit()
        total = conn.execute("SELECT COUNT(*) FROM congress_members").fetchone()[0]
    print(f"party unity for {hit}/{total} members (congress {meta['congress']})")
    return 0
