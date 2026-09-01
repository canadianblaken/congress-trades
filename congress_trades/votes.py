#!/usr/bin/env python3
"""
Party-unity scores and DW-NOMINATE positions from Voteview.

Voteview publishes every recorded roll call as bulk CSV, free and key-less. Its vote
files key on ICPSR ids, while everything else here keys on bioguide -- the members
file carries both, so it bridges the two.

Party unity = share of a member's yea/nay votes that matched their own party's
majority on that roll call. Computed here rather than taken from anywhere, because
the free sources publish the raw votes, not the score.

NOT computed: "votes with the president". That is CQ's proprietary presidential
support score; there is no free equivalent, and inventing one would be worse than
omitting it.
"""
from __future__ import annotations

import csv
import io
import logging
import time
from collections import defaultdict

import requests

from .config import Config, user_agent

log = logging.getLogger("congress_trades.votes")
BASE = "https://voteview.com/static/data/out"
MEMBERS_URL = f"{BASE}/members/HSall_members.csv"
VOTES_URL = BASE + "/votes/{chamber}{congress}_votes.csv"

YEA, NAY = "1", "6"          # voteview cast codes; 7=present, 9=absent, 0=non-member
CACHE_TTL = 7 * 86400


def _fetch(cfg: Config, url: str, name: str) -> list[dict]:
    cache = cfg.cache_dir / name
    if cache.exists() and time.time() - cache.stat().st_mtime < CACHE_TTL:
        text = cache.read_text(encoding="utf-8", errors="ignore")
    else:
        r = requests.get(url, timeout=180, headers={"User-Agent": user_agent(cfg)})
        r.raise_for_status()
        text = r.text
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(text, encoding="utf-8")
    return list(csv.DictReader(io.StringIO(text)))


def unity_scores(cfg: Config, congress: int) -> tuple[dict[str, dict], dict[str, dict]]:
    """Returns (by_bioguide, meta). Party unity is computed per chamber."""
    members = _fetch(cfg, MEMBERS_URL, "vv_members.csv")
    roster = {}
    for m in members:
        if m["congress"] != str(congress) or not m.get("bioguide_id"):
            continue
        roster[m["icpsr"]] = {
            "bioguide": m["bioguide_id"],
            "party": m["party_code"],
            "nominate": m.get("nominate_dim1") or "",
        }

    out: dict[str, dict] = {}
    for chamber, code in (("H", "House"), ("S", "Senate")):
        rows = _fetch(cfg, VOTES_URL.format(chamber=chamber, congress=congress),
                      f"vv_{chamber}{congress}_votes.csv")
        # party majority position per roll call
        tally: dict[tuple, dict[str, int]] = defaultdict(lambda: {YEA: 0, NAY: 0})
        for r in rows:
            who = roster.get(r["icpsr"])
            if not who or r["cast_code"] not in (YEA, NAY):
                continue
            tally[(r["rollnumber"], who["party"])][r["cast_code"]] += 1
        majority = {k: (YEA if v[YEA] >= v[NAY] else NAY) for k, v in tally.items()}

        agree: dict[str, list[int]] = defaultdict(lambda: [0, 0])   # [with_party, total]
        for r in rows:
            who = roster.get(r["icpsr"])
            if not who or r["cast_code"] not in (YEA, NAY):
                continue
            maj = majority.get((r["rollnumber"], who["party"]))
            if maj is None:
                continue
            a = agree[who["bioguide"]]
            a[1] += 1
            if r["cast_code"] == maj:
                a[0] += 1

        for bio, (with_party, total) in agree.items():
            if total < 20:          # too few votes to characterise
                continue
            nom = next((v["nominate"] for v in roster.values() if v["bioguide"] == bio), "")
            out[bio] = {
                "party_unity": round(with_party / total * 100, 1),
                "votes_cast": total,
                "nominate": float(nom) if nom not in ("", None) else None,
                "chamber": code,
            }
    return out, {"congress": congress}
