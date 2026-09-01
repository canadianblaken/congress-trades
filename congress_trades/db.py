"""SQLite storage. Four tables: the disclosures themselves, who the filers are,
their committee seats, and an industry label per ticker."""
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

CREATE TABLE IF NOT EXISTS ticker_sectors (
    ticker     TEXT PRIMARY KEY,
    cik        TEXT,
    company    TEXT,
    sic        TEXT,
    sic_desc   TEXT,
    sector     TEXT,
    updated_at TEXT
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect(db_path: Path):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
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


def untagged_tickers(conn: sqlite3.Connection, limit: int = 0) -> list[str]:
    q = """SELECT t.ticker FROM congress_trades t
           LEFT JOIN ticker_sectors s ON s.ticker = t.ticker
           WHERE t.ticker != '' AND s.ticker IS NULL
           GROUP BY t.ticker ORDER BY COUNT(*) DESC"""
    if limit:
        q += f" LIMIT {int(limit)}"
    return [r[0] for r in conn.execute(q).fetchall()]
