#!/usr/bin/env python3
"""Checks for the three passes that audit something rather than produce it:
the committee-jurisdiction table, the advise brief's own audit, and the parser
regression net.

All three ask a model something and then refuse to take its word for it, so what
is tested here is the refusing. No model is called and no network is touched.

  python3 tests/test_checks.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from congress_trades import (advise, collect, jurisdiction, parserqa,
                             scorecard, sectors)

# ------------------------------------------------- committee jurisdiction rules
# The deliberate omission: these reach every industry, so tagging them would
# flag nearly every trade. It is a rule applied to the answer, not a request in
# a prompt, and it holds however confident the model was.
for key in ("HSAP", "HSAP01", "SSAP19", "HSBU", "HSRU", "HSSO", "HSHA", "HSGO",
            "HSFA", "SSFR12"):
    got, note = jurisdiction.apply_rules(key, ["Agriculture", "Banking & Finance"])
    assert got == (), f"{key} must carry no jurisdiction, got {got}"
    assert "reaches every industry" in note, note

# A body outside that set keeps what it was given.
assert jurisdiction.apply_rules("HSAG14", ["Agriculture"])[0] == ("Agriculture",)

# Low confidence is the enum-collapse failure this table was built around: a
# model that cannot map its reasoning onto the vocabulary emits VOCAB's first
# value, which is "Agriculture". Every low-confidence tag in the generating run
# was one of those, and none of them was correct.
assert sectors.VOCAB[0] == "Agriculture", "the collapse target moved; re-read the rule"
got, note = jurisdiction.apply_rules("HSAS", ["Agriculture"], "low")
assert got == () and "low confidence" in note, note
assert jurisdiction.apply_rules("HSAS28", ["Transportation Equipment"], "medium")[0] \
    == ("Transportation Equipment",), "medium is an opinion, not a failure"
assert jurisdiction.apply_rules("HSAS", [], "low") == ((), ""), \
    "an empty answer is not a dropped one"

# "Unclassified" marks a ticker whose SIC lookup failed, not a jurisdiction.
got, note = jurisdiction.apply_rules("HSIF14", ["Healthcare Services", "Unclassified"])
assert got == ("Healthcare Services",) and "Unclassified" in note, (got, note)
# Anything outside the vocabulary cannot survive, whatever the schema allowed.
assert jurisdiction.apply_rules("HSIF14", ["Not A Sector"])[0] == ()
# A wildcard is trimmed rather than trusted.
wide = list(sectors.VOCAB)[:jurisdiction.MAX_SECTORS + 4]
got, note = jurisdiction.apply_rules("HSSY01", wide)
assert len(got) == jurisdiction.MAX_SECTORS and "trimmed" in note, (got, note)
# Duplicates collapse instead of eating the cap.
assert jurisdiction.apply_rules("HSAG14", ["Agriculture"] * 3)[0] == ("Agriculture",)

# Reviewed corrections beat the model and survive regeneration.
for key, (fixed, why) in jurisdiction.OVERRIDES.items():
    assert why, f"{key} was corrected with no reason recorded"
    assert set(fixed) <= set(sectors.VOCAB), f"{key} corrects to a non-sector"
    assert jurisdiction.apply_rules(key, ["Agriculture"], "high")[0] == fixed

# ------------------------------------------------- the committed table itself
table = jurisdiction.load()
assert table, "seed/committee_sectors.json is missing or empty"
for key, rec in table.items():
    assert set(rec["sectors"]) <= set(sectors.VOCAB), f"{key} carries a non-sector"
    assert "Unclassified" not in rec["sectors"], key
    assert len(rec["sectors"]) <= jurisdiction.MAX_SECTORS, key
    if key[:4] in jurisdiction.BROAD:
        assert not rec["sectors"], f"{key} is broad and was committed with a tag"

# A seat is looked up by committee id, never by name: three different committees
# have a subcommittee called "Health", and a substring test cannot tell them
# apart. Same name, different parents, different answers.
health = [k for k, r in table.items() if r.get("name") == "Health"]
assert len(health) > 1, "expected several subcommittees named Health"
assert len({tuple(table[k]["sectors"]) for k in health}) > 1, \
    "the whole point is that identically named subcommittees differ"

# An id the table has never seen falls back to its parent, not to nothing.
parent = next(k for k, r in table.items() if len(k) == 4 and r["sectors"])
assert jurisdiction.sectors_for_seat({"key": parent + "77"}) == \
    tuple(table[parent]["sectors"]), "a new subcommittee must inherit its parent"
assert jurisdiction.sectors_for_seat({"key": "ZZZZ01"}) == ()
assert jurisdiction.sectors_for_seat({}) == ()
# ...and a new subcommittee of a broad committee still carries nothing.
broad = next(iter(jurisdiction.BROAD))
assert jurisdiction.sectors_for_seat({"key": broad + "77"}) == ()

enum = jurisdiction.schema()["properties"]["items"]["items"]["properties"][
    "sectors"]["items"]["enum"]
assert enum == list(sectors.VOCAB), "the schema enum drifted from sectors.VOCAB"

# --------------------------------------------------------- the advise audit
SRC = ("| AAPL | 4 | +2 | $1.2M | Software & IT Services | +6.1% |\n"
       "| NVDA | 3 | -1 | $250k | Semiconductors | -2.4% |\n"
       "r = -0.06 across 91 members")

# A symbol that is not in the digest was supplied by the model, not the data.
assert advise.stray_tickers("We like AAPL and NVDA.", SRC) == []
assert advise.stray_tickers("Also TSLA looks strong.", SRC) == ["TSLA"]
assert advise.stray_tickers("TSLA, and TSLA again", SRC) == ["TSLA"], "report once"
# Prose must not read as symbols, or a reader learns to skim the findings.
assert advise.stray_tickers(
    "The US SEC says an ETF vs SPY is OK for AI and IT, per the CEO.", SRC) == []
assert advise.stray_tickers("apple is not a ticker", SRC) == []
# This is the failure the pass actually caught on a live brief: a real symbol
# given a fund name that is not its own.
assert "MBS" in advise.stray_tickers("WMB (iShares Core MBS ETF) was sold.", SRC)

# Figures are compared after normalising sign, commas and trailing zeros.
assert advise.stray_numbers("up 6.1% on $1.2M", SRC) == []
assert advise.stray_numbers("up +6.10%", SRC) == []
assert advise.stray_numbers("returned 41%", SRC) == ["41%"]
assert advise.stray_numbers("worth $1,200,000", SRC) == ["$1,200,000"]

# The gate that stops the audit manufacturing what it exists to catch.
DRAFT = "Members bought AAPL heavily this quarter, which is a strong signal."
assert advise._appears("bought AAPL heavily", DRAFT)
assert advise._appears("bought   AAPL\n  heavily", DRAFT), "whitespace is forgiven"
assert not advise._appears("bought TSLA heavily", DRAFT), "a fabricated quote fails"
assert not advise._appears("AAPL", DRAFT), "too short to locate is not a quote"
assert advise.check_schema()["properties"]["findings"]["items"]["properties"][
    "kind"]["enum"] == list(advise.KINDS)

# ------------------------------------------------------------- the parser net
# A page break dropped a repeated column header into the middle of a block, so
# the block holds two transactions and the parser returns one. Real text, from
# filing 20030803.
SPLIT = """
           BITMINE IMMERSIN TECH INC            S          07/28/2025 07/28/2025   $50,001 -
           (BMNR) [ST]                                                             $100,000
           F       S    : New
ID   Owner Asset                                Transaction Date      Notification Amount
           BITMINE IMMERSIN TECH INC            P          07/16/2025 07/16/2025   $1,001 - $15,000
           (BMNR) [ST]
"""
sc = parserqa.scan_one(SPLIT)
assert (sc["n_pattern"], sc["n_parsed"], sc["unaccounted"]) == (2, 2, 0), sc

# The verification gates are tested against a parser that DID miss a row, which
# is what a regression looks like: the scan found two, the parser returned one.
rows = sc["rows"][:1]
ok, why = parserqa.verify({"tx_date": "07/16/2025", "tx_type": "P", "amount": "",
                           "asset": "BITMINE IMMERSIN TECH INC"}, SPLIT, rows)
assert ok and "corroborated" in why, why
# The row the parser DID return is not a miss, however differently it is spelled.
ok, why = parserqa.verify({"tx_date": "07/28/2025", "tx_type": "S", "amount": "",
                           "asset": "Bitmine Immersion Technologies"}, SPLIT, rows)
assert not ok and "already parsed" in why, why
# A date the document never carries.
assert not parserqa.verify({"tx_date": "01/02/2020", "tx_type": "P", "amount": "",
                            "asset": "BITMINE IMMERSIN"}, SPLIT, rows)[0]
# A real date with a company the document never names: the invention gate.
ok, why = parserqa.verify({"tx_date": "07/16/2025", "tx_type": "P", "amount": "",
                           "asset": "Nonesuch Holdings Inc"}, SPLIT, rows)
assert not ok and "appears in the document" in why, why
# Not a date, and an asset with nothing identifying in it.
assert not parserqa.verify({"tx_date": "soon", "tx_type": "P", "amount": "",
                            "asset": "BITMINE"}, SPLIT, rows)[0]
assert not parserqa.verify({"tx_date": "07/16/2025", "tx_type": "P", "amount": "",
                            "asset": "a - b"}, SPLIT, rows)[0]

# A clean filing must not look like a broken one.
CLEAN = """
           SP          UnitedHealth Group Incorporated    P     04/10/2025 05/15/2025   $1,001 - $15,000
                       Common Stock (UNH) [ST]
"""
assert parserqa.scan_one(CLEAN)["unaccounted"] == 0

# The scan counts with the parser's own pattern, so it is blind to anything that
# pattern never matches -- which is why the model pass exists, and how type E
# (exchange) was found. Filing 20035106, where a model saw it first: the parser
# returned one row and the document held two.
EXCHANGE = """
           JT          King Cnty Wash 4.00% 12/01/32 [GS] E     07/26/2026 08/01/2026   $15,001 -
                                                                                        $50,000
"""
ex = parserqa.scan_one(EXCHANGE)
assert ex["n_parsed"] == 1 and ex["unaccounted"] == 0, ex
assert ex["rows"][0]["tx_type"] == "exchange", ex["rows"][0]
assert ex["rows"][0]["amount_min"] == 15001, ex["rows"][0]

# An exchange expresses no view, so it must not score as one. This read
# `if sell else buy` and silently graded every exchange as a purchase.
assert scorecard.alpha("buy", 0.10, 0.04) is not None
assert scorecard.alpha("sell", 0.10, 0.04) is not None
assert scorecard.alpha("exchange", 0.10, 0.04) is None, "an exchange has no direction"
assert scorecard.alpha("", 0.10, 0.04) is None

# Two transactions in one block, because a page break reprinted the column
# header between them with no blank line. Both must come back, with their own
# amounts rather than each other's.
both = collect.parse_house_ptr(SPLIT)
assert len(both) == 2, both
assert [r["tx_type"] for r in both] == ["sell", "buy"], both
assert [r["amount_min"] for r in both] == [50001, 1001], both
assert all(r["ticker"] == "BMNR" for r in both), both

# The asset sits on the type letter's line, so the LAST line before the match is
# the name. Reading the first line instead picked up the previous transaction's
# trailing "Filing Status: New", mangled by pdftotext into "F S : New", as the
# asset name of 26 real trades.
WRAPPED = """
                      F      S     : New
                      S           O : Merrill Lynch Tax Efficient Core
ID   Owner Asset                                   Transaction Date      Notification Amount
     JT    BorgWarner Inc. Common Stock            P          02/12/2025 03/06/2025   $1,001 - $15,000
           (BWA) [ST]
"""
w = collect.parse_house_ptr(WRAPPED)
assert len(w) == 1, w
assert w[0]["asset_name"] == "BorgWarner Inc. Common Stock", w[0]
assert w[0]["owner"] == "Joint" and w[0]["ticker"] == "BWA", w[0]

assert parserqa.schema()["properties"]["missing"]["items"]["properties"][
    "tx_type"]["enum"] == ["P", "S"]
assert json.dumps(parserqa.schema()) and json.dumps(jurisdiction.schema())
assert json.dumps(advise.check_schema())

print(f"ok: {len(table)} committees committed, jurisdiction/confidence/override "
      f"rules, {len(advise.KINDS)} audit kinds with a quote gate, parser scan "
      "and 6 verification gates")
