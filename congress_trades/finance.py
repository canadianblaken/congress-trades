"""FEC campaign finance, cross-referenced with committee jurisdiction and trades.

The project already knows, per member: which committees they sit on, which
sectors those committees oversee (legislators.sectors_for_committee), and which
sectors they've traded (ticker_sectors). What's missing is money -- does a
member take PAC contributions from the industry their committee oversees, and
also trade in it? That three-way overlap is the new signal here; nothing else
in this module is interesting on its own.

Source: FEC bulk data, https://www.fec.gov/data/browse-data/?tab=bulk-data --
free, no key, one zip per file per two-year cycle. Five files:

    cn      candidate master        identity: name, office, state, district
    weball  candidate summary       top-line receipts, including PAC receipts
    cm      committee master        PAC name + connected organisation
    ccl     candidate-committee link  which committees are a candidate's OWN
            (used to exclude candidate-to-candidate transfers from "PAC money")
    pas2    committee->candidate contributions  the actual PAC dollars

Deliberately NOT used: `indiv` (individual contributions). It is gigabytes per
cycle and is a person's own money, not the industry-linked money this module
is about.

Each bulk file is a single pipe-delimited text file with no header row and no
stable field-count guarantee across ancient cycles, so every unpack is sliced
to a known width and skips short lines rather than raising.

Identity matching (FEC cand_id -> bioguide) is deliberately conservative: same
surname, same state, same chamber, same district (House), same first initial.
Any ambiguity -- multiple members clearing that bar -- is recorded as NO match.
A wrong attribution here would be indistinguishable from a real finding, which
is a much worse failure than a missing one.

Industry labels on PAC committees are a small keyword heuristic
(PAC_SECTOR_KEYWORDS), not an FEC-provided classification. Say so wherever the
result is shown, the same way legislators.COMMITTEE_SECTORS labels itself
editorial rather than official.
"""
from __future__ import annotations

import datetime as dt
import io
import os
import statistics
import zipfile
from pathlib import Path

import requests

from . import db
from .config import CONFIG, user_agent
from .legislators import _words, sectors_for_committee

BASE = "https://www.fec.gov/files/bulk-downloads"


def default_cycles() -> tuple[str, ...]:
    """Current + previous FEC 2-year cycle (each named by its even end-year).
    Override with CONGRESS_FEC_CYCLES='2026,2024' (comma separated)."""
    env = os.getenv("CONGRESS_FEC_CYCLES", "").strip()
    if env:
        return tuple(c.strip() for c in env.split(",") if c.strip())
    y = dt.date.today().year
    end = y if y % 2 == 0 else y + 1
    return (str(end), str(end - 2))


# ---------------------------------------------------------------- download
def _download(cfg, cycle: str, code: str) -> Path:
    """One FEC bulk file, cached permanently. A closed cycle's numbers never
    change, and re-pulling pas2 (tens of MB) on every run would be pure waste."""
    cache = cfg.cache_dir / "fec" / f"{code}{cycle}.zip"
    if cache.exists():
        return cache
    url = f"{BASE}/{cycle}/{code}{cycle[-2:]}.zip"
    r = requests.get(url, timeout=180, headers={"User-Agent": user_agent(cfg)})
    r.raise_for_status()
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(r.content)
    return cache


def _rows(path: Path):
    """Yield the pipe-split fields of the one file inside an FEC bulk zip.
    latin-1: FEC files carry the occasional accented name and are not UTF-8."""
    with zipfile.ZipFile(path) as z:
        with z.open(z.namelist()[0]) as f:
            for line in io.TextIOWrapper(f, encoding="latin-1"):
                line = line.rstrip("\n\r")
                if line:
                    yield line.split("|")


def _iso(mmddyyyy: str) -> str:
    if len(mmddyyyy) != 8 or not mmddyyyy.isdigit():
        return ""
    return f"{mmddyyyy[4:8]}-{mmddyyyy[0:2]}-{mmddyyyy[2:4]}"


# ---------------------------------------------------------------- identity matching
def _index_members(conn) -> dict[tuple[str, str, str], list[dict]]:
    """(surname last-token, state, chamber) -> candidate members.

    congress_members has no separate first/last columns, so the surname is
    approximated as the last token of full_name after suffix-stripping. That
    breaks for a multi-word surname ('Van Epps'), but the failure mode is a
    missed match, not a wrong one, and FEC's own name is the same shape on the
    other side of the comparison (see match_candidate) so it's symmetric.
    """
    out: dict[tuple[str, str, str], list[dict]] = {}
    seen: set[str] = set()
    for r in conn.execute(
            "SELECT bioguide, full_name, chamber, state, district "
            "FROM congress_members WHERE bioguide != '' ORDER BY current DESC"):
        if r["bioguide"] in seen:
            continue          # first row per bioguide wins (current spelling first)
        seen.add(r["bioguide"])
        w = _words(r["full_name"])
        if not w:
            continue
        key = (w[-1], (r["state"] or "").upper(), r["chamber"] or "")
        district = (r["district"] or "").lstrip("0") or "0"
        out.setdefault(key, []).append(
            {"bioguide": r["bioguide"], "first": w[0], "district": district})
    return out


def match_candidate(idx: dict, cand_name: str, office: str, state: str,
                     district: str) -> tuple[str | None, str]:
    """Resolve one FEC candidate row to (bioguide, reason). bioguide is None
    unless the match is unambiguous -- see this module's docstring."""
    chamber = {"H": "House", "S": "Senate"}.get(office)
    if not chamber:
        return None, "not-house-or-senate"
    last_part, _, first_part = cand_name.partition(",")
    lw, fw = _words(last_part), _words(first_part)
    if not lw or not fw:
        return None, "unparseable-name"
    cands = idx.get((lw[-1], (state or "").upper(), chamber), [])
    if not cands:
        return None, "no-surname-state-chamber-match"
    if chamber == "House":
        fec_dist = (district or "").lstrip("0") or "0"
        cands = [c for c in cands if c["district"] == fec_dist]
        if not cands:
            return None, "district-mismatch"
    cands = [c for c in cands if c["first"][:1] == fw[0][:1]]
    if len(cands) != 1:
        return None, "ambiguous"
    return cands[0]["bioguide"], "matched"


# ---------------------------------------------------------------- PAC -> sector, a heuristic
# The FEC publishes no industry code for a committee. This is a small keyword match
# against a committee's own name and its connected organisation, kept short and
# legible on purpose -- it exists to label PAC dollars with the same sector
# vocabulary trades already carry (sectors.py), not to be an authoritative industry
# classification. Unmatched committees get '' (no claim), never a guess.
PAC_SECTOR_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("pharmaceutical", "Pharma & Chemicals"),
    ("biotechnology", "Pharma & Chemicals"),
    ("chemical", "Pharma & Chemicals"),
    ("hospital", "Healthcare Services"),
    ("physicians", "Healthcare Services"),
    ("nurses", "Healthcare Services"),
    ("health care", "Healthcare Services"),
    ("healthcare", "Healthcare Services"),
    ("medical device", "Instruments & Medical Devices"),
    ("bankers", "Banking & Finance"),
    ("credit union", "Banking & Finance"),
    ("securities industry", "Banking & Finance"),
    ("financial services", "Banking & Finance"),
    ("insurance", "Insurance"),
    ("realtors", "Real Estate"),
    ("home builders", "Construction & Engineering"),
    ("construction", "Construction & Engineering"),
    ("engineers", "Construction & Engineering"),
    ("petroleum", "Petroleum Refining"),
    ("oil and gas", "Petroleum Refining"),
    ("mining", "Mining & Energy Extraction"),
    ("coal", "Mining & Energy Extraction"),
    ("energy", "Mining & Energy Extraction"),
    ("electric power", "Utilities & Power"),
    ("electric cooperative", "Utilities & Power"),
    ("utilities", "Utilities & Power"),
    ("telecommunications", "Communications"),
    ("broadcasters", "Communications"),
    ("cable television", "Communications"),
    ("wireless", "Communications"),
    ("airlines", "Transportation & Logistics"),
    ("trucking", "Transportation & Logistics"),
    ("railroads", "Transportation & Logistics"),
    ("automobile dealers", "Transportation Equipment"),
    ("aerospace", "Transportation Equipment"),
    ("defense industr", "Electronics & Electrical Equipment"),
    ("semiconductor", "Semiconductors"),
    ("software", "Software & IT Services"),
    ("internet", "Software & IT Services"),
    ("farm bureau", "Agriculture"),
    ("agricultur", "Agriculture"),
    ("dairy", "Food & Beverage"),
    ("beverage", "Food & Beverage"),
    ("grocers", "Food & Beverage"),
    ("restaurant", "Food & Beverage"),
)


def sector_for_committee(name: str, connected_org: str = "") -> str:
    text = f"{name or ''} {connected_org or ''}".lower()
    for kw, sector in PAC_SECTOR_KEYWORDS:
        if kw in text:
            return sector
    return ""


# ---------------------------------------------------------------- ingest (network)
def ingest(cfg=CONFIG, cycles: tuple[str, ...] | None = None, quiet: bool = False) -> dict:
    """Download (or reuse the cache) and load one or more FEC cycles."""
    say = (lambda *_: None) if quiet else print
    cycles = tuple(cycles) if cycles else default_cycles()
    totals = {"total": 0, "matched": 0, "ambiguous": 0, "committees": 0, "pac_pairs": 0}
    with db.connect(cfg.db_path) as conn:
        idx = _index_members(conn)
        for cycle in cycles:
            stats = _ingest_cycle(cfg, cycle, conn, idx, say)
            for k in totals:
                totals[k] += stats[k]
    say(f"ingest done: {totals['matched']}/{totals['total']} candidates matched "
        f"({totals['ambiguous']} ambiguous, skipped), {totals['pac_pairs']} "
        "PAC->candidate pairs stored")
    return totals


def _ingest_cycle(cfg, cycle: str, conn, idx: dict, say) -> dict:
    # 1. candidates + top-line receipts
    weball = {f[0]: f for f in _rows(_download(cfg, cycle, "weball")) if len(f) >= 26}
    total = matched = ambiguous = 0
    known_cands: set[str] = set()
    for f in _rows(_download(cfg, cycle, "cn")):
        if len(f) < 7 or f[5] not in ("H", "S"):
            continue
        cand_id, cand_name, party, _, state, office, district = f[:7]
        total += 1
        known_cands.add(cand_id)
        bioguide, reason = match_candidate(idx, cand_name, office, state, district)
        if bioguide:
            matched += 1
        elif reason == "ambiguous":
            ambiguous += 1
        w = weball.get(cand_id)
        ttl = float(w[5]) if w and w[5] else None
        pac = float(w[25]) if w and w[25] else None
        conn.execute(
            """INSERT INTO fec_candidates
               (cand_id, cycle, cand_name, office, state, district, party,
                ttl_receipts, pac_receipts, bioguide, match_method)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(cand_id, cycle) DO UPDATE SET
                 cand_name=excluded.cand_name, office=excluded.office,
                 state=excluded.state, district=excluded.district, party=excluded.party,
                 ttl_receipts=excluded.ttl_receipts, pac_receipts=excluded.pac_receipts,
                 bioguide=excluded.bioguide, match_method=excluded.match_method""",
            (cand_id, cycle, cand_name, office, state, district, party,
             ttl, pac, bioguide or "", reason))
    say(f"  cycle {cycle}: {total} House/Senate candidates, {matched} matched to a "
        f"bioguide, {ambiguous} ambiguous (skipped)")

    # 2. committees, classified by the keyword heuristic
    n_cmte = 0
    for f in _rows(_download(cfg, cycle, "cm")):
        if len(f) < 15:
            continue
        cmte_id, name = f[0], f[1]
        dsgn, cmte_tp, org_tp, connected = f[8], f[9], f[12], f[13]
        sector = sector_for_committee(name, connected)
        conn.execute(
            """INSERT INTO fec_committees (cmte_id, name, connected_org, cmte_type, sector_guess)
               VALUES (?,?,?,?,?)
               ON CONFLICT(cmte_id) DO UPDATE SET
                 name=excluded.name, connected_org=excluded.connected_org,
                 cmte_type=excluded.cmte_type, sector_guess=excluded.sector_guess""",
            (cmte_id, name, connected, cmte_tp, sector))
        n_cmte += 1
    say(f"  cycle {cycle}: {n_cmte} committees classified")

    # 3. a candidate's own committee(s) -- excluded below, this is candidate-to-
    #    candidate money (leadership PAC transfers etc), not industry PAC money.
    own_committees = {f[3] for f in _rows(_download(cfg, cycle, "ccl")) if len(f) >= 4}

    # 4. PAC -> candidate contributions, aggregated (one pas2 file is 300k-1M+
    #    individual disbursements; the per-transaction detail isn't the question
    #    here, and storing it would bloat the db for nothing this analysis uses)
    agg: dict[tuple[str, str], list] = {}
    for f in _rows(_download(cfg, cycle, "pas2")):
        if len(f) < 17:
            continue
        cmte_id, tx_tp, tx_dt, tx_amt, cand_id = f[0], f[5], f[13], f[14], f[16]
        if tx_tp != "24K" or cand_id not in known_cands or cmte_id in own_committees:
            continue
        try:
            amt = float(tx_amt)
        except ValueError:
            continue
        d = _iso(tx_dt)
        a = agg.setdefault((cmte_id, cand_id), [0.0, 0, d, d])
        a[0] += amt
        a[1] += 1
        if d and (not a[2] or d < a[2]):
            a[2] = d
        if d and (not a[3] or d > a[3]):
            a[3] = d
    for (cmte_id, cand_id), (amt, n, first_dt, last_dt) in agg.items():
        conn.execute(
            """INSERT INTO fec_pac_contributions
               (cycle, cmte_id, cand_id, amount, n_tx, first_dt, last_dt)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(cycle, cmte_id, cand_id) DO UPDATE SET
                 amount=excluded.amount, n_tx=excluded.n_tx,
                 first_dt=excluded.first_dt, last_dt=excluded.last_dt""",
            (cycle, cmte_id, cand_id, amt, n, first_dt, last_dt))
    say(f"  cycle {cycle}: {len(agg)} PAC->candidate pairs, "
        f"${sum(a[0] for a in agg.values()):,.0f} total")
    return {"total": total, "matched": matched, "ambiguous": ambiguous,
            "committees": n_cmte, "pac_pairs": len(agg)}


def _ensure_ingested(cfg, cycles: tuple[str, ...], quiet: bool = True) -> None:
    with db.connect(cfg.db_path) as conn:
        have = {r["cycle"] for r in conn.execute("SELECT DISTINCT cycle FROM fec_candidates")}
    missing = [c for c in cycles if c not in have]
    if missing:
        ingest(cfg, missing, quiet=quiet)


# ---------------------------------------------------------------- the three-way overlap
def _member_jurisdiction(conn) -> dict[str, tuple[str, set]]:
    """bioguide -> (display name, sectors any of their committee seats cover)."""
    names = {r["bioguide"]: r["full_name"] for r in conn.execute(
        "SELECT DISTINCT bioguide, full_name FROM congress_members WHERE bioguide != ''")}
    out: dict[str, tuple[str, set]] = {}
    for r in conn.execute("SELECT bioguide, name FROM member_committees"):
        secs = sectors_for_committee(r["name"])
        nm = names.get(r["bioguide"])
        if not secs or nm is None:
            continue
        if r["bioguide"] not in out:
            out[r["bioguide"]] = (nm, set(secs))
        else:
            out[r["bioguide"]][1].update(secs)
    return out


def _member_trade_sectors(conn) -> dict[str, dict[str, dict]]:
    """bioguide -> {sector: {n, notional}}, from the trades already collected."""
    bio_by_member = {r["member"]: r["bioguide"] for r in conn.execute(
        "SELECT member, bioguide FROM congress_members WHERE bioguide != ''")}
    out: dict[str, dict[str, dict]] = {}
    for r in conn.execute(
            """SELECT t.member, t.amount_min, s.sector
               FROM congress_trades t JOIN ticker_sectors s ON s.ticker = t.ticker
               WHERE t.ticker != '' AND s.sector != ''"""):
        bio = bio_by_member.get(r["member"])
        if not bio:
            continue
        e = out.setdefault(bio, {}).setdefault(r["sector"], {"n": 0, "notional": 0})
        e["n"] += 1
        e["notional"] += r["amount_min"] or 0
    return out


def _member_pac_by_sector(conn, cycles: tuple[str, ...]) -> tuple[dict, dict]:
    """bioguide -> {sector: dollars}, and bioguide -> {total, unclassified}."""
    q = f"""SELECT fc.bioguide, p.amount, cm.sector_guess
            FROM fec_pac_contributions p
            JOIN fec_candidates fc ON fc.cand_id = p.cand_id AND fc.cycle = p.cycle
            JOIN fec_committees cm ON cm.cmte_id = p.cmte_id
            WHERE fc.bioguide != '' AND p.cycle IN ({",".join("?" * len(cycles))})"""
    by_sector: dict[str, dict[str, float]] = {}
    totals: dict[str, dict[str, float]] = {}
    for r in conn.execute(q, cycles):
        t = totals.setdefault(r["bioguide"], {"total": 0.0, "unclassified": 0.0})
        t["total"] += r["amount"] or 0
        sec = r["sector_guess"] or ""
        if not sec:
            t["unclassified"] += r["amount"] or 0
            continue
        d = by_sector.setdefault(r["bioguide"], {})
        d[sec] = d.get(sec, 0.0) + (r["amount"] or 0)
    return by_sector, totals


def run(cycles: tuple[str, ...] | None = None, cfg=CONFIG) -> dict:
    """The three-way overlap: committee jurisdiction x PAC money x trades, per member.

    Ingests the requested cycles first if they aren't already stored (a first
    run downloads; every run after that reads the disk cache).
    """
    cycles = tuple(cycles) if cycles else default_cycles()
    _ensure_ingested(cfg, cycles)
    with db.connect(cfg.db_path) as conn:
        jurisdiction = _member_jurisdiction(conn)
        trade_sectors = _member_trade_sectors(conn)
        pac_by_sector, pac_totals = _member_pac_by_sector(conn, cycles)
        qmarks = ",".join("?" * len(cycles))
        row = conn.execute(
            f"SELECT count(*), sum(bioguide != ''), sum(match_method = 'ambiguous') "
            f"FROM fec_candidates WHERE cycle IN ({qmarks})", cycles).fetchone()
        total, matched, ambiguous = (row[0] or 0, row[1] or 0, row[2] or 0)
        # The FEC-wide ratio above makes the matcher look bad by construction --
        # most FEC candidate records are challengers who never sat in Congress and
        # were never going to match. The number that actually describes matcher
        # quality is roster coverage: of the people this project already tracks,
        # how many were found in FEC's data at all.
        roster_n = conn.execute(
            "SELECT count(DISTINCT bioguide) FROM congress_members "
            "WHERE bioguide != ''").fetchone()[0]
        roster_matched = conn.execute(
            f"SELECT count(DISTINCT bioguide) FROM fec_candidates "
            f"WHERE bioguide != '' AND cycle IN ({qmarks})", cycles).fetchone()[0]
        # How much of the PAC money the keyword heuristic can actually see. This
        # governs how the whole table reads: a member appearing here took money
        # this module could label, but a member NOT appearing may simply have
        # taken money from PACs it cannot label, which is most of them.
        cov_n, cov_lab = conn.execute(
            "SELECT count(*), sum(sector_guess != '' AND sector_guess IS NOT NULL) "
            "FROM fec_committees").fetchone()
        cov_amt = conn.execute(
            f"SELECT sum(p.amount), sum(CASE WHEN f.sector_guess != '' AND "
            f"f.sector_guess IS NOT NULL THEN p.amount ELSE 0 END) "
            f"FROM fec_pac_contributions p JOIN fec_committees f "
            f"ON f.cmte_id = p.cmte_id WHERE p.cycle IN ({qmarks})", cycles).fetchone()
        # PAC share of receipts, among matched members with a usable denominator --
        # the "PAC money is a minority of most members' receipts" caveat, measured
        # rather than asserted.
        shares = [r["pac_receipts"] / r["ttl_receipts"] for r in conn.execute(
            f"SELECT pac_receipts, ttl_receipts FROM fec_candidates "
            f"WHERE cycle IN ({qmarks}) AND bioguide != '' AND ttl_receipts > 0 "
            f"AND pac_receipts IS NOT NULL", cycles)]

    overlaps = []
    for bio, (name, jsecs) in jurisdiction.items():
        tsecs = set(trade_sectors.get(bio, {}))
        psecs = set(pac_by_sector.get(bio, {}))
        both = jsecs & tsecs & psecs
        if not both:
            continue
        overlaps.append({
            "member": name, "bioguide": bio, "sectors": sorted(both),
            "pac_dollars": sum(pac_by_sector[bio][s] for s in both),
            "pac_total": pac_totals.get(bio, {}).get("total", 0.0),
            "trade_count": sum(trade_sectors[bio][s]["n"] for s in both),
            "trade_notional": sum(trade_sectors[bio][s]["notional"] for s in both),
        })
    overlaps.sort(key=lambda o: -o["pac_dollars"])

    with_jur = len(jurisdiction)
    with_pac_in_jur = sum(1 for b, (_, j) in jurisdiction.items()
                          if set(pac_by_sector.get(b, {})) & j)
    with_trade_in_jur = sum(1 for b, (_, j) in jurisdiction.items()
                            if set(trade_sectors.get(b, {})) & j)

    return {
        "cycles": cycles,
        "match": {"total": total, "matched": matched, "ambiguous": ambiguous,
                  "rate": (matched / total) if total else None,
                  "roster_n": roster_n, "roster_matched": roster_matched,
                  "roster_rate": (roster_matched / roster_n) if roster_n else None},
        "coverage": {"cmte_n": cov_n or 0, "cmte_labelled": cov_lab or 0,
                     "amt_total": cov_amt[0] or 0.0, "amt_labelled": cov_amt[1] or 0.0},
        "pac_share_of_receipts": statistics.median(shares) if shares else None,
        "pac_share_n": len(shares),
        "members_with_jurisdiction": with_jur,
        "members_with_pac_in_jurisdiction": with_pac_in_jur,
        "members_trading_in_jurisdiction": with_trade_in_jur,
        "overlaps": overlaps,
    }


def _usd(x) -> str:
    return "—" if x is None else f"${x:,.0f}"


def to_markdown(d: dict) -> str:
    m = d["match"]
    L = [f"# Committee jurisdiction x PAC money x trades — {', '.join(d['cycles'])}",
         "",
         f"Of {m['total']:,} FEC House/Senate candidate records in these cycles, "
         f"{m['matched']:,} were matched to a bioguide on this project's roster "
         f"({m['rate']*100:.0f}% of all FEC candidates -- low by construction, since "
         "most FEC candidate records are challengers who never sat in Congress) and "
         f"{m['ambiguous']:,} were ambiguous and deliberately left unmatched. A wrong "
         "attribution here would look exactly like a real finding, so ambiguity is "
         "resolved toward no match, not a guess.", "",
         f"The number that actually describes the matcher: of "
         f"{m['roster_n']:,} members on this project's own roster, "
         f"**{m['roster_matched']:,} ({m['roster_rate']*100:.0f}%) were found in FEC's "
         "data** for these cycles. The rest are members whose FEC filing name, state, "
         "chamber or district didn't line up exactly with the roster -- most likely "
         "redistricting between cycles, not a bug.", "",
         f"{d['members_with_jurisdiction']} members have a committee seat whose "
         f"jurisdiction maps to a sector (legislators.sectors_for_committee). Of "
         f"those, {d['members_with_pac_in_jurisdiction']} took PAC money from a "
         f"committee this module's keyword heuristic labels as that same sector, "
         f"and {d['members_trading_in_jurisdiction']} traded a stock in it. "
         f"**{len(d['overlaps'])} members hit all three.**", "",
         (lambda c:
          "**The keyword heuristic sees a minority of the money, so read this "
          "table in one direction only.** It labels "
          f"{c['cmte_labelled']:,} of {c['cmte_n']:,} PAC committees "
          f"({c['cmte_labelled']*100/c['cmte_n']:.1f}%), carrying "
          f"{_usd(c['amt_labelled'])} of {_usd(c['amt_total'])} in contributions "
          f"({c['amt_labelled']*100/c['amt_total']:.0f}%). Industry PACs that name "
          "their trade ('bankers', 'realtors', 'farm bureau') are caught; "
          "leadership PACs, single-company PACs and anything named after a person "
          "or an acronym are not. So a member IN this table took money this module "
          "could label -- but a member's ABSENCE from it is close to meaningless, "
          "because most PAC money is unlabelled. Absence is not evidence of "
          "not taking industry money."
          )(d["coverage"]) if d.get("coverage", {}).get("cmte_n") else "", ""]

    if d["pac_share_of_receipts"] is not None:
        L += [f"Among {d['pac_share_n']} matched candidates with usable receipt "
              f"totals, PAC money is a median **{d['pac_share_of_receipts']*100:.0f}%** "
              "of total receipts -- most of a typical campaign's money is not this "
              "kind of money.", ""]

    if not d["overlaps"]:
        L.append("No member currently clears jurisdiction + PAC money + a trade all "
                 "in the same heuristic sector.")
    else:
        L += ["| member | sectors | PAC $ in that sector | PAC $ total | trades | "
              "trade $ (min bracket) |",
              "|---|---|--:|--:|--:|--:|"]
        for o in d["overlaps"]:
            L.append(f"| {o['member']} | {', '.join(o['sectors'])} "
                     f"| {_usd(o['pac_dollars'])} | {_usd(o['pac_total'])} "
                     f"| {o['trade_count']} | {_usd(o['trade_notional'])} |")

    L += ["", "## What this does not show", "",
         "- **A donation is not evidence of a quid pro quo.** Industry PACs give "
         "to committee members almost by default -- it is a relationship signal, "
         "not a transaction. This table is a prompt to go look at a specific "
         "member and sector, never a finding on its own.",
         "- **The sector label on a PAC is a small keyword guess "
         "(PAC_SECTOR_KEYWORDS in finance.py)**, not an FEC-provided classification. "
         "It is deliberately shallow and an unmatched committee gets no label at "
         "all rather than a forced one, which means real industry money is "
         "undercounted here, never overcounted.",
         "- **The committee->sector map itself is editorial** "
         "(legislators.COMMITTEE_SECTORS): broad committees (Appropriations, "
         "Rules, Oversight...) are excluded because they would match nearly "
         "everything.",
         "- **PAC money is a minority of most members' receipts** (see the median "
         "share above); a member's trading pattern is far more likely explained by "
         "wealth and background than by a PAC check.",
         "- **The match rate is not a matcher failure rate.** Most FEC candidate "
         "records are people who ran and lost, or ran for an office this project "
         "does not track; they were never supposed to match.",
         "- **A trade and a donation sharing a sector label says nothing about "
         "timing.** Unlike `congress-trades timing`, this module does not check "
         "whether the donation came before the trade, before the committee "
         "assignment, or years apart."]
    return "\n".join(L) + "\n"


def selftest(cfg=CONFIG):
    d = run(cfg=cfg)
    m = d["match"]
    assert m["total"] >= 0
    if m["total"] == 0:
        print("selftest skipped: no FEC candidates ingested")
        return
    assert m["matched"] <= m["total"]
    assert m["rate"] is None or 0 <= m["rate"] <= 1
    assert m["roster_matched"] <= m["roster_n"]
    assert m["roster_rate"] is None or 0 <= m["roster_rate"] <= 1
    for o in d["overlaps"]:
        assert o["sectors"], "an overlap row with no sectors should not exist"
        assert o["pac_dollars"] <= o["pac_total"] + 1e-6, \
            "sector-scoped PAC dollars exceed the member's total"
        assert o["pac_dollars"] > 0 and o["trade_count"] > 0
    assert d["members_with_pac_in_jurisdiction"] <= d["members_with_jurisdiction"]
    assert d["members_trading_in_jurisdiction"] <= d["members_with_jurisdiction"]
    txt = to_markdown(d)
    assert "PAC money" in txt and "does not show" in txt
    print(f"selftest ok: {m['roster_matched']}/{m['roster_n']} roster members found "
         f"in FEC data ({m['roster_rate']*100:.0f}%), {len(d['overlaps'])} members "
         "with a full jurisdiction+PAC+trade overlap")
