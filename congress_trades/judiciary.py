"""Federal judiciary financial disclosures (Courthouse Ethics and Transparency
Act, 2022) -- what federal judges own, from the same style of public filing
STOCK Act trades come from, so a judge sitting on a case involving a company
they hold is visible the same way a member trading a bill they oversee is.

Why this reads Free Law Project's bulk CSV snapshots rather than the
judiciary's own database:

  pub.jefs.uscourts.gov is the AO's CETA-mandated online database, live since
  November 2022. It requires a fresh registration on *every visit* -- full
  name, occupation, address, and who you represent, matching the statute's own
  wording ("you must provide your name, occupation and address") -- behind a
  Google reCAPTCHA (`grecaptcha.execute()` in its registration form) and an "I
  agree to the [statutory] prohibitions" checkbox. Pre-2022 reports, and every
  judicial-*employee* report, require Form AO-10A instead -- a mail/fax/email
  request to the AO's Financial Disclosure Staff, not a web form at all. This
  project does not bypass a CAPTCHA or an access control, so this source is a
  documented dead end -- the same place OGE Form 278e sits in the README.

  CourtListener (courtlistener.com) republishes the reports it scraped from
  that database, and its REST API answers anonymous GET requests with no key
  at all -- but its own robots.txt explicitly disallows automated agents from
  every path except a handful of informational ones, and it names AI agents by
  user-agent string to do it (`ClaudeBot`, `GPTBot`, and `Claude-User`
  specifically). The same robots.txt says why: "If you would like to crawl
  CourtListener, please contact us. We also have an extensive REST API and
  provide bulk data" -- it is pointing bots at the sanctioned alternative.

  That alternative is what this module reads: quarterly bulk CSV snapshots on
  a separate S3 host (com-courtlistener-storage.s3-us-west-2.amazonaws.com,
  which publishes no robots.txt of its own), marked public domain, no login,
  no key. Structurally this is the same shape as this project's other
  sources -- a bulk index plus detail rows, fetched a handful of times and
  cached -- not a crawl of the site robots.txt disallows.

Coverage and data-quality limits, found while building this:

  The `financial-disclosures` index CSV is badly corrupted by Free Law
  Project's own export, unrelated to anything upstream: its free-text
  `addendum_content_raw` column holds judges' hand-written amendment notes,
  some containing a literal, unescaped quote character, which desyncs csv's
  column boundaries for every row after it until a stray quote happens to
  resync it. Of the 108,948 logical rows csv.DictReader yields from the
  2026-06-30 snapshot, only 37,010 have a numeric `id` -- and even that is
  not enough: with ~37,000 real disclosures spread over a ~40,000-wide id
  space, a garbage fragment that happens to look like a small integer will
  usually collide with a *real* id, silently overwriting a genuine row with
  corrupted year/person/report_type fields keyed under someone else's id.
  `parse_disclosures` additionally requires `date_created` to look like a
  real timestamp, which a coincidental numeric fragment essentially never
  does; this combination recovers exactly 32,336 rows, matching Free Law
  Project's own published count for this dataset. That is the same kind of
  policy `collect.py` applies to a hand-typed notification date that reads
  `03/28/1935` -- a corrupt row is worse than no row, and a wrong row is
  worse than a missing one. `investments` rows whose
  `financial_disclosure_id` points at a dropped row are dropped too (counted
  separately as "orphaned"), since there is nothing to attribute them to.

  There is no ticker. A judicial disclosure names a holding as filed --
  "Vanguard Total Stock Market Index Fund", "TCF Financial Corp." -- with none
  of the "(AAPL)" parenthetical congressional PTRs carry. Nothing here is
  priced, scored, or matched to a ticker by guesswork; `description` is stored
  exactly as filed and that is the whole of what this module claims about it.
"""
from __future__ import annotations

import bz2
import csv
import logging
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime

import requests

from . import db
from .config import Config, CONFIG, user_agent

log = logging.getLogger("hermes_trends.judiciary")
TIMEOUT = 60
BULK_BASE = "https://com-courtlistener-storage.s3-us-west-2.amazonaws.com"
_S3_NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}

# Several dataset names share a stem in the bulk-data/ prefix
# ("financial-disclosures-" also prefixes -agreements-, -debts-, -positions-
# and more), so each is matched by a prefix for the S3 listing call and then
# an anchored regex against the date suffix, not a loose prefix alone.
_DATASETS = {
    "disclosures": ("bulk-data/financial-disclosures-",
                    re.compile(r"^bulk-data/financial-disclosures-\d{4}-\d{2}-\d{2}\.csv\.bz2$")),
    "investments": ("bulk-data/financial-disclosure-investments-",
                    re.compile(r"^bulk-data/financial-disclosure-investments-\d{4}-\d{2}-\d{2}\.csv\.bz2$")),
    "people": ("bulk-data/people-db-people-",
               re.compile(r"^bulk-data/people-db-people-\d{4}-\d{2}-\d{2}\.csv\.bz2$")),
    "positions": ("bulk-data/people-db-positions-",
                  re.compile(r"^bulk-data/people-db-positions-\d{4}-\d{2}-\d{2}\.csv\.bz2$")),
    "courts": ("bulk-data/courts-",
               re.compile(r"^bulk-data/courts-\d{4}-\d{2}-\d{2}\.csv\.bz2$")),
}

_YEAR = re.compile(r"^(19|20)\d{2}$")
_ID = re.compile(r"^\d+$")
# A real row's date_created is a Postgres timestamp. Corruption can still land a
# numeric-looking fragment in the id column (see parse_disclosures), and with
# ~37,000 real ids in a ~40,000-wide id space almost any stray few-digit number
# collides with a real one -- so id alone is not enough to tell a genuine row
# from a coincidence. This is what actually distinguishes them.
_TIMESTAMP = re.compile(r"^(19|20)\d{2}-\d{2}-\d{2} ")

# Judicial-officer position_type codes in people-db-positions. Excludes
# 'clerk', 'prac' (practicing attorney), 'prof', 'legis', 'mayor' and similar
# non-bench roles that live in the same table -- CourtListener's people-db
# covers every kind of legal-adjacent career, not just judges.
_JUDICIAL_TYPES = {
    "jud", "pres-jud", "trial-jud", "mag", "jus", "act-jus", "ass-jus",
    "ad-law-jud", "sup-jud", "asst-pres-jud", "ass-jud", "c-jud", "chief-mag",
    "spec-mag", "spec-tr-jud", "chief-jud", "act-jud",
}

# AO-10's own dollar-range tables (Rev. 3/2023), kept for display only -- rows
# in the database keep the letter code, since re-deriving a range from a code
# the form itself defines already is safer than a display-time guess.
INCOME_CODES = {
    "A": "$1,000 or less", "B": "$1,001-$2,500", "C": "$2,501-$5,000",
    "D": "$5,001-$15,000", "E": "$15,001-$50,000", "F": "$50,001-$100,000",
    "G": "$100,001-$1,000,000", "H1": "$1,000,001-$5,000,000",
    "H2": "more than $5,000,000",
}
VALUE_CODES = {
    "J": "$15,000 or less", "K": "$15,001-$50,000", "L": "$50,001-$100,000",
    "M": "$100,001-$250,000", "N": "$250,001-$500,000", "O": "$500,001-$1,000,000",
    "P1": "$1,000,001-$5,000,000", "P2": "$5,000,001-$25,000,000",
    "P3": "$25,000,001-$50,000,000", "P4": "more than $50,000,000",
}

# Free-text transaction verbs, transcribed by hand and by OCR across decades
# of forms -- "Buy (add'l)", "Sold (part)", "BUY", "sell" all appear in the
# same column. Matched by stem rather than an exhaustive list of variants.
_BUY = re.compile(r"buy|purchas", re.I)
_SELL = re.compile(r"sold|sell|sale|redeem|matur", re.I)

# Roughly the currently authorized federal bench: 870 Article III judgeships
# (uscourts.gov, Feb 2026) + 345 bankruptcy judgeships (fjc.gov, FY2025) + 563
# full-time magistrate judges (uscourts.gov, FY2025). Excludes senior,
# recalled and part-time judges, who also file and do appear in this data --
# so any fraction computed against it is a floor on true coverage, not a
# ceiling. There is no single authoritative headcount that includes all of
# those categories for a specific year.
BENCH_SIZE = 870 + 345 + 563


def norm_tx_type(raw: str) -> str:
    """Free-text transaction verb -> buy | sell | other | ''."""
    r = (raw or "").strip()
    if not r:
        return ""
    if _BUY.search(r):
        return "buy"
    if _SELL.search(r):
        return "sell"
    return "other"


# --------------------------------------------------------------- bulk fetch
def _latest_key(cfg: Config, dataset: str) -> str:
    """The newest snapshot filename for one dataset, via S3's public (keyless)
    ListObjectsV2 -- the same discovery step house_filings does against a
    year's index ZIP, just against a bucket listing instead of an XML index."""
    prefix, pat = _DATASETS[dataset]
    r = requests.get(BULK_BASE + "/", params={"list-type": "2", "prefix": prefix},
                     timeout=TIMEOUT, headers={"User-Agent": user_agent(cfg)})
    r.raise_for_status()
    root = ET.fromstring(r.text)
    if (root.findtext("s3:IsTruncated", "", _S3_NS) or "").lower() == "true":
        log.warning("judiciary bulk listing for %s truncated at 1000 keys", dataset)
    keys = [k.text for k in root.findall(".//s3:Key", _S3_NS) if pat.match(k.text or "")]
    if not keys:
        raise RuntimeError(f"no bulk-data snapshot found for {dataset!r}")
    return max(keys)  # ISO dates in the filename sort correctly


def _fetch_csv(cfg: Config, dataset: str) -> list[dict]:
    """Download (if not already cached) and parse one bulk CSV. Cached by its
    own filename, which changes every snapshot -- an old snapshot is never
    re-fetched, the same immutable-by-name caching collect.py uses for PDFs."""
    key = _latest_key(cfg, dataset)
    cache = cfg.cache_dir / key.rsplit("/", 1)[-1]
    if not cache.exists():
        r = requests.get(f"{BULK_BASE}/{key}", timeout=300,
                         headers={"User-Agent": user_agent(cfg)})
        r.raise_for_status()
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(r.content)
    with bz2.open(cache, "rt", newline="", encoding="utf-8", errors="replace") as f:
        return list(csv.DictReader(f))


# ----------------------------------------------------------------- parsing
def parse_disclosures(rows: list[dict]) -> tuple[list[dict], int]:
    """Disclosure index rows -> (clean rows, corrupt rows dropped). See the
    module docstring for why csv.DictReader yields far more rows than there
    are real disclosures. A numeric id is necessary but not sufficient --
    date_created must look like a real timestamp too, or a coincidentally
    numeric garbage fragment silently overwrites a genuine row that happens
    to share its id (verified against Free Law Project's own published count:
    this filter recovers exactly 32,336 rows from the 2026-06-30 snapshot)."""
    out, dropped = [], 0
    this_year = date.today().year
    for r in rows:
        rid = (r.get("id") or "").strip()
        if not _ID.match(rid) or not _TIMESTAMP.match(r.get("date_created") or ""):
            dropped += 1
            continue
        year_raw = (r.get("year") or "").strip()
        year = (int(year_raw) if _YEAR.match(year_raw) and 1978 <= int(year_raw) <= this_year + 1
                else None)
        pid_raw = (r.get("person_id") or "").strip()
        out.append({
            "id": int(rid),
            "person_id": int(pid_raw) if _ID.match(pid_raw) else None,
            "year": year,
            "report_type": (r.get("report_type") or "").strip()[:8],
            "is_amended": 1 if (r.get("is_amended") or "").strip().lower() == "t" else 0,
            "filepath": r.get("filepath") or "",
        })
    return out, dropped


def parse_investments(rows: list[dict], valid_ids: set[int]) -> tuple[list[dict], int]:
    """Investment/transaction rows -> (clean rows, rows dropped as orphans).
    This file is not corrupted the way the index is -- it has no long
    free-text field -- so a row is dropped only if the disclosure it belongs
    to didn't survive parse_disclosures."""
    out, orphaned = [], 0
    for r in rows:
        rid_raw = (r.get("id") or "").strip()
        did_raw = (r.get("financial_disclosure_id") or "").strip()
        if not _ID.match(rid_raw) or not _ID.match(did_raw):
            continue
        did = int(did_raw)
        if did not in valid_ids:
            orphaned += 1
            continue
        tx_date = ""
        raw_date = (r.get("transaction_date") or "").strip()
        if raw_date:
            try:
                tx_date = datetime.strptime(raw_date, "%Y-%m-%d").date().isoformat()
            except ValueError:
                tx_date = ""
        out.append({
            "id": int(rid_raw), "disclosure_id": did,
            "description": (r.get("description") or "").strip()[:200],
            "tx_type": norm_tx_type(r.get("transaction_during_reporting_period", "")),
            "tx_date": tx_date,
            "income_code": (r.get("income_during_reporting_period_code") or "").strip()[:4],
            "value_code": (r.get("gross_value_code") or "").strip()[:4],
            "tx_value_code": (r.get("transaction_value_code") or "").strip()[:4],
            "tx_gain_code": (r.get("transaction_gain_code") or "").strip()[:4],
            "tx_partner": (r.get("transaction_partner") or "").strip()[:120],
        })
    return out, orphaned


def parse_people(rows: list[dict]) -> dict[int, dict]:
    out = {}
    for r in rows:
        pid_raw = (r.get("id") or "").strip()
        if not _ID.match(pid_raw):
            continue
        out[int(pid_raw)] = {
            "name_first": r.get("name_first") or "", "name_middle": r.get("name_middle") or "",
            "name_last": r.get("name_last") or "", "name_suffix": r.get("name_suffix") or "",
            "slug": r.get("slug") or "", "fjc_id": r.get("fjc_id") or "",
        }
    return out


def parse_courts(rows: list[dict]) -> dict[str, str]:
    return {r["id"]: (r.get("full_name") or r.get("short_name") or r["id"])
            for r in rows if r.get("id")}


def parse_judge_courts(rows: list[dict], courts: dict[str, str]) -> dict[int, tuple[str, str]]:
    """person_id -> (court_id, court_name), picking each judge's most recently
    started judicial-type position. people-db-positions holds every career a
    person in CourtListener's identity graph ever had -- law professor, state
    legislator, mayor -- so non-judicial rows are filtered out first."""
    best: dict[int, tuple[str, str, str]] = {}  # person_id -> (start_date, court_id, name)
    for r in rows:
        if (r.get("position_type") or "") not in _JUDICIAL_TYPES:
            continue
        cid = (r.get("court_id") or "").strip()
        pid_raw = (r.get("person_id") or "").strip()
        if not cid or not _ID.match(pid_raw):
            continue
        pid = int(pid_raw)
        start = r.get("date_start") or ""
        cur = best.get(pid)
        if cur is None or start > cur[0]:
            best[pid] = (start, cid, courts.get(cid, cid))
    return {pid: (cid, name) for pid, (start, cid, name) in best.items()}


# ------------------------------------------------------------------- run
def run(cfg: Config = CONFIG, quiet: bool = False) -> dict:
    disclosures, dropped = parse_disclosures(_fetch_csv(cfg, "disclosures"))
    valid_ids = {d["id"] for d in disclosures}
    investments, orphaned = parse_investments(_fetch_csv(cfg, "investments"), valid_ids)
    people = parse_people(_fetch_csv(cfg, "people"))
    courts = parse_courts(_fetch_csv(cfg, "courts"))
    judge_courts = parse_judge_courts(_fetch_csv(cfg, "positions"), courts)

    judge_ids = {d["person_id"] for d in disclosures if d["person_id"]}
    now = db.utcnow()

    with db.connect(cfg.db_path) as conn:
        for pid in judge_ids:
            prof = people.get(pid, {})
            cid, cname = judge_courts.get(pid, ("", ""))
            conn.execute(
                """INSERT INTO judiciary_judges
                   (person_id, name_first, name_middle, name_last, name_suffix,
                    slug, fjc_id, court_id, court_name, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(person_id) DO UPDATE SET
                     name_first=excluded.name_first, name_middle=excluded.name_middle,
                     name_last=excluded.name_last, name_suffix=excluded.name_suffix,
                     slug=excluded.slug, fjc_id=excluded.fjc_id,
                     court_id=excluded.court_id, court_name=excluded.court_name,
                     updated_at=excluded.updated_at""",
                (pid, prof.get("name_first", ""), prof.get("name_middle", ""),
                 prof.get("name_last", ""), prof.get("name_suffix", ""),
                 prof.get("slug", ""), prof.get("fjc_id", ""), cid, cname, now))
        for d in disclosures:
            conn.execute(
                """INSERT INTO judiciary_disclosures
                   (id, person_id, year, report_type, is_amended, filepath, first_seen)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     person_id=excluded.person_id, year=excluded.year,
                     report_type=excluded.report_type, is_amended=excluded.is_amended,
                     filepath=excluded.filepath""",
                (d["id"], d["person_id"], d["year"], d["report_type"],
                 d["is_amended"], d["filepath"], now))
        for iv in investments:
            conn.execute(
                """INSERT INTO judiciary_investments
                   (id, disclosure_id, description, tx_type, tx_date, income_code,
                    value_code, tx_value_code, tx_gain_code, tx_partner, first_seen)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     disclosure_id=excluded.disclosure_id, description=excluded.description,
                     tx_type=excluded.tx_type, tx_date=excluded.tx_date,
                     income_code=excluded.income_code, value_code=excluded.value_code,
                     tx_value_code=excluded.tx_value_code, tx_gain_code=excluded.tx_gain_code,
                     tx_partner=excluded.tx_partner""",
                (iv["id"], iv["disclosure_id"], iv["description"], iv["tx_type"],
                 iv["tx_date"], iv["income_code"], iv["value_code"],
                 iv["tx_value_code"], iv["tx_gain_code"], iv["tx_partner"], now))

    stats = {"judges": len(judge_ids), "disclosures": len(disclosures),
             "investments": len(investments), "dropped_index_rows": dropped,
             "orphaned_investments": orphaned}
    if not quiet:
        print(f"judiciary: {stats['judges']:,} judges, {stats['disclosures']:,} "
              f"disclosures, {stats['investments']:,} investment lines "
              f"({dropped:,} corrupt index rows dropped, {orphaned:,} "
              f"investments orphaned by them)")
    return stats


# --------------------------------------------------------------- reporting
def coverage(cfg: Config = CONFIG) -> dict:
    with db.connect(cfg.db_path) as conn:
        n_judges = conn.execute("SELECT COUNT(*) FROM judiciary_judges").fetchone()[0]
        n_disc = conn.execute("SELECT COUNT(*) FROM judiciary_disclosures").fetchone()[0]
        n_inv = conn.execute("SELECT COUNT(*) FROM judiciary_investments").fetchone()[0]
        ymin, ymax = conn.execute(
            "SELECT MIN(year), MAX(year) FROM judiciary_disclosures WHERE year IS NOT NULL").fetchone()
        by_year = conn.execute(
            """SELECT year, COUNT(DISTINCT person_id) n FROM judiciary_disclosures
               WHERE year IS NOT NULL GROUP BY year ORDER BY year DESC LIMIT 10""").fetchall()
        courts = conn.execute(
            """SELECT court_name, COUNT(*) n FROM judiciary_judges
               WHERE court_name != '' GROUP BY court_name ORDER BY n DESC LIMIT 15""").fetchall()
    by_year = [(r["year"], r["n"]) for r in by_year]
    # The best-covered *recent* year, not simply the last one present: this
    # dataset's freshness collapses hard after 2020 (see to_markdown), so
    # MAX(year) alone would understate real coverage by two orders of
    # magnitude and overstate the gap by picking a nearly-empty final year.
    peak_year, peak_judges = max(by_year, key=lambda yn: yn[1]) if by_year else (None, 0)
    return {"judges": n_judges, "disclosures": n_disc, "investments": n_inv,
            "year_min": ymin, "year_max": ymax, "by_year": by_year,
            "peak_year": peak_year, "peak_judges": peak_judges,
            "bench_size": BENCH_SIZE,
            "courts": [(r["court_name"], r["n"]) for r in courts]}


def to_markdown(d: dict) -> str:
    L = ["# Federal judiciary financial disclosures", "",
         f"{d['judges']:,} judges, {d['disclosures']:,} disclosure reports "
         f"({d['year_min']}-{d['year_max']}), {d['investments']:,} disclosed "
         "investment/holding lines.", ""]
    if d["peak_year"]:
        frac = d["peak_judges"] / d["bench_size"] * 100 if d["bench_size"] else 0
        L.append(f"**{d['peak_judges']:,} distinct judges filed a report for "
                 f"{d['peak_year']}**, its best-covered recent year -- against "
                 f"roughly {d['bench_size']:,} currently authorized Article "
                 "III, bankruptcy and full-time magistrate judgeships combined "
                 f"({frac:.0f}%). That denominator is a floor, not a ceiling: "
                 "senior, recalled and part-time judges also file and count "
                 "toward the judge total above but not toward it, and a filer "
                 "this data couldn't match to a court doesn't appear in the "
                 "table below.")
        L.append("")
    if d["by_year"] and d["year_max"] and d["year_max"] < 2023:
        L.append(f"**This snapshot's coverage collapses after {d['year_max']}.** "
                  "Free Law Project's bulk export was built mainly from an older "
                  "FOIA'd archive; the Courthouse Ethics and Transparency Act "
                  "requires the AO to keep publishing annual reports from 2022 "
                  "onward, but those newer filings are not yet reflected in this "
                  "bulk dataset -- only a handful of 2021-2022 reports appear at "
                  "all, and nothing later. A run against a fresher upstream "
                  "snapshot, once one is published, is the only fix; this "
                  "collector re-checks for one automatically.")
        L.append("")
    if d["by_year"]:
        L += ["## Judges with a filing on record, by year", "",
              "| year | distinct judges |", "|---|--:|"]
        for yr, n in d["by_year"]:
            L.append(f"| {yr} | {n:,} |")
        L.append("")
    L += ["**No ticker is disclosed on these forms.** `description` is the "
          "asset name exactly as filed -- \"Vanguard Total Stock Market Index "
          "Fund\", \"TCF Financial Corp.\" -- and nothing here is priced, "
          "matched to a ticker, or scored the way congressional trades are.", ""]
    if d["courts"]:
        L += ["## Judges by most recent court on file", "",
              "| court | judges |", "|---|--:|"]
        for name, n in d["courts"]:
            L.append(f"| {name} | {n:,} |")
        L.append("")
    return "\n".join(L) + "\n"


def selftest(cfg: Config = CONFIG):
    assert norm_tx_type("Buy (add'l)") == "buy"
    assert norm_tx_type("Sold (part)") == "sell"
    assert norm_tx_type("BUY") == "buy"
    assert norm_tx_type("sell") == "sell"
    assert norm_tx_type("Redeemed") == "sell"
    assert norm_tx_type("Exempt") == "other"
    assert norm_tx_type("") == ""

    # A corrupt index row (garbage id, the addendum-desync artifact the module
    # docstring describes) must be dropped, not stored with a null id.
    disc, dropped = parse_disclosures([
        {"id": "1108", "date_created": "2021-01-04 03:23:26.114299+00", "year": "2009",
         "person_id": "2084", "report_type": "-1", "is_amended": "f",
         "filepath": "us/.../mattice-disclosure.2009.pdf"},
        {"id": "merger of balance of 50 shares of Sprint stock with Nextel",
         "date_created": None, "year": None, "person_id": None, "report_type": None,
         "is_amended": None, "filepath": None},
        {"id": "20670", "date_created": "2021-02-10 00:23:41.274143+00", "year": "9228",
         "person_id": "64", "report_type": "2", "is_amended": "f", "filepath": ""},  # OCR'd garbage year
        # A garbage fragment that happens to look like a real id (see the module
        # docstring): numeric, but no real timestamp behind it -- must not
        # overwrite disclosure 1108 above.
        {"id": "1108", "date_created": "3", "year": "1935", "person_id": "9",
         "report_type": "a corrupted fragment", "is_amended": None, "filepath": None},
    ])
    assert dropped == 2
    assert len(disc) == 2
    assert disc[0]["id"] == 1108 and disc[0]["year"] == 2009 and disc[0]["person_id"] == 2084
    assert disc[1]["id"] == 20670 and disc[1]["year"] is None, \
        "an out-of-range year must not be stored as though it were real"

    valid = {1108}
    inv, orphaned = parse_investments([
        {"id": "4558608", "financial_disclosure_id": "1108",
         "description": "Fidelity Cash Reserves", "income_during_reporting_period_code": "A",
         "gross_value_code": "K", "transaction_during_reporting_period": "",
         "transaction_date": "", "transaction_value_code": "", "transaction_gain_code": "",
         "transaction_partner": ""},
        {"id": "4558609", "financial_disclosure_id": "20670",  # points at a dropped disclosure
         "description": "orphan", "gross_value_code": "L",
         "transaction_during_reporting_period": "", "transaction_date": "",
         "income_during_reporting_period_code": "", "transaction_value_code": "",
         "transaction_gain_code": "", "transaction_partner": ""},
    ], valid)
    assert orphaned == 1
    assert len(inv) == 1 and inv[0]["disclosure_id"] == 1108
    assert inv[0]["value_code"] == "K"
    assert "AAPL" not in str(inv[0]), "no ticker should ever be synthesized"

    courts = parse_courts([{"id": "cadc", "short_name": "D.C. Cir.",
                            "full_name": "United States Court of Appeals for the District of Columbia Circuit"}])
    jc = parse_judge_courts([
        {"position_type": "jud", "court_id": "cadc", "person_id": "64", "date_start": "1986-01-01"},
        {"position_type": "prof", "court_id": "", "person_id": "64", "date_start": "1980-01-01"},
    ], courts)
    assert jc[64][0] == "cadc"
    assert "District of Columbia" in jc[64][1]

    md = to_markdown({"judges": 5, "disclosures": 10, "investments": 100,
                      "year_min": 2020, "year_max": 2022, "by_year": [(2022, 1), (2020, 5)],
                      "peak_year": 2020, "peak_judges": 5, "bench_size": BENCH_SIZE,
                      "courts": []})
    assert "No ticker is disclosed" in md and "5" in md
    assert "collapses after 2022" in md, "a pre-2023 year_max should surface the coverage-gap note"

    # The checks above are pure-function checks against pasted rows -- they'd
    # pass even if `run()` itself were broken. These check the actual database,
    # which only has rows in it if `run(cfg)` has been executed against the
    # real bulk snapshot at least once.
    with db.connect(cfg.db_path) as conn:
        n_judges = conn.execute("SELECT COUNT(*) FROM judiciary_judges").fetchone()[0]
        n_disc = conn.execute("SELECT COUNT(*) FROM judiciary_disclosures").fetchone()[0]
        n_inv = conn.execute("SELECT COUNT(*) FROM judiciary_investments").fetchone()[0]
        orphan_investments = conn.execute(
            """SELECT COUNT(*) FROM judiciary_investments i WHERE NOT EXISTS
               (SELECT 1 FROM judiciary_disclosures d WHERE d.id = i.disclosure_id)"""
        ).fetchone()[0]
        judges_with_no_disclosure = conn.execute(
            """SELECT COUNT(*) FROM judiciary_judges j WHERE NOT EXISTS
               (SELECT 1 FROM judiciary_disclosures d WHERE d.person_id = j.person_id)"""
        ).fetchone()[0]
    if n_disc == 0:
        print("selftest: judiciary_disclosures is empty -- run judiciary.run(cfg) "
              "against the real bulk snapshot first for the stored-data checks; "
              "the parser/vocabulary checks above already passed on their own")
    else:
        assert orphan_investments == 0, \
            f"{orphan_investments} stored investment rows reference a disclosure id " \
            "that was never inserted -- a foreign-key leak from run()"
        assert judges_with_no_disclosure == 0, \
            "run() only inserts a judge for a person_id seen on a disclosure, so " \
            "every stored judge must trace back to at least one"
        # Sanity bounds, not an exact match -- a future quarterly snapshot will
        # add rows, but the 2026-06-30 one recovers exactly 32,336 disclosures
        # and 1,901,720 investments, so a collapse to a small fraction of that
        # would mean the recovery filter broke, not that the source shrank.
        assert 25_000 <= n_disc <= 200_000, f"disclosure count {n_disc:,} looks wrong"
        assert n_inv >= 500_000, f"investment count {n_inv:,} looks truncated"
        assert n_judges >= 1_000, f"judge count {n_judges:,} looks truncated"
        print(f"selftest (stored data): {n_judges:,} judges, {n_disc:,} disclosures, "
              f"{n_inv:,} investments -- 0 orphaned investments, 0 judges without "
              "a disclosure")

    print(f"selftest ok: {len(INCOME_CODES)} income codes, {len(VALUE_CODES)} "
          f"value codes, {len(_JUDICIAL_TYPES)} judicial position types, "
          f"bench denominator {BENCH_SIZE:,}")


if __name__ == "__main__":
    selftest()
