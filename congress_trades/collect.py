"""Congressional stock-trade disclosures (STOCK Act periodic transaction reports).

Two very different sources, one normalized row shape:

  House  -- Clerk publishes a yearly ZIP holding an XML index of every filing; the
            transaction detail is in per-DocID PDFs. Digitally generated, so pdftotext
            reads them. No auth, no rate limit.
  Senate -- efdsearch requires accepting a terms page to get a session cookie + CSRF
            token, then a paginated JSON search, then one HTML table per report.

Both are cached on disk keyed by document id: filings are immutable once posted, so a
weekly run only fetches what's new.

Normalized transaction:
  {chamber, member, owner, ticker, asset_name, tx_type, tx_date, disclosed, amount_min,
   amount_range, doc_id, doc_url}
"""
from __future__ import annotations

import html as _html
import io
import json
import logging
import re
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import requests

from .config import Config, user_agent

log = logging.getLogger("hermes_trends.congress")
TIMEOUT = 30


class MissingTool(RuntimeError):
    """An external binary the collector cannot work without is not installed.

    Kept apart from ordinary per-filing failures on purpose: those are logged and
    skipped, and a missing converter would otherwise skip every House filing with
    a warning apiece and hand back an empty, plausible-looking dataset.
    """


# --- House PDF layout -------------------------------------------------------------
# "P  07/24/2026 07/24/2026" -- type letter then transaction date then notification date.
#
# E is exchange, and it was missing until a parser audit found it. The Clerk's
# type codes are P (purchase), S (sale) and E (exchange); matching only [PS]
# dropped 65 transactions across 40 filings silently, and the deterministic scan
# could not see them because it counts with this same pattern. An exchange is
# not a directional trade, and nothing downstream scores it as one.
_H_TYPE = re.compile(r"\b([PSE])\b(?:\s*\(partial\))?\s+(\d{2}/\d{2}/\d{4})\s+(\d{2}/\d{2}/\d{4})")
_H_TX_TYPE = {"P": "buy", "S": "sell", "E": "exchange"}
# The original form: a ticker in parentheses followed by an asset-type code,
# e.g. "Apple Inc. (AAPL) [ST]". High confidence, so it is tried first.
_H_TICKER = re.compile(r"\(([A-Za-z][A-Za-z.\-]{0,6})\)\s*\[")
# The same thing without the type code -- "Apple Inc. - Common Stock (AAPL)" --
# which the Clerk's newer PDF template emits and the old pattern silently dropped.
_H_TICKER_BARE = re.compile(r"\(([A-Za-z]{1,5}(?:\.[A-Za-z]{1,2})?)\)")

# Words that sit in parentheses and are not tickers. pdftotext also renders some
# capitals in lower case -- "(bLK)", "(CAg)", "(TSlA)" -- so matching is
# case-insensitive and the result is upper-cased, which means these have to be
# excluded by name rather than by case.
_NOT_TICKERS = {
    "NYSE", "NASDAQ", "AMEX", "OTC", "ADR", "ADS", "ETF", "REIT", "LLC", "LP",
    "LLP", "INC", "CORP", "CO", "USD", "EUR", "CLASS", "COMMON", "STOCK", "FUND",
    "TRUST", "PLC", "NV", "SA", "AG", "AB", "SE", "IRA", "JR", "SR", "II", "III",
}


def ticker_from_name(name: str, known: set[str] | None = None) -> str:
    """Pull a ticker out of an asset name, or return "".

    One definition, two callers: the House parser uses it while reading filings,
    and `repair_tickers` uses it to recover rows an older parser dropped. Two
    copies of this logic would drift, and the recovered rows have to match the
    freshly parsed ones exactly or the same holding gets scored two ways.

    `known` restricts results to tickers already seen from EDGAR or elsewhere in
    the data; a wrong ticker on a real disclosure is worse than no ticker, since
    it would be priced and scored as another company. Call with known=None to
    get the raw candidate and validate it yourself -- `repair_tickers` does that
    by asking whether a price series exists, which is the only real evidence.
    """
    if not name:
        return ""
    m = _H_TICKER.search(name) or None
    if not m:
        # Several may match ("(A) (AAPL)"); the ticker is conventionally last.
        found = _H_TICKER_BARE.findall(name)
        cand = found[-1] if found else ""
    else:
        cand = m.group(1)
    cand = cand.upper().strip(".-")
    if not cand or cand in _NOT_TICKERS:
        return ""
    if known is not None and cand not in known:
        return ""
    return cand
_H_OWNER = re.compile(r"^\s*(SP|JT|DC)\b")
# The labelled fields under an asset -- Filing Status, Subholding Of, Description,
# Comments -- which pdftotext renders as "F S :", "S O :", "D :". Their text is
# about the holding, not its name: "Registered Index Linked Annuity (RILA)" or
# "403(b)" there was being read as a ticker, and past them sits the NEXT
# transaction's asset line, whose ticker was landing on this one.
_H_FIELD = re.compile(
    r"^\s*(?:[A-Z](?:\s+[A-Z])?|Filing Status|Subholding Of|Description|Comments?)\s*:",
    re.M)
_MONEY = re.compile(r"\$([\d,]+)")


def _money_min(text: str) -> int:
    m = _MONEY.search(text)
    return int(m.group(1).replace(",", "")) if m else 0


def _money_range(text: str) -> str:
    found = _MONEY.findall(text)
    if not found:
        return ""
    if len(found) == 1:
        return f"${found[0]}"
    return f"${found[0]} - ${found[1]}"


# ---------------------------------------------------------------------- House
def _house_ptr_text(cfg: Config, year: str, doc_id: str) -> str:
    cache = cfg.cache_dir / f"{doc_id}.txt"
    if cache.exists():
        return cache.read_text(encoding="utf-8", errors="ignore")
    url = f"{cfg.house_disc_base}/ptr-pdfs/{year}/{doc_id}.pdf"
    r = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": user_agent(cfg)})
    r.raise_for_status()
    with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
        tmp.write(r.content)
        tmp.flush()
        try:
            out = subprocess.run(["pdftotext", "-layout", tmp.name, "-"],
                                 capture_output=True, text=True, timeout=90)
        except FileNotFoundError:
            raise MissingTool(
                "pdftotext is not on PATH, and House filings are PDFs. Install "
                "Poppler:\n"
                "      brew install poppler              # macOS\n"
                "      sudo apt install poppler-utils    # Debian/Ubuntu\n"
                "      sudo dnf install poppler-utils    # Fedora/RHEL"
            ) from None
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(out.stdout, encoding="utf-8")
    return out.stdout


def parse_house_ptr(text: str) -> list[dict]:
    """Transactions out of one House PTR.

    Blank lines usually separate transaction blocks, but not always, and that
    "usually" cost 241 transactions across 90 filings. When a page break lands
    inside a transaction the Clerk reprints the column header there -- "ID Owner
    Asset / Transaction Date / Type ..." -- with no blank line around it, so two
    transactions end up in one block. Taking a single match per block dropped
    the second every time. Filing 20033446 lost 23 of its 473 that way.

    So every match in a block is a transaction, and each one is read from its
    own span: the asset line immediately before it, and the text up to the next
    match. Bounding the span matters as much as finding the match -- an unbounded
    tail would read the FOLLOWING transaction's amount bracket when a page break
    put the two together.
    """
    rows = []
    for block in re.split(r"\n\s*\n", text):
        found = list(_H_TYPE.finditer(block))
        for i, kind in enumerate(found):
            # The asset sits on the same line as the type letter, so the last
            # line before the match is the one to read. For the first match that
            # is the line the block opens with; for a later one it is the line
            # after the reprinted header, and taking the FIRST line there would
            # read the previous transaction's amount instead.
            head = block[found[i - 1].end():kind.start()] if i else block[:kind.start()]
            line = (head.splitlines() or [""])[-1]
            owner = _H_OWNER.search(line)
            # asset name = the head line minus its owner code, whitespace collapsed
            name = re.sub(r"^\s*(SP|JT|DC)\b", "", line).strip()
            name = re.sub(r"\s{2,}", " ", name).strip()
            # Stop at the next transaction, so the amounts and the ticker read
            # here belong to this one.
            stop = found[i + 1].start() if i + 1 < len(found) else len(block)
            tail = block[kind.end():stop]
            # Read from the head line, not the whole block: the tail holds amount
            # brackets and dates that can carry their own parentheses. The
            # fallback is this transaction's own tail rather than the block,
            # because the block may hold another transaction's symbol -- and only
            # the tail up to the first labelled field, where a wrapped asset name
            # ("... Corporation Common Stock (IBM)") ends.
            field = _H_FIELD.search(tail)
            tick = ticker_from_name(name) or ticker_from_name(
                tail[:field.start()] if field else tail)
            rows.append({
                "owner": {"SP": "Spouse", "JT": "Joint", "DC": "Dependent"}.get(
                    owner.group(1) if owner else "", "Self"),
                "ticker": tick,
                "asset_name": name[:160],
                "tx_type": _H_TX_TYPE.get(kind.group(1), ""),
                "tx_date": kind.group(2),
                "disclosed": kind.group(3),
                "amount_min": _money_min(tail),
                "amount_range": _money_range(tail),
            })
    return rows


def house_filings(cfg: Config, year: str) -> list[dict]:
    """PTR index entries for one year, newest first."""
    z = requests.get(f"{cfg.house_disc_base}/financial-pdfs/{year}FD.ZIP",
                     timeout=TIMEOUT, headers={"User-Agent": user_agent(cfg)})
    z.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(z.content)) as zf:
        xml_name = next(n for n in zf.namelist() if n.lower().endswith(".xml"))
        root = ET.fromstring(zf.read(xml_name))
    out = []
    for member in root:
        f = {c.tag: (c.text or "").strip() for c in member}
        if f.get("FilingType") != "P" or not f.get("DocID"):
            continue
        try:
            filed = datetime.strptime(f["FilingDate"], "%m/%d/%Y").date()
        except ValueError:
            continue
        out.append({
            "doc_id": f["DocID"],
            "year": f.get("Year", year),
            "member": f"{f.get('First','')} {f.get('Last','')}".strip(),
            "state": f.get("StateDst", ""),
            "filed": filed,
        })
    out.sort(key=lambda d: d["filed"], reverse=True)
    return out


def house_transactions(cfg: Config, years: list[str], progress=None) -> list[dict]:
    rows = []
    for year in years:
        try:
            filings = house_filings(cfg, year)
        except Exception as e:
            log.warning("house index %s failed: %s", year, e)
            continue
        for i, f in enumerate(filings):
            try:
                text = _house_ptr_text(cfg, f["year"], f["doc_id"])
            except MissingTool:
                raise                     # not this filing's problem; stop the run
            except Exception as e:
                log.warning("house PTR %s unreadable: %s", f["doc_id"], e)
                continue
            url = f"{cfg.house_disc_base}/ptr-pdfs/{f['year']}/{f['doc_id']}.pdf"
            for tx in parse_house_ptr(text):
                # Trust the index's filing date over the PDF's notification column: the
                # latter is hand-typed and does produce junk (seen: 03/28/1935). A bad
                # disclosure date would hide the trade from every future digest window.
                rows.append({**tx, "chamber": "House", "member": f["member"],
                             "state": f["state"], "doc_id": f["doc_id"], "doc_url": url,
                             "disclosed": f["filed"].isoformat()})
            if progress and i % 25 == 0:
                progress(f"house {year}: {i}/{len(filings)} filings")
    return rows


# --------------------------------------------------------------------- Senate
_SEN_HOME = "https://efdsearch.senate.gov/search/home/"
_SEN_DATA = "https://efdsearch.senate.gov/search/report/data/"


def _senate_session(cfg: Config) -> requests.Session:
    """Accept the prohibition-notice form to get a usable session + CSRF token."""
    s = requests.Session()
    s.headers["User-Agent"] = user_agent(cfg)
    r = s.get(_SEN_HOME, timeout=TIMEOUT)
    r.raise_for_status()
    m = re.search(r"name=['\"]csrfmiddlewaretoken['\"]\s+value=['\"]([^'\"]+)", r.text)
    if not m:
        raise RuntimeError("no CSRF token on efdsearch home page")
    s.post(_SEN_HOME, data={"csrfmiddlewaretoken": m.group(1), "prohibition_agreement": "1"},
           headers={"Referer": _SEN_HOME}, timeout=TIMEOUT).raise_for_status()
    s.headers["Referer"] = _SEN_HOME
    s.headers["X-Requested-With"] = "XMLHttpRequest"
    return s


def senate_filings(cfg: Config, since: str) -> list[dict]:
    """PTR index rows since a MM/DD/YYYY date. report_types=[11] is 'periodic transaction'."""
    s = _senate_session(cfg)
    out, start = [], 0
    while True:
        csrf = s.cookies.get("csrftoken", "")
        r = s.post(_SEN_DATA, data={
            "start": str(start), "length": "100", "report_types": "[11]",
            "filer_types": "[]", "submitted_start_date": f"{since} 00:00:00",
            "submitted_end_date": "", "candidate_state": "", "senator_state": "",
            "office_id": "", "first_name": "", "last_name": "",
            "csrfmiddlewaretoken": csrf,
        }, headers={"X-CSRFToken": csrf}, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json().get("data", [])
        if not data:
            break
        for row in data:
            href = re.search(r'href="([^"]+)"', row[3])
            if not href:
                continue
            out.append({
                "member": f"{_strip(row[0])} {_strip(row[1])}".strip(),
                # row[2] is the office label ("Doe, Jane (Senator)"), not a state code --
                # the Senate index has no clean state field, so leave it blank.
                "state": "",
                "doc_id": href.group(1).rstrip("/").rsplit("/", 1)[-1],
                "doc_url": "https://efdsearch.senate.gov" + href.group(1),
                "filed": _strip(row[4]),
            })
        start += len(data)
        if start >= r.json().get("recordsTotal", 0):
            break
        time.sleep(0.4)
    return out, s


def _strip(cell: str) -> str:
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", cell or ""))).strip()


def parse_senate_report(body: str) -> list[dict]:
    """Transactions out of one Senate PTR HTML page."""
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", body, re.S):
        cells = [_strip(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)]
        # #, Transaction Date, Owner, Ticker, Asset Name, Asset Type, Type, Amount, Comment
        if len(cells) < 8 or not re.fullmatch(r"\d+", cells[0]):
            continue
        kind = cells[6].lower()
        tx_type = "buy" if "purchase" in kind else "sell" if "sale" in kind else kind
        ticker = cells[3] if cells[3] not in ("--", "") else ""
        rows.append({
            "owner": cells[2] or "Self",
            "ticker": ticker,
            "asset_name": cells[4][:160],
            "tx_type": tx_type,
            "tx_date": cells[1],
            "disclosed": "",
            "amount_min": _money_min(cells[7]),
            "amount_range": cells[7],
        })
    return rows


def senate_transactions(cfg: Config, since: str, progress=None) -> list[dict]:
    try:
        filings, sess = senate_filings(cfg, since)
    except Exception as e:
        log.warning("senate index failed: %s", e)
        return []
    rows = []
    for i, f in enumerate(filings):
        cache = cfg.cache_dir / f"senate-{f['doc_id']}.html"
        try:
            if cache.exists():
                body = cache.read_text(encoding="utf-8", errors="ignore")
            else:
                r = sess.get(f["doc_url"], timeout=TIMEOUT)
                r.raise_for_status()
                body = r.text
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(body, encoding="utf-8")
                time.sleep(0.6)  # be polite to a .gov
        except Exception as e:
            log.warning("senate report %s failed: %s", f["doc_id"], e)
            continue
        report = _senate_title(body)
        for tx in parse_senate_report(body):
            rows.append({**tx, "chamber": "Senate", "member": f["member"], "report": report,
                         "state": f["state"], "doc_id": f["doc_id"],
                         "doc_url": f["doc_url"], "disclosed": tx["disclosed"] or f["filed"]})
        if progress and i % 25 == 0:
            progress(f"senate: {i}/{len(filings)} filings")
    return rows


def _senate_title(body: str) -> str:
    """'Periodic Transaction Report for 05/15/2025 (Amendment 1)', or ''."""
    m = re.search(r"<h1[^>]*>(.*?)</h1>", body, re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(1))).strip() if m else ""


_AMENDMENT = re.compile(r"^(.*?)\s*\(Amendment (\d+)\)$")
_TRADE_KEY = ("chamber", "member", "tx_date", "asset_name", "amount_range", "tx_type", "owner")


def supersede(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """(kept, dropped). Rows must already carry their row_idx.

    Two ways the same trade reaches the data twice, and both inflated every count
    built on it -- convergence most of all:

      - A Senate amendment re-files the whole report, usually with corrections
        (44 of 60 amended reports differed from their original). The highest
        amendment of a member's "Report for <date>" replaces every earlier
        version outright, so a corrected amount or a withdrawn line is gone
        rather than counted alongside its correction.
      - Any filing, either chamber, can repeat a trade an earlier one disclosed.
        The House marks nothing, so this is matched on content: a later filing's
        copy of an identical trade is dropped, counting repeats, so two genuine
        identical lots in one filing both survive.

    A trade the public already knew about keeps the date it was FIRST disclosed:
    returns are measured from when the information became public, and an
    amendment does not make it news again.
    """
    # 1. Senate amendments: the latest version of each report wins.
    version: dict[tuple, int] = {}
    for r in rows:
        if r.get("chamber") == "Senate":
            m = _AMENDMENT.match(r.get("report", "")) or None
            base, n = (m.group(1), int(m.group(2))) if m else (r.get("report", ""), 0)
            r["_report"], r["_version"] = (r["member"], base), n
            version[r["_report"]] = max(version.get(r["_report"], 0), n)
    first_seen: dict[tuple, str] = {}
    for r in rows:
        if "_report" in r:
            k = tuple(r.get(f) for f in _TRADE_KEY)
            if r.get("disclosed") and (k not in first_seen or r["disclosed"] < first_seen[k]):
                first_seen[k] = r["disclosed"]
    dropped = [r for r in rows if "_report" in r and r["_version"] < version[r["_report"]]]
    out = [r for r in rows if not ("_report" in r and r["_version"] < version[r["_report"]])]
    for r in out:
        k = tuple(r.get(f) for f in _TRADE_KEY)
        if "_report" in r and first_seen.get(k, r["disclosed"]) < r["disclosed"]:
            r["disclosed"] = first_seen[k]

    # 2. Re-reports: walk filings oldest first; a filing may repeat a trade only as
    # many times as no earlier filing already has.
    by_doc: dict[str, list[dict]] = {}
    for r in out:
        by_doc.setdefault(r["doc_id"], []).append(r)
    order = sorted(by_doc, key=lambda d: (min(r.get("disclosed") or "9" for r in by_doc[d]), d))
    seen: dict[tuple, int] = {}
    kept = []
    for d in order:
        here: dict[tuple, int] = {}
        for r in by_doc[d]:
            k = tuple(r.get(f) for f in _TRADE_KEY)
            here[k] = here.get(k, 0) + 1
            (dropped if here[k] <= seen.get(k, 0) else kept).append(r)
        for k, n in here.items():
            seen[k] = max(seen.get(k, 0), n)
    for r in rows:
        r.pop("_report", None), r.pop("_version", None)
    return kept, dropped


# ----------------------------------------------------------------- normalize
def _iso(d: str) -> str:
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(d, fmt).date().isoformat()
        except (ValueError, TypeError):
            continue
    return ""


_HONORIFIC = re.compile(r"\b(?:Mr|Mrs|Ms|Dr|Rev|Hon)\.?\s+")


def clean_member_name(name: str) -> str:
    """The Clerk's index occasionally embeds an honorific in First or Last
    ('Scott' + 'Mr Franklin' -> 'Scott Mr Franklin'), which then reads as a
    second, distinct member everywhere the name is used as a key. Strip it."""
    return _HONORIFIC.sub("", name or "").strip()


def normalize(rows: list[dict], min_amount: int) -> list[dict]:
    """Apply the disclosure-bracket floor and ISO-ify dates. Untickered assets are kept
    (they still belong on a member's timeline); ticker rollups skip them."""
    out = []
    for r in rows:
        if r.get("amount_min", 0) < min_amount:
            continue
        tx_date = _iso(r.get("tx_date", ""))
        if not tx_date:
            continue
        out.append({**r, "member": clean_member_name(r.get("member", "")),
                    "tx_date": tx_date, "disclosed": _iso(r.get("disclosed", ""))})
    return out
