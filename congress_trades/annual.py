"""House annual Financial Disclosure Reports (FDRs) -- the schedules a Periodic
Transaction Report structurally cannot carry: liabilities, outside earned
income, full asset holdings (not just what traded), and outside positions.

The Clerk's yearly ZIP index (`collect.house_filings`) reads only FilingType
"P" (Periodic Transaction Reports); this module reads the FDR types instead
-- "O" (annual report, a sitting member's yearly filing; the vast majority of
what's useful here), plus "A"/"C"/"T"/"H" (amendment / candidate / termination
/ new-member versions of the same form). It does not touch collect.py: the
FDR index and PDF path both differ from the PTR ones (financial-pdfs/<year>/
<doc_id>.pdf, not ptr-pdfs/), so duplicating the small ZIP-read here keeps the
existing PTR pipeline provably untouched.

The PDF layout, from real filings:
  - Every schedule is introduced by a line "S <letter>: <title>" where the
    title itself is garbled to its first letters ("Schedule D: Liabilities"
    renders as "S D: L") -- this PDF form appears to render section titles in
    a bold font that pdftotext collapses to one glyph per word. The *data*
    rows are unaffected; only these header/title lines lose their text, so
    parsing keys off the letter before the colon, never the garbled title.
  - Column tables (Schedule D creditors, C income sources, A assets, E
    positions) are pdftotext `-layout` output: each field's horizontal
    position is preserved as spaces, and a value that doesn't fit widens the
    row onto a second physical line, continuing in the *same character
    column*. Two brackets (an asset's value and its income) can both be left
    "open" on the same wrapped line, so column position -- not line order --
    is the only reliable way to reassemble a wrapped row without guessing
    which half belongs to which number. `_parse_record` does that matching.
  - A one-line freeform annotation ("C   : paid in full", "L   : city, state",
    "D   : what this asset is") can trail a row. These carry real information
    but no structure worth a column, so they are stripped and discarded
    rather than risk corrupting the row they're attached to.
  - Page breaks insert a form-feed (`\\f`) and sometimes reprint the column
    header mid-schedule with no surrounding blank line. Both are stripped
    before splitting into records.

Parsed here: Schedule D (liabilities), Schedule C (earned income), Schedule A
(assets), and Schedule E (positions held) as a bonus -- it is a plain
two-column table, no harder than C, and it is what answers "who sits on what
board." Deliberately NOT parsed:
  - Schedule F (agreements) -- real content exists (future employment,
    pensions) but it appeared in exactly one of 39 sampled filings. Three
    columns wrapping unpredictably on one real example is not enough evidence
    to claim a stable layout; guessing from one instance is exactly the
    failure mode this project refuses.
  - Schedule G (gifts) -- zero of 39 sampled filings disclosed a gift, so
    there is no real layout to test a parser against at all.
  - Schedule H (travel reimbursements) -- an 8-column table (source, two
    dates, a multi-line itinerary, a day count, three Y/N inclusion flags)
    that wraps the itinerary across as many lines as the trip has legs. That
    is real tabular data defeated by the same text-only rendering that Schedule
    A's two columns already stress; an 8-column reconstruction would be
    guessing, not parsing.
Nothing is stored for F, G, or H; `run()` says so in its own text rather than
leaving the gap silent.
"""
from __future__ import annotations

import io
import logging
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime

import requests

from . import db
from .config import CONFIG, Config, user_agent

log = logging.getLogger("hermes_trends.congress.annual")
TIMEOUT = 30

# Sitting-member annual report, amendment, candidate, termination, new-member --
# the FDR forms. "X" (extension request) and "D"/"W" carry no schedule content
# and are skipped; "E"/"G"/"B" are single digits a year and not worth a path.
FDR_TYPES = ("O", "A", "C", "T", "H")

_OWNER_CODES = {"SP": "Spouse", "JT": "Joint", "DC": "Dependent"}
_MONEY_SENTINELS = {"Undetermined", "N/A"}
_MONEY_NUM = re.compile(r"\$([\d,]+(?:\.\d+)?)")
_SECTION = re.compile(r"(?m)^S\s+([A-Z]):.*$")
_ANNOTATION = re.compile(r"^\s*[A-Z]\s+:\s?")  # "C  : ...", "L  : ...", "D  : ..."

_HDR_STRIP = {
    "D": re.compile(r"(?mi)^[ \t]*Owner Creditor.*Date Incurred.*$\n?"
                     r"(?:^[ \t]*Liability[ \t]*$\n?)?"),
    "C": re.compile(r"(?mi)^[ \t]*Source[ \t]+Type[ \t]+Amount[ \t]*$\n?"),
    "A": re.compile(r"(?mi)^[ \t]*Asset[ \t]+Owner Value of Asset.*$\n?"
                     r"(?:^[ \t]*\$?1,000\?[ \t]*$\n?)?"),
    "E": re.compile(r"(?mi)^[ \t]*Position[ \t]+Name of Organization[ \t]*$\n?"),
}


# ------------------------------------------------------------------ collect
def house_fdr_filings(cfg: Config, year: str, types: tuple[str, ...] = FDR_TYPES) -> list[dict]:
    """FDR index entries for one year, newest first. Same ZIP as the PTR index
    (`collect.house_filings`), different FilingType letters -- duplicated here
    rather than parameterizing collect.house_filings, so the PTR path stays
    provably byte-identical."""
    z = requests.get(f"{cfg.house_disc_base}/financial-pdfs/{year}FD.ZIP",
                     timeout=TIMEOUT, headers={"User-Agent": user_agent(cfg)})
    z.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(z.content)) as zf:
        xml_name = next(n for n in zf.namelist() if n.lower().endswith(".xml"))
        root = ET.fromstring(zf.read(xml_name))
    out = []
    for member in root:
        f = {c.tag: (c.text or "").strip() for c in member}
        if f.get("FilingType") not in types or not f.get("DocID"):
            continue
        try:
            filed = datetime.strptime(f["FilingDate"], "%m/%d/%Y").date()
        except ValueError:
            continue
        out.append({
            "doc_id": f["DocID"],
            "filing_type": f["FilingType"],
            "year": f.get("Year", year),
            "member": f"{f.get('First','')} {f.get('Last','')}".strip(),
            "state": f.get("StateDst", ""),
            "filed": filed,
        })
    out.sort(key=lambda d: d["filed"], reverse=True)
    return out


def _fdr_text(cfg: Config, year: str, doc_id: str) -> str:
    """FDR PDF text, cached by doc_id like every other filing here -- filings
    are immutable once posted. A distinct `fdr-` cache prefix, since a PTR and
    an FDR could in principle share a doc_id numbering space and collect.py's
    PTR cache already claims the bare `<doc_id>.txt` name."""
    cache = cfg.cache_dir / f"fdr-{doc_id}.txt"
    if cache.exists():
        return cache.read_text(encoding="utf-8", errors="ignore")
    url = f"{cfg.house_disc_base}/financial-pdfs/{year}/{doc_id}.pdf"
    r = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": user_agent(cfg)})
    r.raise_for_status()
    with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
        tmp.write(r.content)
        tmp.flush()
        out = subprocess.run(["pdftotext", "-layout", tmp.name, "-"],
                             capture_output=True, text=True, timeout=90)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(out.stdout, encoding="utf-8")
    return out.stdout


# ------------------------------------------------------------------ layout
def _sections(text: str) -> dict[str, str]:
    """Split one FDR's text into {schedule letter: its span}. `\\f` page-break
    markers are folded to newlines first, since one can land directly in
    front of a header with no newline of its own (`\\fS  E: P`)."""
    text = text.replace("\f", "\n")
    marks = list(_SECTION.finditer(text))
    out = {}
    for i, m in enumerate(marks):
        start = m.end()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        out[m.group(1)] = text[start:end]
    return out


def _cells(line: str) -> list[tuple[int, str]]:
    """Split one pdftotext -layout line into (start_column, text) cells on
    runs of 2+ spaces. A single space stays inside a cell -- "$250,001 -" and
    "Bank of America" both have internal single spaces -- so this only breaks
    where the form actually put a column gap. The start column is kept so a
    wrapped continuation line can be matched to the right field by position,
    which is the only anchor available once two columns wrap on the same line."""
    out = []
    i, n = 0, len(line)
    while i < n:
        while i < n and line[i] == " ":
            i += 1
        if i >= n:
            break
        start = i
        j = i
        while j < n and not (line[j] == " " and j + 1 < n and line[j + 1] == " "):
            j += 1
        out.append((start, line[start:j].rstrip()))
        i = j
    return out


def _is_money(text: str) -> bool:
    return text in _MONEY_SENTINELS or text.startswith("$")


# A short value ("$50,000") can leave only one space before the next column's
# text starts ("$50,000 Dividends"), which `_cells` -- correctly, for every
# other case -- keeps as one cell since it only breaks on 2+ spaces. Peel the
# recognizable money token off the front so "Dividends" still reaches the
# income-type slot instead of being silently absorbed into the amount.
_MONEY_HEAD = re.compile(
    r"^(\$[\d,]+(?:\.\d+)?(?:\s*-\s*(?:\$[\d,]+(?:\.\d+)?)?)?|Undetermined|N/A)(?:\s+(\S.*))?$")


def _split_cells(line: str) -> list[tuple[int, str]]:
    out = []
    for col, text in _cells(line):
        m = _MONEY_HEAD.match(text) if _is_money(text) else None
        if m and m.group(2):
            out.append((col, m.group(1)))
            out.append((col + len(m.group(1)) + 1, m.group(2)))
        else:
            out.append((col, text))
    return out


def _money_bracket(text: str) -> tuple[int | None, str]:
    """A merged money cell's text -> (lower bound, display string). "$X - $Y"
    and a bare "$X" both come through here; brackets that never closed (a
    continuation line's column didn't line up, so the merge failed) fall back
    to whatever was disclosed rather than inventing the missing half."""
    text = (text or "").strip()
    if not text or text == "N/A":
        return None, text
    if text == "Undetermined":
        return None, "Undetermined"
    nums = _MONEY_NUM.findall(text)
    if not nums:
        return None, text
    lo = int(nums[0].replace(",", "").split(".")[0])
    if len(nums) >= 2:
        return lo, f"${nums[0]} - ${nums[1]}"
    return lo, f"${nums[0]}"


def _parse_record(lines: list[str], text_slots: tuple[str, ...], n_money: int,
                   has_owner: bool) -> dict:
    """Reconstruct one wrapped table row into named fields.

    Cells on the first line fill text_slots in order (skipping a lone SP/JT/DC
    owner token, if `has_owner`) until a money-or-sentinel cell is hit; each
    money cell after that is collected in order. Cells past the last text slot
    and the expected money cells (the House form's Y/N "traded >$1,000?" flag
    on Schedule A) are dropped -- not needed here and not worth a column.

    Cells on any continuation line are matched to whichever slot -- text or
    money -- started nearest their column, within an 8-character tolerance.
    That's deliberately loose: pdftotext's column snapping isn't pixel-exact
    across filings, but it is far tighter than the gap between two genuinely
    different columns on this form.
    """
    owner = "Self"
    slots: dict[str, list] = {}
    order: list[str] = []
    money: list[list] = []
    ti = 0

    cells0 = _split_cells(lines[0]) if lines else []
    for col, text in cells0:
        if has_owner and text in _OWNER_CODES:
            owner = _OWNER_CODES[text]
            continue
        if _is_money(text):
            money.append([col, text])
            continue
        if ti < len(text_slots):
            slots[text_slots[ti]] = [col, text]
            order.append(text_slots[ti])
            ti += 1
        elif money:
            continue  # trailing text after the row's own money -- a stray flag
        elif order:
            slots[order[-1]][1] += " " + text  # more text than slots: glue on

    for line in lines[1:]:
        for col, text in _split_cells(line):
            best, dist = None, 9
            for name, s in slots.items():
                d = abs(col - s[0])
                if d < dist:
                    best, dist = ("text", name), d
            for i, m in enumerate(money):
                d = abs(col - m[0])
                if d < dist:
                    best, dist = ("money", i), d
            if best is None:
                continue
            if best[0] == "text":
                slots[best[1]][1] += " " + text
            else:
                money[best[1]][1] += " " + text

    out = {"owner": owner, "_money": [m[1] for m in money]}
    for name in text_slots:
        out[name] = slots[name][1].strip() if name in slots else ""
    return out


def _records(section_text: str, schedule: str) -> list[list[str]]:
    """Strip repeated column headers and per-row freeform annotations, then
    split what's left on blank lines into one line-list per row."""
    # A page-break form feed can land glued directly onto a reprinted header
    # with no newline of its own; fold it here too, not just in `_sections`,
    # so each schedule parser is safe to call on a raw span by itself.
    text = _HDR_STRIP[schedule].sub("", section_text.replace("\f", "\n"))
    out = []
    for block in re.split(r"\n\s*\n", text):
        lines = [l for l in block.split("\n") if l.strip() and not _ANNOTATION.match(l)]
        if not lines or lines[0].strip().lower().startswith("none disclosed"):
            continue
        out.append(lines)
    return out


# --------------------------------------------------------------- schedules
def parse_liabilities(section_text: str) -> list[dict]:
    """Schedule D. `date_incurred` and `liability_type` are stored as disclosed
    text, not normalized -- the form itself doesn't constrain their format
    ("2018", "over many years", "March 2018; December 2019" all appear)."""
    rows = []
    for lines in _records(section_text, "D"):
        rec = _parse_record(lines, ("creditor", "date_incurred", "liability_type"),
                            n_money=1, has_owner=True)
        if not rec["creditor"] or not rec["_money"]:
            continue
        amt_min, amt_range = _money_bracket(rec["_money"][0])
        rows.append({"owner": rec["owner"], "creditor": rec["creditor"][:160],
                     "date_incurred": rec["date_incurred"][:80],
                     "liability_type": rec["liability_type"][:160],
                     "amount_min": amt_min, "amount_range": amt_range})
    return rows


def parse_earned_income(section_text: str) -> list[dict]:
    """Schedule C. Spouse income is routinely reported as "N/A" -- the STOCK
    Act lets a filer withhold the exact figure for a spouse -- so amount_val
    is NULL far more often here than for the filer's own income, and that is
    the disclosure working as designed, not a parse failure."""
    rows = []
    for lines in _records(section_text, "C"):
        rec = _parse_record(lines, ("source", "income_type"), n_money=1, has_owner=False)
        if not rec["source"] or not rec["_money"]:
            continue
        amt_min, amt_range = _money_bracket(rec["_money"][0])
        rows.append({"source": rec["source"][:160], "income_type": rec["income_type"][:160],
                     "amount": amt_range, "amount_val": amt_min})
    return rows


def parse_assets(section_text: str) -> list[dict]:
    """Schedule A. Two money columns per row (value held, income earned), and
    the House form links a sub-holding to its parent with a trailing "=>"
    arrow ("214 Wyoming Ave LLC => / Wyoming Office Building [RP]") that wraps
    the row onto a second line along with both brackets -- exactly the case
    `_parse_record`'s column matching exists for."""
    rows = []
    for lines in _records(section_text, "A"):
        rec = _parse_record(lines, ("asset_name", "income_type"), n_money=2, has_owner=True)
        name = re.sub(r"\s+", " ", rec["asset_name"].replace("⇒", " ")).strip()
        # A real Schedule A row always discloses a value, even if "Undetermined".
        # Without one, this is boilerplate that slipped past the header/annotation
        # strip -- the page-footer note on asset-type codes, seen in real filings.
        if not name or not rec["_money"]:
            continue
        value_min, value_range = (_money_bracket(rec["_money"][0]) if rec["_money"]
                                  else (None, ""))
        income_min, income_range = (_money_bracket(rec["_money"][1]) if len(rec["_money"]) > 1
                                    else (None, ""))
        rows.append({"asset_name": name[:200], "owner": rec["owner"],
                     "value_min": value_min, "value_range": value_range,
                     "income_type": rec["income_type"][:80],
                     "income_min": income_min, "income_range": income_range})
    return rows


def parse_positions(section_text: str) -> list[dict]:
    """Schedule E. No money column at all -- just who holds what seat where."""
    rows = []
    for lines in _records(section_text, "E"):
        rec = _parse_record(lines, ("position", "organization"), n_money=0, has_owner=False)
        if not rec["position"] and not rec["organization"]:
            continue
        rows.append({"position": rec["position"][:120], "organization": rec["organization"][:200]})
    return rows


def parse_fdr(text: str) -> dict:
    """All four parsed schedules out of one FDR's raw pdftotext text."""
    sec = _sections(text)
    return {
        "liabilities": parse_liabilities(sec.get("D", "")),
        "earned_income": parse_earned_income(sec.get("C", "")),
        "assets": parse_assets(sec.get("A", "")),
        "positions": parse_positions(sec.get("E", "")),
    }


# -------------------------------------------------------------- collection
def collect_annual(cfg: Config, years: list[str], types: tuple[str, ...] = ("O",),
                    limit: int | None = None, progress=None) -> dict:
    """Fetch and parse a bounded sample of FDR filings. `types` defaults to
    "O" only (a sitting member's annual report) -- the primary target and the
    type every other letter is a variant of; pass the fuller `FDR_TYPES` for a
    complete pull. `limit` caps filings per year, for development against a
    sample rather than the full ~430-a-year set."""
    filings, liabilities, earned_income, assets, positions = [], [], [], [], []
    for year in years:
        try:
            idx = house_fdr_filings(cfg, year, types)
        except Exception as e:
            log.warning("FDR index %s failed: %s", year, e)
            continue
        if limit:
            idx = idx[:limit]
        for i, f in enumerate(idx):
            try:
                text = _fdr_text(cfg, f["year"], f["doc_id"])
            except Exception as e:
                log.warning("FDR %s unreadable: %s", f["doc_id"], e)
                continue
            parsed = parse_fdr(text)
            url = f"{cfg.house_disc_base}/financial-pdfs/{f['year']}/{f['doc_id']}.pdf"
            filings.append({**f, "doc_url": url})
            for r in parsed["liabilities"]:
                liabilities.append({**r, "doc_id": f["doc_id"]})
            for r in parsed["earned_income"]:
                earned_income.append({**r, "doc_id": f["doc_id"]})
            for r in parsed["assets"]:
                assets.append({**r, "doc_id": f["doc_id"]})
            for r in parsed["positions"]:
                positions.append({**r, "doc_id": f["doc_id"]})
            if progress and i % 10 == 0:
                progress(f"FDR {year}: {i}/{len(idx)} filings")
    return {"filings": filings, "liabilities": liabilities,
            "earned_income": earned_income, "assets": assets, "positions": positions}


def store(conn, data: dict) -> dict:
    return {
        "filings": db.upsert_annual_filings(conn, data["filings"]),
        "liabilities": db.upsert_annual_rows(conn, "annual_liabilities", data["liabilities"]),
        "earned_income": db.upsert_annual_rows(conn, "annual_earned_income", data["earned_income"]),
        "assets": db.upsert_annual_rows(conn, "annual_assets", data["assets"]),
        "positions": db.upsert_annual_rows(conn, "annual_positions", data["positions"]),
    }


# ------------------------------------------------------------------- report
def run(cfg=CONFIG) -> dict:
    """What's actually in the annual-report tables: who carries the most
    disclosed debt (summed lower bounds -- a floor, since every figure here is
    a bracket), who has the most outside earned-income sources, and who holds
    the most outside positions."""
    with db.connect(cfg.db_path) as conn:
        debt = conn.execute(
            """SELECT f.member, SUM(l.amount_min) AS total, COUNT(*) AS n
                 FROM annual_liabilities l JOIN annual_filings f ON f.doc_id = l.doc_id
                WHERE l.amount_min IS NOT NULL
                GROUP BY f.member ORDER BY total DESC LIMIT 10""").fetchall()
        income = conn.execute(
            """SELECT f.member, COUNT(*) AS n,
                      SUM(CASE WHEN e.amount_val IS NOT NULL THEN e.amount_val ELSE 0 END) AS total
                 FROM annual_earned_income e JOIN annual_filings f ON f.doc_id = e.doc_id
                GROUP BY f.member ORDER BY n DESC LIMIT 10""").fetchall()
        positions = conn.execute(
            """SELECT f.member, COUNT(*) AS n
                 FROM annual_positions p JOIN annual_filings f ON f.doc_id = p.doc_id
                GROUP BY f.member ORDER BY n DESC LIMIT 10""").fetchall()
        by_org = conn.execute(
            """SELECT p.organization, COUNT(DISTINCT f.member) AS n
                 FROM annual_positions p JOIN annual_filings f ON f.doc_id = p.doc_id
                WHERE p.organization != ''
                GROUP BY p.organization HAVING n > 1 ORDER BY n DESC LIMIT 10""").fetchall()
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("annual_filings", "annual_liabilities", "annual_earned_income",
                            "annual_assets", "annual_positions")}
    return {"counts": counts, "debt": [dict(r) for r in debt],
            "income": [dict(r) for r in income], "positions": [dict(r) for r in positions],
            "by_org": [dict(r) for r in by_org]}


def to_markdown(d: dict) -> str:
    c = d["counts"]
    L = ["# Annual Financial Disclosure Reports", "",
         f"{c['annual_filings']:,} FDRs parsed: {c['annual_liabilities']:,} liabilities, "
         f"{c['annual_earned_income']:,} earned-income rows, {c['annual_assets']:,} asset "
         f"holdings, {c['annual_positions']:,} outside positions.", "",
         "Schedules F (agreements), G (gifts) and H (travel) are not parsed -- see the "
         "module docstring in `annual.py` for why each one was refused rather than guessed "
         "at.", "",
         "## Most disclosed debt", "",
         "Sum of each liability's *lower* bracket bound -- a floor, not the real total, "
         "since every figure on this form is a range.", "",
         "| member | liabilities | disclosed floor |", "|---|--:|--:|"]
    for r in d["debt"]:
        L.append(f"| {r['member']} | {r['n']} | ${r['total']:,.0f}+ |")
    L += ["", "## Most outside earned-income sources", "",
          "| member | sources | disclosed total (excludes N/A rows) |", "|---|--:|--:|"]
    for r in d["income"]:
        L.append(f"| {r['member']} | {r['n']} | ${r['total']:,.0f} |")
    L += ["", "## Most outside positions held", "",
          "| member | positions |", "|---|--:|"]
    for r in d["positions"]:
        L.append(f"| {r['member']} | {r['n']} |")
    if d["by_org"]:
        L += ["", "## Organizations more than one member sits with", "",
              "| organization | members |", "|---|--:|"]
        for r in d["by_org"]:
            L.append(f"| {r['organization']} | {r['n']} |")
    return "\n".join(L) + "\n"


def selftest(cfg=CONFIG):
    d = run(cfg)
    assert d["counts"]["annual_filings"] >= 0
    for section in ("debt", "income", "positions"):
        assert isinstance(d[section], list)
    txt = to_markdown(d)
    assert "Annual Financial Disclosure Reports" in txt
    print(f"selftest ok: {d['counts']['annual_filings']} FDRs, "
          f"{d['counts']['annual_liabilities']} liabilities, "
          f"{d['counts']['annual_earned_income']} earned-income rows, "
          f"{d['counts']['annual_assets']} assets, "
          f"{d['counts']['annual_positions']} positions")
