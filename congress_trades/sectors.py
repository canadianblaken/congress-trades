"""Ticker -> industry sector, via the SEC's own classification.

Two SEC endpoints, both free and key-less (they only require a real User-Agent):
  company_tickers.json      ticker -> CIK
  data.sec.gov/submissions  CIK    -> SIC code + description

SIC is used rather than a vendor taxonomy because it is authoritative, stable, and
free. It is also coarse in places, so the precise `sicDescription` is kept alongside
the rolled-up sector for display.

Lookups are cached on disk permanently -- a company's industry does not change.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import requests

from .config import Config, user_agent

log = logging.getLogger("hermes_trends.sectors")
TIMEOUT = 30
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:010d}.json"

# SIC major group (first two digits) -> readable sector.
_RANGES: list[tuple[int, int, str]] = [
    (1, 9, "Agriculture"),
    (10, 14, "Mining & Energy Extraction"),
    (15, 17, "Construction & Engineering"),
    (20, 21, "Food & Beverage"),
    (22, 23, "Textiles & Apparel"),
    (24, 27, "Paper, Wood & Publishing"),
    (28, 28, "Pharma & Chemicals"),
    (29, 29, "Petroleum Refining"),
    (30, 32, "Materials"),
    (33, 34, "Metals & Fabrication"),
    (35, 35, "Machinery & Computer Equipment"),
    (36, 36, "Electronics & Electrical Equipment"),
    (37, 37, "Transportation Equipment"),
    (38, 38, "Instruments & Medical Devices"),
    (39, 39, "Misc Manufacturing"),
    (40, 47, "Transportation & Logistics"),
    (48, 48, "Communications"),
    (49, 49, "Utilities & Power"),
    (50, 51, "Wholesale"),
    (52, 59, "Retail"),
    (60, 62, "Banking & Finance"),
    (63, 64, "Insurance"),
    (65, 66, "Real Estate"),
    (67, 67, "Funds & Holding Companies"),
    (70, 72, "Consumer Services"),
    (73, 73, "Software & IT Services"),
    (74, 79, "Consumer Services"),
    (80, 80, "Healthcare Services"),
    (81, 89, "Professional Services"),
    (91, 99, "Public Administration"),
]

# Where the major group is too coarse to be useful for this dataset.
_SPECIFIC = {
    "3674": "Semiconductors",
    "3663": "Communications Equipment",
    "3585": "HVAC & Refrigeration",
    "4911": "Utilities & Power",
    "4931": "Utilities & Power",
    "4813": "Communications",
    "7372": "Software & IT Services",
    "6798": "Real Estate (REIT)",
}


# Every sector name this project uses, which is exactly the set that can appear
# in ticker_sectors.sector. Derived rather than typed out: the committee-hearing
# tagger in topics.py is handed this as a closed enum, and a hearing tagged with a
# sector no ticker can carry would silently never match a trade.
VOCAB: tuple[str, ...] = tuple(sorted(
    {name for _, _, name in _RANGES} | set(_SPECIFIC.values()) | {"Unclassified"}))


def sector_for_sic(sic: str) -> str:
    sic = (sic or "").strip()
    if sic in _SPECIFIC:
        return _SPECIFIC[sic]
    if not sic.isdigit():
        return "Unclassified"
    mg = int(sic[:2])
    for lo, hi, name in _RANGES:
        if lo <= mg <= hi:
            return name
    return "Unclassified"


def ticker_map(cfg: Config) -> dict[str, int]:
    """ticker -> CIK, cached for 30 days (new listings appear occasionally)."""
    cache = cfg.cache_dir / "sec_tickers.json"
    if cache.exists() and time.time() - cache.stat().st_mtime < 30 * 86400:
        return json.loads(cache.read_text())
    r = requests.get(TICKERS_URL, timeout=TIMEOUT, headers={"User-Agent": user_agent(cfg)})
    r.raise_for_status()
    out = {row["ticker"].upper(): int(row["cik_str"]) for row in r.json().values()}
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(out))
    return out


def ticker_titles(cfg: Config) -> dict[str, str]:
    """ticker -> registered company name, from the same SEC file, cached 30 days.

    ticker_map() throws the title away because sector lookup only needs the CIK.
    Ticker recovery needs the name: a symbol a model proposed is only accepted if
    SEC's own name for it recognisably matches the asset text in the filing, and
    that check is the entire difference between recovering AMD and inventing it.
    """
    cache = cfg.cache_dir / "sec_ticker_titles.json"
    if cache.exists() and time.time() - cache.stat().st_mtime < 30 * 86400:
        try:
            return json.loads(cache.read_text())
        except ValueError:
            pass
    r = requests.get(TICKERS_URL, timeout=TIMEOUT, headers={"User-Agent": user_agent(cfg)})
    r.raise_for_status()
    out = {row["ticker"].upper(): (row.get("title") or "")
           for row in r.json().values()}
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(out))
    return out


def lookup(cfg: Config, ticker: str, tmap: dict | None = None,
           _last=[0.0]) -> dict | None:
    """Company + SIC + rolled-up sector for one ticker. Cached permanently."""
    t = (ticker or "").strip().upper()
    if not t:
        return None
    cache = cfg.cache_dir / f"sic-{t.replace('/', '_')}.json"
    if cache.exists():
        try:
            return json.loads(cache.read_text())
        except ValueError:
            pass
    tmap = tmap if tmap is not None else ticker_map(cfg)
    cik = tmap.get(t) or tmap.get(t.replace(".", "-")) or tmap.get(t.split(".")[0])
    if not cik:
        return None
    gap = time.time() - _last[0]
    if gap < 0.15:                      # SEC asks for <= 10 req/s
        time.sleep(0.15 - gap)
    try:
        r = requests.get(SUBMISSIONS.format(cik=cik), timeout=TIMEOUT,
                         headers={"User-Agent": user_agent(cfg)})
        _last[0] = time.time()
        if r.status_code != 200:
            return None
        j = r.json()
    except Exception as e:
        log.warning("sec %s: %s", t, e)
        return None
    out = {
        "ticker": t, "cik": str(cik), "company": j.get("name", ""),
        "sic": j.get("sic", ""), "sic_desc": j.get("sicDescription", ""),
        "sector": sector_for_sic(j.get("sic", "")),
    }
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(out))
    return out
