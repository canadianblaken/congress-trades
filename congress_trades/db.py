"""SQLite storage: the disclosures themselves, who the filers are, their committee
seats, an industry label per ticker, forward price returns, the alert ledger,
committee meeting dates, and the two tables a model writes to. Later
additions (FEC campaign finance, lobbying, annual reports, judiciary) are
appended below, each documented where it's declared.

Model output is kept apart from parsed fact, in its own tables, with the model
that produced each row. Nothing here merges the two: a reader can always drop
asset_labels and meeting_topics and be back to what the filings actually said.
The one exception is a recovered ticker, which is written into congress_trades so
the rest of the pipeline can price it -- and that only happens after the symbol
has been checked against SEC registration and a real price series, so what lands
in the trades table is a verified fact rather than a suggestion."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS congress_trades (
    id           INTEGER PRIMARY KEY,
    chamber      TEXT NOT NULL,        -- House | Senate
    member       TEXT NOT NULL,        -- the name exactly as filed
    state        TEXT,
    owner        TEXT,                 -- Self | Spouse | Joint | Dependent
    ticker       TEXT,                 -- '' for untickered assets (notes, funds)
    asset_name   TEXT,
    tx_type      TEXT,                 -- buy | sell
    tx_date      TEXT,                 -- ISO
    disclosed    TEXT,                 -- ISO
    amount_min   INTEGER,              -- lower bound of the disclosed bracket
    amount_range TEXT,
    doc_id       TEXT NOT NULL,
    doc_url      TEXT,
    row_idx      INTEGER NOT NULL,     -- position within the filing
    first_seen   TEXT NOT NULL,
    UNIQUE(doc_id, row_idx)
);
CREATE INDEX IF NOT EXISTS idx_ct_member ON congress_trades(member);
CREATE INDEX IF NOT EXISTS idx_ct_ticker ON congress_trades(ticker);
CREATE INDEX IF NOT EXISTS idx_ct_date   ON congress_trades(tx_date);

CREATE TABLE IF NOT EXISTS congress_members (
    member       TEXT PRIMARY KEY,     -- the name as it appears on filings
    bioguide     TEXT,
    full_name    TEXT,                 -- the name the person actually goes by
    chamber      TEXT,
    state        TEXT,
    district     TEXT,
    party        TEXT,
    official_url TEXT,
    birthday     TEXT,
    current      INTEGER DEFAULT 1,
    wiki_title   TEXT,
    wiki_desc    TEXT,
    wiki_extract TEXT,
    wiki_thumb   TEXT,
    wiki_url     TEXT,
    party_unity  REAL,                 -- % of yea/nay votes with own party majority
    votes_cast   INTEGER,
    nominate     REAL,                 -- DW-NOMINATE dim 1
    votes_congress INTEGER,
    updated_at   TEXT
);

CREATE TABLE IF NOT EXISTS member_committees (
    bioguide   TEXT NOT NULL,
    key        TEXT NOT NULL,
    name       TEXT,
    parent     TEXT,                   -- '' for a full committee
    title      TEXT,                   -- Chairman / Ranking Member / ''
    PRIMARY KEY (bioguide, key)
);
CREATE INDEX IF NOT EXISTS idx_mc_bio ON member_committees(bioguide);

CREATE TABLE IF NOT EXISTS trade_returns (
    trade_id   INTEGER PRIMARY KEY,    -- congress_trades.id
    px_0       REAL,                   -- close on/after the disclosure date
    px_30      REAL,
    px_90      REAL,
    px_now     REAL,
    ret_30     REAL,                   -- raw price change, sign NOT flipped for sells
    ret_90     REAL,
    ret_now    REAL,
    bench_30   REAL,                   -- SPY over the identical window
    bench_90   REAL,
    bench_now  REAL,
    sec_30     REAL,                   -- the trade's own sector ETF, same window
    sec_90     REAL,
    sec_now    REAL,
    sec_etf    TEXT,                   -- which ETF stood in for the sector
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS alerts_seen (
    fingerprint TEXT PRIMARY KEY,      -- one row per alert already emitted
    first_seen  TEXT
);

CREATE TABLE IF NOT EXISTS committee_meetings (
    event_id     TEXT PRIMARY KEY,
    congress     INTEGER,
    chamber      TEXT,
    meeting_date TEXT,                 -- ISO date
    type         TEXT,                 -- Hearing | Markup | Business Meeting
    title        TEXT,
    roots        TEXT                  -- comma separated 4-char committee roots
);
CREATE INDEX IF NOT EXISTS idx_cm_date ON committee_meetings(meeting_date);

CREATE TABLE IF NOT EXISTS ticker_sectors (
    ticker     TEXT PRIMARY KEY,
    cik        TEXT,
    company    TEXT,
    sic        TEXT,
    sic_desc   TEXT,
    sector     TEXT,
    updated_at TEXT
);

-- One LD-2 quarterly report per row. filing_uuid is the LDA's own primary key and
-- is immutable once posted (an amendment is a NEW filing_uuid, not an edit of this
-- one), so re-ingestion is a pure overwrite, not a merge.
CREATE TABLE IF NOT EXISTS lobbying_filings (
    filing_uuid    TEXT PRIMARY KEY,
    filing_type    TEXT,              -- Q1..Q4 report/amendment/termination code
    filing_type_display TEXT,
    filing_period  TEXT,              -- first_quarter .. fourth_quarter
    filing_year    INTEGER,
    client_name    TEXT,
    client_id      INTEGER,
    client_state   TEXT,
    client_desc    TEXT,              -- the client's own one-line business description
    registrant_id  INTEGER,
    registrant_name TEXT,             -- the lobbying firm/entity that filed
    income         REAL,              -- WHOLE-FILING total; not attributable to one issue
    expenses       REAL,
    dt_posted      TEXT,
    doc_url        TEXT,
    first_seen     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lf_period ON lobbying_filings(filing_year, filing_period);
CREATE INDEX IF NOT EXISTS idx_lf_client ON lobbying_filings(client_name);

-- One row per issue area within a filing (a filing lists several). This is where
-- the government_entities (chambers/agencies lobbied) and the fixed issue-code
-- vocabulary live. NEVER stores a specific member -- LD-2 filings don't name one.
CREATE TABLE IF NOT EXISTS lobbying_activities (
    filing_uuid  TEXT NOT NULL,
    activity_idx INTEGER NOT NULL,    -- position within the filing's activity list
    issue_code   TEXT,                -- fixed LDA vocabulary, e.g. TAX, HCR, DEF
    issue_desc   TEXT,
    description  TEXT,                -- free text; may name a bill, never a member
    chambers     TEXT,                -- comma-separated subset of House/Senate lobbied
    agencies     TEXT,                -- comma-separated other government entities lobbied
    PRIMARY KEY (filing_uuid, activity_idx)
);
CREATE INDEX IF NOT EXISTS idx_la_issue ON lobbying_activities(issue_code);

-- FEC campaign-finance cross-reference (bulk downloads, no API key -- see
-- congress_trades/finance.py). cand_id is FEC's own id, not bioguide; bioguide
-- is filled in only where finance.match_candidate found an unambiguous match,
-- and match_method records why every unmatched row was left unmatched.
CREATE TABLE IF NOT EXISTS fec_candidates (
    cand_id      TEXT NOT NULL,
    cycle        TEXT NOT NULL,       -- FEC 2-year cycle, e.g. '2026'
    cand_name    TEXT,                -- 'LAST, FIRST MIDDLE' as FEC files it
    office       TEXT,                -- H | S
    state        TEXT,
    district     TEXT,
    party        TEXT,
    ttl_receipts REAL,                -- weball TTL_RECEIPTS
    pac_receipts REAL,                -- weball OTHER_POL_CMTE_CONTRIB: PAC money only
    bioguide     TEXT,                -- '' when unmatched
    match_method TEXT,                -- 'matched' | the reason it was not
    PRIMARY KEY (cand_id, cycle)
);
CREATE INDEX IF NOT EXISTS idx_fc_bioguide ON fec_candidates(bioguide);

CREATE TABLE IF NOT EXISTS fec_committees (
    cmte_id       TEXT PRIMARY KEY,
    name          TEXT,
    connected_org TEXT,
    cmte_type     TEXT,
    sector_guess  TEXT                -- '' when no keyword hit -- see PAC_SECTOR_KEYWORDS
);

CREATE TABLE IF NOT EXISTS fec_pac_contributions (
    cycle    TEXT NOT NULL,
    cmte_id  TEXT NOT NULL,
    cand_id  TEXT NOT NULL,
    amount   REAL,                    -- sum of Schedule B 24K contributions, the cycle
    n_tx     INTEGER,
    first_dt TEXT,
    last_dt  TEXT,
    PRIMARY KEY (cycle, cmte_id, cand_id)
);
CREATE INDEX IF NOT EXISTS idx_fpc_cand ON fec_pac_contributions(cand_id, cycle);

-- Annual Financial Disclosure Reports (see annual.py). A separate filing
-- stream from congress_trades: FilingType O/A/C/T/H rather than P, and keyed
-- by doc_id alone since one FDR carries many schedules' worth of rows, each
-- keyed (doc_id, row_idx) against it.
CREATE TABLE IF NOT EXISTS annual_filings (
    doc_id       TEXT PRIMARY KEY,
    member       TEXT NOT NULL,
    state        TEXT,
    filing_type  TEXT NOT NULL,      -- O | A | C | T | H
    year         TEXT,
    filed        TEXT,               -- ISO date
    doc_url      TEXT,
    first_seen   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_af_member ON annual_filings(member);

CREATE TABLE IF NOT EXISTS annual_liabilities (
    doc_id         TEXT NOT NULL,
    row_idx        INTEGER NOT NULL,
    owner          TEXT,             -- Self | Spouse | Joint | Dependent
    creditor       TEXT,
    date_incurred  TEXT,             -- as disclosed -- the form has no fixed date format
    liability_type TEXT,
    amount_min     INTEGER,          -- lower bound of the disclosed bracket
    amount_range   TEXT,
    UNIQUE(doc_id, row_idx)
);

CREATE TABLE IF NOT EXISTS annual_earned_income (
    doc_id      TEXT NOT NULL,
    row_idx     INTEGER NOT NULL,
    source      TEXT,
    income_type TEXT,
    amount      TEXT,                -- as disclosed: an exact dollar figure, or "N/A"
    amount_val  REAL,                -- parsed numeric; NULL for "N/A" rows
    UNIQUE(doc_id, row_idx)
);

CREATE TABLE IF NOT EXISTS annual_assets (
    doc_id       TEXT NOT NULL,
    row_idx      INTEGER NOT NULL,
    asset_name   TEXT,
    owner        TEXT,
    value_min    INTEGER,            -- NULL for "Undetermined" (e.g. defined-benefit pensions)
    value_range  TEXT,
    income_type  TEXT,               -- Dividends | Interest | Rent | Tax-Deferred | None | ...
    income_min   INTEGER,
    income_range TEXT,
    UNIQUE(doc_id, row_idx)
);

CREATE TABLE IF NOT EXISTS annual_positions (
    doc_id       TEXT NOT NULL,
    row_idx      INTEGER NOT NULL,
    position     TEXT,
    organization TEXT,
    UNIQUE(doc_id, row_idx)
);

-- Federal judiciary financial disclosures (Courthouse Ethics and Transparency Act,
-- 2022), sourced from Free Law Project's public-domain bulk CSV snapshots -- see
-- congress_trades/judiciary.py for why (the AO's own database gates every visit
-- behind identity registration + reCAPTCHA, which this project will not automate).
-- IDs are CourtListener's own, kept as the primary key so a re-ingested quarterly
-- snapshot updates rows in place rather than duplicating them.
CREATE TABLE IF NOT EXISTS judiciary_judges (
    person_id    INTEGER PRIMARY KEY,   -- CourtListener person id
    name_first   TEXT,
    name_middle  TEXT,
    name_last    TEXT,
    name_suffix  TEXT,
    slug         TEXT,
    fjc_id       TEXT,                  -- Federal Judicial Center id, when known
    court_id     TEXT,                  -- CourtListener court slug of the most recent judicial seat found
    court_name   TEXT,                  -- resolved from the courts snapshot at ingest time
    updated_at   TEXT
);

CREATE TABLE IF NOT EXISTS judiciary_disclosures (
    id           INTEGER PRIMARY KEY,   -- CourtListener financial_disclosure id
    person_id    INTEGER,
    year         INTEGER,               -- reporting year; NULL if the source row was unrecoverable
    report_type  TEXT,
    is_amended   INTEGER,
    filepath     TEXT,                  -- the underlying PDF, on CourtListener's storage
    first_seen   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jd_person ON judiciary_disclosures(person_id);
CREATE INDEX IF NOT EXISTS idx_jd_year   ON judiciary_disclosures(year);

CREATE TABLE IF NOT EXISTS judiciary_investments (
    id             INTEGER PRIMARY KEY,   -- CourtListener investment id
    disclosure_id  INTEGER NOT NULL,
    description    TEXT,                 -- asset name as filed -- no ticker; see judiciary.py
    tx_type        TEXT,                 -- buy | sell | other | '' (normalized from free text)
    tx_date        TEXT,                 -- ISO, '' if absent or unparseable
    income_code    TEXT,                 -- AO-10 Income Gain Code (A-H2)
    value_code     TEXT,                 -- AO-10 Category-of-Value code (J-P4), holding's own value
    tx_value_code  TEXT,                 -- same code table, for the transaction amount
    tx_gain_code   TEXT,
    tx_partner     TEXT,
    first_seen     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ji_disclosure ON judiciary_investments(disclosure_id);

-- What a model made of the asset names the parser could not resolve. Keyed on
-- the filing's text verbatim, because that is what repeats across filings.
CREATE TABLE IF NOT EXISTS asset_labels (
    asset_name TEXT PRIMARY KEY,
    label      TEXT NOT NULL,          -- one of assets.VOCAB, never free text
    proposed   TEXT NOT NULL DEFAULT '',  -- the symbol the model offered, before any check
    ticker     TEXT NOT NULL DEFAULT '',  -- '' unless independently verified
    issuer     TEXT,                   -- the entity behind the instrument
    confidence TEXT,                   -- high | medium | low, the model's own
    verdict    TEXT,                   -- how a proposed ticker was checked, or why it was refused
    model      TEXT NOT NULL,
    updated_at TEXT
);

-- What a parser audit made of one cached filing text. Not part of any pipeline:
-- a regression net over filings already collected, so a template change that
-- starts dropping rows is caught by a periodic run rather than by noticing the
-- totals look wrong a year later. Dropping this table loses nothing but history.
CREATE TABLE IF NOT EXISTS parser_audits (
    doc_id     TEXT PRIMARY KEY,
    n_parsed   INTEGER,             -- rows the parser returned for this text
    n_pattern  INTEGER,             -- transaction headers the pattern finds in it
    n_claimed  INTEGER,             -- rows a model said were missing
    n_verified INTEGER,             -- ...of those, how many the text corroborates
    detail     TEXT,                -- JSON: the verified misses, with their evidence
    model      TEXT NOT NULL,       -- '' when only the deterministic scan ran
    updated_at TEXT
);

-- What each committee meeting was actually about. Titles come from Congress.gov;
-- the sector tags are a model's reading of the title.
CREATE TABLE IF NOT EXISTS meeting_topics (
    event_id   TEXT PRIMARY KEY,
    sectors    TEXT NOT NULL,          -- comma separated, from the sectors.py vocabulary
    subject    TEXT,                   -- the title reduced to a plain subject line
    confidence TEXT,
    model      TEXT NOT NULL,
    updated_at TEXT
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# Columns added to a table after it shipped. CREATE TABLE IF NOT EXISTS does
# nothing to an existing table, so a database made by an earlier version would
# otherwise be missing them with no sign but a query error.
_ADDED_COLUMNS = (
    ("asset_labels", "proposed", "TEXT NOT NULL DEFAULT ''"),
)


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, decl in _ADDED_COLUMNS:
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if have and column not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


@contextmanager
def connect(db_path: Path):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------- trades
def upsert_trades(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """Insert transactions, ignoring ones already stored.

    Rows are keyed (doc_id, row_idx). NOTE: row_idx is the position within the
    filing as parsed, so it is only stable if every run parses the same rows --
    which is why collection stores every bracket and filtering happens at display
    time. Changing what gets stored means rebuilding the table, not merging.
    """
    now = utcnow()
    per_doc: dict[str, int] = {}
    new = 0
    for r in rows:
        idx = per_doc.get(r["doc_id"], 0)
        per_doc[r["doc_id"]] = idx + 1
        idx = r.get("row_idx", idx)       # set by backfill before supersede() drops any
        cur = conn.execute(
            """INSERT OR IGNORE INTO congress_trades
               (chamber, member, state, owner, ticker, asset_name, tx_type, tx_date,
                disclosed, amount_min, amount_range, doc_id, doc_url, row_idx, first_seen)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (r.get("chamber"), r.get("member"), r.get("state"), r.get("owner"),
             r.get("ticker", ""), r.get("asset_name"), r.get("tx_type"), r.get("tx_date"),
             r.get("disclosed"), r.get("amount_min", 0), r.get("amount_range"),
             r["doc_id"], r.get("doc_url"), idx, now))
        new += cur.rowcount
    return new


def reconcile_trades(conn: sqlite3.Connection, kept: list[dict],
                     dropped: list[dict]) -> tuple[int, int]:
    """(removed, re-dated). Apply collect.supersede() to rows already stored.

    INSERT OR IGNORE never revisits a stored row, so a superseded version stays
    unless removed here, and a kept row whose first disclosure was found in an
    earlier version keeps its later date unless updated here. Either way its
    forward returns were measured from the wrong row or date, so they go too and
    `prices` recomputes them.
    """
    removed = redated = 0
    for r in dropped:
        for (i,) in conn.execute(
                "SELECT id FROM congress_trades WHERE doc_id = ? AND row_idx = ?",
                (r["doc_id"], r["row_idx"])).fetchall():
            conn.execute("DELETE FROM trade_returns WHERE trade_id = ?", (i,))
            conn.execute("DELETE FROM congress_trades WHERE id = ?", (i,))
            removed += 1
    for r in kept:
        for (i,) in conn.execute(
                "SELECT id FROM congress_trades WHERE doc_id = ? AND row_idx = ? "
                "AND disclosed > ?",
                (r["doc_id"], r["row_idx"], r.get("disclosed") or "")).fetchall():
            conn.execute("UPDATE congress_trades SET disclosed = ? WHERE id = ?",
                         (r["disclosed"], i))
            conn.execute("DELETE FROM trade_returns WHERE trade_id = ?", (i,))
            redated += 1
    return removed, redated


def ticker_names(conn: sqlite3.Connection) -> dict[str, str]:
    """ticker -> a readable name: SEC's registered name where the sectors pass found
    one, else the asset description filed most often under that ticker."""
    out: dict[str, str] = {}
    for t, name, _n in conn.execute(
            """SELECT ticker, asset_name, COUNT(*) n FROM congress_trades
                WHERE ticker != '' AND asset_name != ''
                GROUP BY ticker, asset_name ORDER BY n"""):
        out[t] = name                         # ascending, so the commonest wins
    for t, company in conn.execute(
            "SELECT ticker, company FROM ticker_sectors WHERE company != ''"):
        if t in out:
            out[t] = company
    return out


def all_trades(conn: sqlite3.Connection, since: str = "") -> list[sqlite3.Row]:
    q = "SELECT * FROM congress_trades"
    args: list = []
    if since:
        q += " WHERE tx_date >= ?"
        args.append(since)
    return conn.execute(q + " ORDER BY tx_date DESC", args).fetchall()


# ---------------------------------------------------------------- members
_MEMBER_COLS = ("bioguide", "full_name", "chamber", "state", "district", "party",
                "official_url", "birthday", "current", "wiki_title", "wiki_desc",
                "wiki_extract", "wiki_thumb", "wiki_url")


def upsert_member(conn: sqlite3.Connection, member: str, prof: dict) -> None:
    conn.execute(
        f"""INSERT INTO congress_members (member, {', '.join(_MEMBER_COLS)}, updated_at)
            VALUES ({', '.join('?' * (len(_MEMBER_COLS) + 2))})
            ON CONFLICT(member) DO UPDATE SET
              {', '.join(f'{c}=excluded.{c}' for c in _MEMBER_COLS)},
              updated_at=excluded.updated_at""",
        (member, *[prof.get(c) for c in _MEMBER_COLS], utcnow()))


def all_members(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM congress_members").fetchall()


def stale_members(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    """Least-recently-refreshed first; never-seen members lead."""
    return conn.execute(
        """SELECT t.member, t.chamber, t.state
           FROM (SELECT DISTINCT member, chamber, state FROM congress_trades) t
           LEFT JOIN congress_members m ON m.member = t.member
           ORDER BY m.updated_at IS NOT NULL, m.updated_at ASC
           LIMIT ?""", (limit,)).fetchall()


def distinct_filers(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT DISTINCT member, chamber, state FROM congress_trades ORDER BY member"
    ).fetchall()


# ---------------------------------------------------------------- committees
def replace_committees(conn: sqlite3.Connection, bioguide: str, rows: list[dict]) -> int:
    """Replace rather than merge: a stale seat is worse than none."""
    conn.execute("DELETE FROM member_committees WHERE bioguide=?", (bioguide,))
    conn.executemany(
        "INSERT OR IGNORE INTO member_committees (bioguide, key, name, parent, title) "
        "VALUES (?,?,?,?,?)",
        [(bioguide, r["key"], r["name"], r.get("parent", ""), r.get("title", ""))
         for r in rows])
    return len(rows)


def committees_by_member(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in conn.execute(
            "SELECT * FROM member_committees ORDER BY parent='' DESC, name"):
        out.setdefault(r["bioguide"], []).append(dict(r))
    return out


# ---------------------------------------------------------------- sectors
def upsert_sector(conn: sqlite3.Connection, rec: dict) -> None:
    conn.execute(
        """INSERT INTO ticker_sectors (ticker, cik, company, sic, sic_desc, sector, updated_at)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(ticker) DO UPDATE SET
             cik=excluded.cik, company=excluded.company, sic=excluded.sic,
             sic_desc=excluded.sic_desc, sector=excluded.sector,
             updated_at=excluded.updated_at""",
        (rec["ticker"], rec.get("cik"), rec.get("company"), rec.get("sic"),
         rec.get("sic_desc"), rec.get("sector"), utcnow()))


def all_sectors(conn: sqlite3.Connection) -> dict[str, dict]:
    return {r["ticker"]: dict(r) for r in conn.execute("SELECT * FROM ticker_sectors")}


def member_medians(conn: sqlite3.Connection, floor: int) -> dict[str, float]:
    """Each member's own median disclosed amount, over their whole history and
    above `floor`. One definition, so 'unusual for them' means the same thing
    everywhere it's quoted -- digest's lone-large multiplier and alerts' bar
    used to compute this two different ways and disagreed by an order of
    magnitude on the same trade."""
    by: dict[str, list[float]] = {}
    for r in conn.execute(
            "SELECT member, amount_min FROM congress_trades "
            "WHERE ticker != '' AND amount_min >= ?", (floor,)):
        by.setdefault(r["member"], []).append(r["amount_min"])
    import statistics
    return {m: statistics.median(v) for m, v in by.items() if v}

# ---------------------------------------------------------- annual FDRs
def upsert_annual_filings(conn: sqlite3.Connection, rows: list[dict]) -> int:
    now = utcnow()
    new = 0
    for r in rows:
        cur = conn.execute(
            """INSERT OR IGNORE INTO annual_filings
               (doc_id, member, state, filing_type, year, filed, doc_url, first_seen)
               VALUES(?,?,?,?,?,?,?,?)""",
            (r["doc_id"], r.get("member"), r.get("state"), r.get("filing_type"),
             r.get("year"), r.get("filed").isoformat() if hasattr(r.get("filed"), "isoformat")
             else r.get("filed"), r.get("doc_url"), now))
        new += cur.rowcount
    return new


# Column lists per schedule table -- upsert_annual_rows uses these to build
# one parameterized INSERT rather than repeating near-identical SQL four times.
_ANNUAL_COLS = {
    "annual_liabilities": ("owner", "creditor", "date_incurred", "liability_type",
                          "amount_min", "amount_range"),
    "annual_earned_income": ("source", "income_type", "amount", "amount_val"),
    "annual_assets": ("asset_name", "owner", "value_min", "value_range",
                      "income_type", "income_min", "income_range"),
    "annual_positions": ("position", "organization"),
}


def upsert_annual_rows(conn: sqlite3.Connection, table: str, rows: list[dict]) -> int:
    """Insert schedule rows, keyed (doc_id, row_idx) like congress_trades --
    row_idx is the row's position within its schedule as parsed, so a parser
    change means rebuilding the table, not merging into it."""
    cols = _ANNUAL_COLS[table]
    per_doc: dict[str, int] = {}
    new = 0
    for r in rows:
        idx = per_doc.get(r["doc_id"], 0)
        per_doc[r["doc_id"]] = idx + 1
        cur = conn.execute(
            f"""INSERT OR IGNORE INTO {table} (doc_id, row_idx, {', '.join(cols)})
                VALUES(?,?,{', '.join('?' * len(cols))})""",
            (r["doc_id"], idx, *[r.get(c) for c in cols]))
        new += cur.rowcount
    return new


def untagged_tickers(conn: sqlite3.Connection, limit: int = 0) -> list[str]:
    q = """SELECT t.ticker FROM congress_trades t
           LEFT JOIN ticker_sectors s ON s.ticker = t.ticker
           WHERE t.ticker != '' AND s.ticker IS NULL
           GROUP BY t.ticker ORDER BY COUNT(*) DESC"""
    if limit:
        q += f" LIMIT {int(limit)}"
    return [r[0] for r in conn.execute(q).fetchall()]


# ---------------------------------------------------------------- lobbying (LDA)
def upsert_lobbying_filing(conn: sqlite3.Connection, filing: dict) -> None:
    """Overwrite rather than merge, like `replace_committees` -- filings are
    immutable once posted, so a re-fetch always agrees with what's stored."""
    conn.execute(
        """INSERT INTO lobbying_filings
             (filing_uuid, filing_type, filing_type_display, filing_period, filing_year,
              client_name, client_id, client_state, client_desc, registrant_id,
              registrant_name, income, expenses, dt_posted, doc_url, first_seen)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(filing_uuid) DO UPDATE SET
             filing_type=excluded.filing_type, filing_type_display=excluded.filing_type_display,
             filing_period=excluded.filing_period, filing_year=excluded.filing_year,
             client_name=excluded.client_name, client_id=excluded.client_id,
             client_state=excluded.client_state, client_desc=excluded.client_desc,
             registrant_id=excluded.registrant_id, registrant_name=excluded.registrant_name,
             income=excluded.income, expenses=excluded.expenses,
             dt_posted=excluded.dt_posted, doc_url=excluded.doc_url""",
        (filing["filing_uuid"], filing["filing_type"], filing["filing_type_display"],
         filing["filing_period"], filing["filing_year"], filing["client_name"],
         filing["client_id"], filing["client_state"], filing["client_desc"],
         filing["registrant_id"], filing["registrant_name"], filing["income"],
         filing["expenses"], filing["dt_posted"], filing["doc_url"], utcnow()))


def replace_lobbying_activities(conn: sqlite3.Connection, filing_uuid: str,
                                 activities: list[dict]) -> None:
    conn.execute("DELETE FROM lobbying_activities WHERE filing_uuid=?", (filing_uuid,))
    conn.executemany(
        """INSERT INTO lobbying_activities
             (filing_uuid, activity_idx, issue_code, issue_desc, description,
              chambers, agencies)
           VALUES (?,?,?,?,?,?,?)""",
        [(a["filing_uuid"], a["activity_idx"], a["issue_code"], a["issue_desc"],
          a["description"], a["chambers"], a["agencies"]) for a in activities])


def lobbying_filing_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT count(*) FROM lobbying_filings").fetchone()[0]
