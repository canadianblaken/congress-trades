"""Member identity + biography, from the canonical `unitedstates/congress-legislators`
dataset plus Wikipedia summaries.

Disclosure filings give a formal name and (for the House) a state/district code, and
nothing else -- no party, no state for senators, no bio. This module resolves a filing
name to a real legislator so the rest of the app can show who the person actually is.

Matching is surname-first because filings use formal names the person doesn't go by
("Rafael E Cruz" -> Ted Cruz, "A. Mitchell McConnell, Jr." -> Mitch McConnell), so a
first-name match fails far more often than a surname match. State disambiguates the
duplicate surnames; first initial breaks the rest.

Both rosters are cached on disk; the historical one is trimmed to members who left
recently, since nobody filing a PTR today left Congress in 1953.
"""
from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from pathlib import Path

import requests

from .config import Config, user_agent

log = logging.getLogger("hermes_trends.legislators")
TIMEOUT = 30
CURRENT_URL = "https://unitedstates.github.io/congress-legislators/legislators-current.json"
COMMITTEES_URL = "https://unitedstates.github.io/congress-legislators/committees-current.json"
MEMBERSHIP_URL = "https://unitedstates.github.io/congress-legislators/committee-membership-current.json"
HISTORICAL_URL = "https://unitedstates.github.io/congress-legislators/legislators-historical.json"
WIKI_SUMMARY = "https://en.wikipedia.org/api/rest_v1/page/summary/"

SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v", "md", "dr", "phd", "esq"}


def _words(s: str) -> list[str]:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z\s]", " ", s.lower())
    return [w for w in s.split() if w and w not in SUFFIXES]


# The roster only changes when Congress does -- a general election, a special election,
# or an appointment. Re-downloading 15MB weekly to learn nothing is waste, so the cache
# is long-lived and `force` refreshes it on demand. Callers refresh when a name fails to
# match, which is exactly when the roster is actually out of date.
ROSTER_TTL = 180 * 86400


def _roster(cfg: Config, force: bool = False) -> list[dict]:
    """Current legislators plus anyone who left since 2015."""
    cache = cfg.cache_dir / "legislators.json"
    if not force and cache.exists() and time.time() - cache.stat().st_mtime < ROSTER_TTL:
        return json.loads(cache.read_text())
    people = []
    for url, recent_only in ((CURRENT_URL, False), (HISTORICAL_URL, True)):
        r = requests.get(url, timeout=90, headers={"User-Agent": user_agent(cfg)})
        r.raise_for_status()
        for m in r.json():
            terms = m.get("terms") or []
            if not terms:
                continue
            if recent_only and (terms[-1].get("end") or "") < "2015-01-01":
                continue
            t = terms[-1]
            people.append({
                "bioguide": m["id"].get("bioguide", ""),
                "wikipedia": m["id"].get("wikipedia", ""),
                "opensecrets": m["id"].get("opensecrets", ""),
                "first": m["name"].get("first", ""),
                "last": m["name"].get("last", ""),
                "nickname": m["name"].get("nickname", ""),
                "full": m["name"].get("official_full") or
                       f"{m['name'].get('first','')} {m['name'].get('last','')}".strip(),
                "chamber": "Senate" if t.get("type") == "sen" else "House",
                "state": t.get("state", ""),
                "district": t.get("district"),
                "party": t.get("party", ""),
                "url": t.get("url", ""),
                "birthday": (m.get("bio") or {}).get("birthday", ""),
                "current": not recent_only,
            })
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(people))
    return people


def build_index(cfg: Config, force: bool = False) -> dict:
    """Surname -> candidate legislators. Multi-word surnames ('Van Epps') are indexed
    under every trailing-word combination so either spelling style resolves."""
    idx: dict[str, list[dict]] = {}
    for p in _roster(cfg, force=force):
        lw = _words(p["last"])
        if not lw:
            continue
        for i in range(len(lw)):
            idx.setdefault(" ".join(lw[i:]), []).append(p)
    return idx


def match(idx: dict, name: str, chamber: str = "", state: str = "") -> dict | None:
    """Resolve one filing name. `state` is the House's StateDst code ('CA11') or ''."""
    w = _words(name)
    if not w:
        return None
    # try the longest trailing phrase first, so 'van epps' beats 'epps'
    cands: list[dict] = []
    for i in range(len(w)):
        cands = idx.get(" ".join(w[i:]), [])
        if cands:
            break
    if not cands:
        cands = idx.get(w[-1], [])
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0]

    pool = cands
    if state:
        by_state = [c for c in pool if c["state"] == state[:2].upper()]
        if by_state:
            pool = by_state
    if len(pool) > 1 and chamber:
        by_ch = [c for c in pool if c["chamber"] == chamber]
        if by_ch:
            pool = by_ch
    if len(pool) > 1:
        # first name or nickname sharing an initial with the filing's leading word
        first = w[0]
        by_first = [c for c in pool
                    if _words(c["first"])[:1] == [first]
                    or _words(c["nickname"])[:1] == [first]
                    or (_words(c["first"]) and _words(c["first"])[0][0] == first[0])]
        if by_first:
            pool = by_first
    if len(pool) > 1:
        pool = sorted(pool, key=lambda c: not c["current"])  # prefer a sitting member
    return pool[0]


def wiki_summary(cfg: Config, title: str, max_age: float | None = None,
                 _last=[0.0]) -> dict:
    """Wikipedia extract + thumbnail, cached on disk. Returns {} when unavailable.

    Wikimedia rate-limits anonymous callers hard (HTTP 429), so requests are spaced
    and retried with backoff. Failures are never cached -- a 429 today should not
    permanently blank out a member's bio.

    `max_age` (seconds) re-fetches an entry older than that; None trusts any cached
    copy. The daily rotation passes 0 to force a re-check on its slice of members.
    """
    if not title:
        return {}
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", title)[:80]
    cache = cfg.cache_dir / f"wiki-{safe}.json"
    if cache.exists() and (max_age is None
                           or time.time() - cache.stat().st_mtime < max_age):
        try:
            return json.loads(cache.read_text())
        except ValueError:
            pass
    url = WIKI_SUMMARY + requests.utils.quote(title.replace(" ", "_"), safe="")
    for attempt in range(4):
        gap = time.time() - _last[0]
        if gap < 0.6:
            time.sleep(0.6 - gap)
        try:
            r = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": user_agent(cfg)})
            _last[0] = time.time()
        except Exception as e:
            log.warning("wiki %s: %s", title, e)
            return {}
        if r.status_code == 429:
            time.sleep(2 ** attempt)
            continue
        if r.status_code != 200:
            return {}
        j = r.json()
        out = {
            "title": j.get("title", title),
            "extract": (j.get("extract") or "")[:900],
            "description": j.get("description", ""),
            "thumb": ((j.get("thumbnail") or {}).get("source") or ""),
            "url": ((j.get("content_urls") or {}).get("desktop") or {}).get("page", ""),
        }
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(out))
        return out
    log.warning("wiki %s: still rate-limited after retries", title)
    return {}


# Committee jurisdiction -> the sectors it plausibly covers.
#
# EDITORIAL, NOT OFFICIAL. Real jurisdiction is defined by chamber rules and is messier
# than any keyword map. This exists to surface "traded in an industry their committee
# oversees" as a prompt to go look, never as a finding.
#
# Deliberately omitted: Appropriations, Budget, Rules, Ethics, Oversight, Foreign
# Affairs. Their reach is so broad that flagging them would match nearly every trade
# and drown the signal.
COMMITTEE_SECTORS: list[tuple[str, tuple[str, ...]]] = [
    ("agriculture", ("Agriculture", "Food & Beverage")),
    ("armed services", ("Transportation Equipment", "Electronics & Electrical Equipment",
                        "Instruments & Medical Devices")),
    ("energy and commerce", ("Utilities & Power", "Communications", "Pharma & Chemicals",
                             "Software & IT Services", "Healthcare Services")),
    ("energy and natural resources", ("Utilities & Power", "Mining & Energy Extraction",
                                      "Petroleum Refining")),
    ("natural resources", ("Mining & Energy Extraction", "Petroleum Refining",
                           "Utilities & Power")),
    ("commerce, science", ("Communications", "Transportation & Logistics",
                           "Software & IT Services", "Semiconductors")),
    ("financial services", ("Banking & Finance", "Insurance", "Real Estate",
                            "Funds & Holding Companies", "Real Estate (REIT)")),
    ("banking, housing", ("Banking & Finance", "Insurance", "Real Estate",
                          "Funds & Holding Companies", "Real Estate (REIT)")),
    ("health", ("Healthcare Services", "Pharma & Chemicals", "Instruments & Medical Devices")),
    ("transportation and infrastructure", ("Transportation & Logistics",
                                           "Transportation Equipment",
                                           "Construction & Engineering")),
    ("science, space", ("Software & IT Services", "Semiconductors",
                        "Electronics & Electrical Equipment")),
    ("homeland security", ("Software & IT Services", "Professional Services")),
    ("veterans", ("Healthcare Services",)),
    ("intelligence", ("Software & IT Services", "Electronics & Electrical Equipment",
                      "Communications Equipment")),
    ("judiciary", ("Software & IT Services",)),
    ("ways and means", ("Banking & Finance", "Insurance", "Healthcare Services")),
    ("small business", ()),
]


def sectors_for_committee(name: str) -> tuple[str, ...]:
    n = (name or "").lower()
    out: set[str] = set()
    for needle, secs in COMMITTEE_SECTORS:
        if needle in n:
            out.update(secs)
    return tuple(sorted(out))


def committees(cfg: Config, force: bool = False) -> dict[str, list[dict]]:
    """bioguide -> committee/subcommittee assignments, cached like the roster."""
    cache = cfg.cache_dir / "committees.json"
    if not force and cache.exists() and time.time() - cache.stat().st_mtime < ROSTER_TTL:
        return json.loads(cache.read_text())
    hdr = {"User-Agent": user_agent(cfg)}
    coms = requests.get(COMMITTEES_URL, timeout=60, headers=hdr)
    coms.raise_for_status()
    mem = requests.get(MEMBERSHIP_URL, timeout=60, headers=hdr)
    mem.raise_for_status()

    top, subs = {}, {}
    for c in coms.json():
        tid = c.get("thomas_id")
        if not tid:
            continue
        top[tid] = c.get("name", "")
        for sc in c.get("subcommittees", []):
            subs[tid + sc.get("thomas_id", "")] = (c.get("name", ""), sc.get("name", ""))

    out: dict[str, list[dict]] = {}
    for key, people in mem.json().items():
        if key in top:
            name, parent = top[key], ""
        elif key in subs:
            parent, name = subs[key]
        else:
            continue
        for p in people:
            bio = p.get("bioguide")
            if not bio:
                continue
            out.setdefault(bio, []).append({
                "key": key, "name": name, "parent": parent,
                "title": p.get("title", "") or "",
            })
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(out))
    return out
