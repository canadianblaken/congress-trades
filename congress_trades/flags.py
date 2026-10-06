"""One row per member: the accountability facts side by side.

Four flags, each a checkable fact rather than a finding, and each taken from the
module that owns its rule so this page can never disagree with that report:

  late        trades disclosed past the STOCK Act's 45-day deadline (compliance)
  committee   trades at or above the default floor in a sector one of their own
              committees oversees (digest.committee_overlap, jurisdiction table)
  pac         PAC money from committees in the sectors they oversee (finance)
  lone        trades of $100,001+ that no other member made in that ticker within
              30 days either side -- the bet nobody else was in

Deliberately no combined score. Adding a late filing to a PAC dollar to a
committee trade would invent a scale on which they are comparable, and a ranked
"most suspicious" list is a claim this data cannot support. Rows are ordered by
how many flags apply, then by name; every column sorts on its own in the portal.
"""
from __future__ import annotations

import bisect
import datetime as dt
import json
from collections import defaultdict

from . import compliance, db, digest, finance
from .config import CONFIG

LONE_MIN = 100_001
LONE_DAYS = 30


def _lone(rows) -> dict[str, int]:
    """Per member, trades of LONE_MIN+ with no other member in that ticker within
    LONE_DAYS either side."""
    by_ticker = defaultdict(list)                 # ticker -> sorted [(ordinal, member)]
    for r in rows:
        d = r.get("d0") or ""
        try:
            by_ticker[r["ticker"]].append((dt.date.fromisoformat(d[:10]).toordinal(), r["member"]))
        except ValueError:
            continue
    for v in by_ticker.values():
        v.sort()
    out: dict[str, int] = defaultdict(int)
    for r in rows:
        if (r["amount_min"] or 0) < LONE_MIN:
            continue
        try:
            day = dt.date.fromisoformat((r.get("d0") or "")[:10]).toordinal()
        except ValueError:
            continue
        v = by_ticker[r["ticker"]]
        lo, hi = bisect.bisect_left(v, (day - LONE_DAYS, "")), bisect.bisect_right(v, (day + LONE_DAYS, "￿"))
        if all(m == r["member"] for _, m in v[lo:hi]):
            out[r["member"]] += 1
    return out


def build(cfg=CONFIG) -> dict:
    floor = cfg.default_floor
    rows, members, seats, sectors, _, _, _ = digest.load(36500, floor, cfg)
    for r in rows:
        r["d0"] = r.get("disclosed") or r.get("tx_date") or ""
    comm = defaultdict(lambda: [0, 0])
    for o in digest.committee_overlap(rows, members, seats, sectors, limit=None):
        c = comm[o["r"]["member"]]
        c[0] += 1
        c[1] += o["r"]["amount_min"] or 0
    lone = _lone(rows)
    late = compliance.build(cfg).get("members") or {}
    pac = {o["bioguide"]: o for o in finance.run(None, cfg).get("overlaps") or []}

    out = []
    for name in sorted(set(late) | set(comm) | set(lone)):
        p = members.get(name) or {}
        lt = late.get(name) or {}
        pc = pac.get(p.get("bioguide") or "") or {}
        row = {"member": name, "full_name": p.get("full_name") or name,
               "party": (p.get("party") or "")[:1], "chamber": p.get("chamber") or lt.get("chamber") or "",
               "trades": lt.get("total_trades") or 0,
               "late": lt.get("late_count") or 0, "late_share": lt.get("late_share") or 0,
               "committee": comm[name][0], "committee_dollars": comm[name][1],
               "pac_dollars": pc.get("pac_dollars") or 0,
               "pac_sectors": pc.get("sectors") or [],
               "lone": lone.get(name, 0)}
        row["flags"] = sum(bool(row[k]) for k in ("late", "committee", "pac_dollars", "lone"))
        out.append(row)
    out.sort(key=lambda r: (-r["flags"], r["full_name"]))
    return {"floor": floor, "lone_min": LONE_MIN, "lone_days": LONE_DAYS,
            "deadline": compliance.STATUTORY_DAYS, "members": out}


def to_markdown(d: dict) -> str:
    m = digest.money
    L = ["# Flags: the accountability facts, per member", "",
         "Each column is a checkable fact from its own report, not a finding. "
         "There is no combined score: these are not on a common scale, and a "
         "ranked list of the most suspicious would claim more than the data can. "
         "Rows are ordered by how many of the four apply, and a flag applies on a "
         "single instance: one late filing in a hundred counts the same as fifty. "
         "Read the numbers in each column, not the count.", "",
         f"- **late**: trades disclosed more than {d['deadline']} days after the trade "
         "(Compliance report).",
         f"- **committee**: trades of {m(d['floor'])}+ in a sector one of the member's "
         "committees oversees (the jurisdiction table is editorial, generated once by "
         "a model and reviewed).",
         "- **PAC $**: money from PACs in the sectors their committees oversee "
         "(PAC money report; the PAC-to-sector match is a keyword heuristic).",
         f"- **lone**: trades of {m(d['lone_min'])}+ that no other member made in that "
         f"ticker within {d['lone_days']} days either side.", "",
         "| member | party | flags | trades | late | committee | PAC $ | lone |",
         "|---|---|--:|--:|--:|--:|--:|--:|"]
    for r in d["members"]:
        if not r["flags"]:
            continue
        L.append(f"| {r['full_name']} | {r['party']} {r['chamber'][:1]} | {r['flags']} | "
                 f"{r['trades']} | {r['late'] or ''}{f' ({round(100*r['late_share'])}%)' if r['late'] else ''} | "
                 f"{r['committee'] or ''}{f' ({m(r['committee_dollars'])})' if r['committee'] else ''} | "
                 f"{m(r['pac_dollars']) if r['pac_dollars'] else ''} | {r['lone'] or ''} |")
    n0 = sum(1 for r in d["members"] if not r["flags"])
    L += ["", f"{n0} member(s) with trades raise none of the four."]
    return "\n".join(L) + "\n"


def selftest(cfg=CONFIG):
    d = build(cfg)
    rows = d["members"]
    assert rows and all(0 <= r["flags"] <= 4 for r in rows)
    assert [r["flags"] for r in rows] == sorted((r["flags"] for r in rows), reverse=True)
    late = compliance.build(cfg)["members"]
    for r in rows[:20]:
        assert r["late"] == (late.get(r["member"]) or {}).get("late_count", 0), r["member"]
    # the lone rule: a trade beside another member's within the window is not lone
    fake = [{"ticker": "X", "member": "A", "amount_min": 250001, "d0": "2025-01-10"},
            {"ticker": "X", "member": "B", "amount_min": 1001, "d0": "2025-02-05"},
            {"ticker": "Y", "member": "A", "amount_min": 250001, "d0": "2025-01-10"},
            {"ticker": "Y", "member": "B", "amount_min": 1001, "d0": "2025-03-01"}]
    assert dict(_lone(fake)) == {"A": 1}, dict(_lone(fake))
    print(f"selftest ok: {len(rows)} members, {sum(1 for r in rows if r['flags'])} with a flag, "
          f"late counts match compliance, lone rule respects the {LONE_DAYS}-day window")


if __name__ == "__main__":
    print(json.dumps(build(), indent=2, default=str)[:2000])
