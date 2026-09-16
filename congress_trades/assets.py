"""What the untickered disclosures actually are, and recovering the ones that
were only untickered by accident.

Two different problems live in the same pile. Most of it was a parser gap: the
House Clerk's newer template writes "Apple Inc. - Common Stock (AAPL)" with no
asset-type code after the ticker, and the original pattern required one, so
thousands of ordinary equity trades were stored with an empty ticker and then
filtered out of every analysis. pdftotext also renders some capitals in lower
case, so "(bLK)" and "(CAg)" failed a case-sensitive match.

What remains after recovery is genuinely untickered and cannot be priced --
Treasury bills, money-market funds, structured notes, partnership units. Those
are worth counting rather than discarding: a member whose disclosures are mostly
Treasuries is parking money, not picking stocks, and no alpha figure conveys
that.
"""
from __future__ import annotations

import re

from . import collect, db, prices
from .config import CONFIG

# The House template tags each holding with its own asset-type code. That
# taxonomy beats any keyword guess: municipal debt in these filings is written
# "Los Angeles CA GO UTX [GS]", which no amount of matching on "general
# obligation" will catch, but the [GS] says it outright. Only about a quarter of
# rows carry a code, so the name patterns below remain the fallback.
TYPE_CODE = re.compile(r"\[([A-Z]{2})\]")
CODE_CLASS = {
    "ST": "Listed equity",
    "ET": "Funds & trusts",
    "MF": "Funds & trusts",
    "GS": "Government & municipal debt",
    "CS": "Corporate debt",
    "OP": "Options & derivatives",
    "CT": "Crypto",
    "PS": "Private & pre-IPO equity",
    "HN": "Hedge funds & non-public",
    "AB": "Private & pre-IPO equity",
    "OI": "Private & pre-IPO equity",
    "OL": "Private & pre-IPO equity",
    "SA": "Listed equity",
}

# Codes that name one thing, and are therefore authoritative. [GS] settles a
# municipal bond written as "GO UTX"; [HN] settles a partnership that is really a
# hedge fund.
PRECISE_CODES = {"ST", "ET", "MF", "GS", "OP", "CT", "PS", "HN", "AB", "OI",
                 "OL", "SA"}
# Codes that cover several things, so a name pattern is better evidence. [CS]
# holds both "Goldman Sachs MTN 4/29/2030" and "BLF FedFund TDDXX", which are
# corporate debt and a money-market fund respectively; [OT] means "other".
BROAD_CODES = {"CS": "Corporate debt", "OT": ""}

# Ordered: the first pattern to match wins, so the specific precede the general.
CLASSES = (
    # "UNITED STATES TREAS BILLS" abbreviates to five letters, so match the stem.
    ("Treasuries", re.compile(
        r"\b(treas\w*|t-?bill|t-?note|t-?bond|"
        r"u\.?\s?s\.?\s+(bill|note|bond))", re.I)),
    ("Cash & money market", re.compile(
        r"\b(money\s?market|fedfund|govt?\s?fund|sweep|cash\s?reserve|"
        r"deposit|cd\b|certificate of deposit|tddxx|savings)", re.I)),
    ("Structured notes", re.compile(
        r"\b(structured\s?note|linked\s?note|market\s?linked|autocall\w*|"
        r"buffered?\b.*note)", re.I)),
    # Municipal debt is written in the filings' own abbreviations: GO (general
    # obligation), Rev, Oblig, Cnty, Sch Dist, TRAN, Auth, Var.
    ("Government & municipal debt", re.compile(
        r"\b(muni\w*|general obligation|revenue bond|school district|"
        r"fannie\s?mae|freddie\s?mac|ginnie\s?mae|agency bond|"
        r"go\s+(utx|ltd|unltd)|\bgo\b.*\b(bds?|bond)|sales tax rev|"
        r"transitional fin|sch\s?dist|cnty|county of|city of|"
        r"\brev\b.*\b(bds?|oblig|auth)|\boblig\b|\btrans?\b\s|"
        r"(pwr|util|hlth|arpt|port|wtr|swr)\s?auth|indl dev)", re.I)),
    ("Corporate debt", re.compile(r"\b(bond|debenture|note[s]?\b|senior notes)", re.I)),
    ("Partnerships & private", re.compile(
        r"\b(l\.?\s?p\.?\b|limited partnership|l\.?\s?l\.?\s?c\.?\b|"
        r"partners(hip)?\b|private (equity|placement)|holdings? llc)", re.I)),
    ("Funds & trusts", re.compile(
        r"\b(fund|etf|index|portfolio|trust|reit|annuit\w+|"
        r"401\(?k\)?|ira\b|pension)", re.I)),
    ("Real estate", re.compile(r"\b(real estate|rental|farmland|acres?|residence)", re.I)),
    ("Options & derivatives", re.compile(
        r"\b(call|put|option|warrant|futures?)\b", re.I)),
    ("Crypto", re.compile(r"\b(bitcoin|ethereum|crypto\w*|coinbase wallet)", re.I)),
)


def classify(name: str) -> str:
    """One label per untickered asset.

    The filing's own asset-type code wins where present; keyword patterns are the
    fallback for the three quarters of rows that carry no code. "Other" stays
    honest rather than becoming a dumping ground -- it is mostly company names
    whose ticker is simply absent from the filing.
    """
    n = name or ""
    codes = TYPE_CODE.findall(n)
    code = codes[-1] if codes else ""
    if code in PRECISE_CODES:
        label = CODE_CLASS.get(code, "")
        if label:
            return label
    for label, pat in CLASSES:
        if pat.search(n):
            return label
    if code in BROAD_CODES and BROAD_CODES[code]:
        return BROAD_CODES[code]
    return "Other / unlabelled"


def known_tickers(conn) -> set[str]:
    """Tickers we have independent evidence for: classified by EDGAR, or already
    parsed cleanly from some other filing."""
    out = {r["ticker"] for r in conn.execute("SELECT ticker FROM ticker_sectors")}
    out |= {r["ticker"] for r in
            conn.execute("SELECT DISTINCT ticker FROM congress_trades "
                         "WHERE ticker != ''")}
    return {t for t in out if t}


def repair_tickers(cfg=CONFIG, dry_run: bool = False, quiet: bool = False) -> int:
    """Fill in tickers an older parser dropped, using the parser's own function.

    Only a candidate that matches a ticker we already have evidence for is
    accepted: a wrong ticker on a real disclosure is worse than no ticker, since
    it would be priced and scored as though it were that company.

    Rows are updated in place rather than reparsed and reinserted, because
    trade_returns references congress_trades.id -- renumbering would orphan
    every priced row.
    """
    with db.connect(cfg.db_path) as conn:
        known = known_tickers(conn)
        rows = [dict(r) for r in conn.execute(
            "SELECT id, asset_name FROM congress_trades WHERE ticker = ''")]
        fixes = []
        checked: dict[str, bool] = {}
        probed = 0
        for r in rows:
            cand = collect.ticker_from_name(r["asset_name"])
            if not cand:
                continue
            if cand in known:
                fixes.append((cand, r["id"]))
                continue
            # Unseen candidate: the only real evidence is whether a price series
            # exists. Memoised, so each unknown symbol costs one lookup.
            if cand not in checked:
                checked[cand] = bool(prices.series(cand, cfg))
                probed += 1
            if checked[cand]:
                fixes.append((cand, r["id"]))
        if not dry_run:
            conn.executemany("UPDATE congress_trades SET ticker = ? WHERE id = ?",
                             fixes)
    if not quiet:
        verb = "would recover" if dry_run else "recovered"
        print(f"{verb} {len(fixes):,} tickers from {len(rows):,} untickered rows "
              f"({len(rows) - len(fixes):,} genuinely untickered; "
              f"{probed} unseen symbols checked against the price source, "
              f"{sum(checked.values())} real)")
    return 0


def mix(floor: int = 0, cfg=CONFIG) -> dict:
    """Asset mix per member, and overall: is this person trading or parking?"""
    with db.connect(cfg.db_path) as conn:
        rows = [dict(r) for r in conn.execute(
            """SELECT member, chamber, ticker, asset_name, amount_min
                 FROM congress_trades WHERE amount_min >= ?""", (floor,))]
    overall: dict[str, int] = {}
    per: dict[str, dict] = {}
    for r in rows:
        label = "Listed equity" if r["ticker"] else classify(r["asset_name"])
        overall[label] = overall.get(label, 0) + 1
        m = per.setdefault(r["member"], {"chamber": r["chamber"], "n": 0,
                                         "classes": {}})
        m["n"] += 1
        m["classes"][label] = m["classes"].get(label, 0) + 1
    members = []
    for name, m in per.items():
        if m["n"] < 10:
            continue
        eq = m["classes"].get("Listed equity", 0)
        members.append({
            "member": name, "chamber": m["chamber"], "n": m["n"],
            "equity_share": eq / m["n"],
            "top": max(m["classes"].items(), key=lambda kv: kv[1]),
            "classes": m["classes"],
        })
    members.sort(key=lambda x: x["equity_share"])
    return {"total": len(rows), "overall": overall, "members": members}


def to_markdown(d: dict) -> str:
    L = [f"# Asset mix — what is actually being disclosed", "",
         f"{d['total']:,} disclosures. Anything with a resolved ticker counts as "
         "listed equity; the rest is classified by name, since it cannot be priced.",
         "",
         "| class | disclosures | share |", "|---|--:|--:|"]
    for label, n in sorted(d["overall"].items(), key=lambda kv: -kv[1]):
        L.append(f"| {label} | {n:,} | {n/d['total']*100:.1f}% |")

    L += ["", "## Least equity-heavy filers", "",
          "A member whose disclosures are mostly Treasuries or money-market funds "
          "is preserving wealth, not picking stocks — and an alpha figure computed "
          "on their handful of equity trades says very little about them.",
          "",
          "| member | disclosures | listed equity | mostly |", "|---|--:|--:|---|"]
    for m in d["members"][:12]:
        L.append(f"| {m['member']} ({m['chamber'][:1]}) | {m['n']:,} "
                 f"| {m['equity_share']*100:.0f}% | {m['top'][0]} "
                 f"({m['top'][1]:,}) |")
    return "\n".join(L) + "\n"


def selftest(cfg=CONFIG):
    assert classify("US TREASURY BILL") == "Treasuries"
    assert classify("UNITED STATES TREAS BILLS") == "Treasuries"
    assert classify("BLF FedFund TDDXX [CS]") == "Cash & money market"
    assert classify("GS Managed Structured Note Strategy S&P 500 Linked Note") \
        == "Structured notes"
    assert classify("USA Compression Partners, LP") == "Partnerships & private"
    # The filing's own code beats the keyword fallback.
    assert classify("Goldman Sachs MTN 4/29/2030 [CS]") == "Corporate debt"
    assert classify("Los Angeles CA GO UTX [GS]") == "Government & municipal debt"
    assert classify("NVIDIA Corporation (NVDA) [OP]") == "Options & derivatives"
    assert classify("Litecoin (LTC) [CT]") == "Crypto"
    assert classify("Saronic Technologies [PS]") == "Private & pre-IPO equity"
    assert classify("Listen Ventures IV, LP [HN]") == "Hedge funds & non-public"
    # Abbreviated municipal names, with no code at all.
    assert classify("Illinois ST Sales Tax Rev JR Oblig") == "Government & municipal debt"
    assert classify("New York NY City Transitional Fin") == "Government & municipal debt"
    assert classify("Vanguard Total Stock Market Index Fund") == "Funds & trusts"
    assert classify("SPY Option [OT]") == "Options & derivatives"
    assert classify("") == "Other / unlabelled"

    # The repair pass must never invent a ticker we have no evidence for.
    with db.connect(cfg.db_path) as conn:
        known = known_tickers(conn)
    assert collect.ticker_from_name("Nonesuch Holdings (ZZZZQ)", known) == "", \
        "a candidate with no independent evidence was accepted"
    assert collect.ticker_from_name("Nonesuch Holdings (ZZZZQ)") == "ZZZZQ", \
        "the raw candidate should still be returned for the caller to validate"
    assert collect.ticker_from_name("Apple Inc. - Common Stock (AAPL)", known) == "AAPL"

    d = mix(cfg=cfg)
    assert sum(d["overall"].values()) == d["total"], "classes do not partition rows"
    assert all(0 <= m["equity_share"] <= 1 for m in d["members"])
    shares = [m["equity_share"] for m in d["members"]]
    assert shares == sorted(shares), "not sorted by equity share"
    txt = to_markdown(d)
    assert "Asset mix" in txt and "Listed equity" in txt
    print(f"selftest ok: {len(CLASSES)+2} classes, {d['total']:,} rows partitioned, "
          f"{len(d['members'])} members with 10+ disclosures")
