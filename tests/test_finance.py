#!/usr/bin/env python3
"""Checks for the two things that break silently in finance.py: FEC's field order
(no header row in any bulk file, so an off-by-one index parses garbage without
erroring) and the identity matcher (a wrong match is worse than a missed one).

Fixture lines below are real rows pulled from the FEC's own 2024-cycle bulk
files (cn.txt, cm.txt, itpas2.txt) on 2026-09-16, trimmed to the file they came
from -- not fabricated.

  python3 tests/test_finance.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from congress_trades.finance import _iso, match_candidate, sector_for_committee

# --- cn.txt: candidate master, real rows -------------------------------------------
# Mitch McConnell (Senate, KY) and Adam Schiff (Senate, CA -- he moved from the House),
# plus two rows that must NOT match: Schiff's old House record (this roster only has
# him as a senator) and an unrelated Mindy McConnell running for the House in LA.
MCCONNELL_S = "S2KY00012|MCCONNELL, MITCH|REP|2026|KY|S|00|I|C|C00193342|2318 DUNDEE ROAD||LOUISVILLE|KY|40205"
SCHIFF_S = "S4CA00555|SCHIFF, ADAM|DEM|2024|CA|S|00|O|C|C00343871|611 PENNSYLVANIA AVE SE|#143|WASHINGTON|DC|20003"
SCHIFF_H = "H0CA27085|SCHIFF, ADAM|DEM|2024|CA|H|30|I|N|C00343871|777 S. FIGUEROA STREET, SUITE 4050||LOS ANGELES|CA|90017"
MINDY_H = "H2LA02198|MCCONNELL, MINDY|LIB|2021|LA|H|02|O|N|C00767145|7000 MILNE BLVD||NEW ORLEANS|LA|70124"

# What _index_members(conn) would build from congress_members for these two people.
IDX = {
    ("mcconnell", "KY", "Senate"): [{"bioguide": "M000355", "first": "mitch", "district": "0"}],
    ("schiff", "CA", "Senate"): [{"bioguide": "S001150", "first": "adam", "district": "0"}],
}


def _cn_fields(line):
    f = line.split("|")
    assert len(f) >= 7, f"cn.txt row too short: {f}"
    cand_id, cand_name, party, yr, state, office, district = f[:7]
    return cand_name, office, state, district


name, office, state, district = _cn_fields(MCCONNELL_S)
b, reason = match_candidate(IDX, name, office, state, district)
assert (b, reason) == ("M000355", "matched"), (b, reason)

name, office, state, district = _cn_fields(SCHIFF_S)
b, reason = match_candidate(IDX, name, office, state, district)
assert (b, reason) == ("S001150", "matched"), (b, reason)

# Same person, wrong chamber for this roster: must NOT match, not fall back to a guess.
name, office, state, district = _cn_fields(SCHIFF_H)
b, reason = match_candidate(IDX, name, office, state, district)
assert b is None, "Schiff's old House record matched -- should require chamber+state agreement"

# Same surname, different person entirely (different state, different chamber): no match.
name, office, state, district = _cn_fields(MINDY_H)
b, reason = match_candidate(IDX, name, office, state, district)
assert b is None, "unrelated same-surname candidate matched"

# --- ambiguity and district: constructed, since real same-district collisions are rare ---
IDX_AMBIG = {("smith", "TX", "House"): [
    {"bioguide": "A000001", "first": "michael", "district": "5"},
    {"bioguide": "A000002", "first": "mark", "district": "5"}]}
b, reason = match_candidate(IDX_AMBIG, "SMITH, MARCUS", "H", "TX", "05")
assert b is None and reason == "ambiguous", (b, reason)

IDX_DIST = {("jones", "OH", "House"): [{"bioguide": "J000001", "first": "pat", "district": "3"}]}
b, reason = match_candidate(IDX_DIST, "JONES, PAT", "H", "OH", "07")
assert b is None and reason == "district-mismatch", (b, reason)
# the same candidate, right district (FEC zero-pads; district match must tolerate it)
b, reason = match_candidate(IDX_DIST, "JONES, PAT", "H", "OH", "03")
assert b == "J000001", (b, reason)

# --- cm.txt: committee master, real rows -------------------------------------------
REALTORS = "C00030718|NATIONAL ASSOCIATION OF REALTORS POLITICAL ACTION COMMITTEE|SANFORD, CRAIG W MR.|430 NORTH MICHIGAN AVENUE||CHICAGO|IL|606114011|B|Q|UNK|M|M|NATIONAL ASSOCIATION OF REALTORS|"
OCCIDENTAL = "C00083857|OCCIDENTAL PETROLEUM CORPORATION POLITICAL ACTION COMMITTEE|FORTHUBER, FRED|1701 PENNSYLVANIA AVE NW|SUITE 800|WASHINGTON|DC|20006|B|Q||M|C|OCCIDENTAL PETROLEUM CORPORATION|"
HALLMARK = "C00000059|HALLMARK CARDS, INC. PAC (HALLPAC)|KLEIN, CASSIE MS.|2501 MCGEE, MD853||KANSAS CITY|MO|64108|B|Q|UNK|M|C|HALLMARK CARDS, INC.|"

for line in (REALTORS, OCCIDENTAL, HALLMARK):
    f = line.split("|")
    assert len(f) == 15, f"cm.txt field count drifted: {len(f)} in {f}"

f = REALTORS.split("|")
assert sector_for_committee(f[1], f[13]) == "Real Estate", f[1]
f = OCCIDENTAL.split("|")
assert sector_for_committee(f[1], f[13]) == "Petroleum Refining", f[1]
f = HALLMARK.split("|")
assert sector_for_committee(f[1], f[13]) == "", \
    "a greeting-card PAC should get no sector label, not a guessed one"

# --- itpas2.txt: PAC -> candidate contribution, a real McConnell-committee row -----
PAS2 = ("C00001388|N|M3|P2026|202303149579012694|24K|CCM|MCCONNELL SENATE COMMITTEE '08"
        "|LOUISVILLE|KY|40201|||02092023|2500|C00193342|S2KY00012|SB23.55864|1692949|||"
        "4031420231734386566")
f = PAS2.split("|")
assert len(f) == 22, f"itpas2.txt field count drifted: {len(f)}"
cmte_id, tx_tp, tx_dt, tx_amt, cand_id = f[0], f[5], f[13], f[14], f[16]
assert cmte_id == "C00001388" and tx_tp == "24K" and cand_id == "S2KY00012"
assert tx_amt == "2500"
assert _iso(tx_dt) == "2023-02-09", _iso(tx_dt)
assert _iso("not-a-date") == ""
assert _iso("1332023") == ""          # 7 digits -- malformed, must not half-parse

print("ok: FEC row parsing (cn/cm/itpas2 field order) and the conservative identity matcher")
