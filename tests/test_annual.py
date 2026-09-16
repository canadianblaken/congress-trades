#!/usr/bin/env python3
"""Self-checks for the annual FDR schedule parsers -- real `pdftotext -layout`
output from actual House filings, trimmed. Real DocIDs are noted per fixture
so a failure can be checked against the source PDF.

  python3 tests/test_annual.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from congress_trades.annual import (
    parse_assets, parse_earned_income, parse_fdr, parse_liabilities,
    parse_positions, _sections,
)

# --- Schedule D: real liability rows from filing 10073339, trimmed --------
# Owner code (JT), a wrapped amount bracket, and a "C: <comment>" annotation
# line that must be dropped rather than corrupting the next row.
LIABILITIES = """
Owner Creditor                                       Date Incurred       Type                         Amount of
                                                                                                      Liability

JT         Metro Credit Union                        2018                Mortgage primary residence   $500,001 -
                                                                                                      $1,000,000
           C          : Paid in full August 2025


JT         Bank of America                           September 2025      Mortgage primary residence   $1,000,001 -
                                                                                                      $5,000,000
"""
liab = parse_liabilities(LIABILITIES)
assert len(liab) == 2, f"expected 2 liabilities, got {len(liab)}: {liab}"
assert [r["owner"] for r in liab] == ["Joint", "Joint"], liab
assert [r["creditor"] for r in liab] == ["Metro Credit Union", "Bank of America"], liab
assert liab[0]["date_incurred"] == "2018" and liab[1]["date_incurred"] == "September 2025", liab
assert liab[0]["liability_type"] == "Mortgage primary residence", liab[0]
# The comment line must not have leaked into the type or amount.
assert "Paid in full" not in liab[0]["liability_type"], liab[0]
assert liab[0]["amount_min"] == 500001, liab[0]
assert liab[0]["amount_range"] == "$500,001 - $1,000,000", liab[0]
assert liab[1]["amount_min"] == 1000001, liab[1]

# --- Schedule D: a Type column so long it pushes both Type and Amount onto a
# second physical line (filing 10078029) -- the case column-position matching
# exists for, not just line order. No owner code: implicit "Self".
LIABILITIES_WRAP = """
Owner Creditor                                            Date Incurred           Type                                               Amount of
                                                                                                                                     Liability

           Wells Fargo Bank                               August 2008             Mortgage on Rental Property, Hermosa               $250,001 -
                                                                                  Beach, CA.                                         $500,000
"""
lw = parse_liabilities(LIABILITIES_WRAP)
assert len(lw) == 1, lw
assert lw[0]["owner"] == "Self", lw[0]
assert lw[0]["creditor"] == "Wells Fargo Bank", lw[0]
assert lw[0]["liability_type"] == "Mortgage on Rental Property, Hermosa Beach, CA.", lw[0]
assert lw[0]["amount_min"] == 250001 and lw[0]["amount_range"] == "$250,001 - $500,000", lw[0]

# --- Schedule C: real earned-income rows from filing 10074914-style layout,
# plus a Type column wrapped onto a second line (filing 10075533) and the
# "N/A" amount the STOCK Act allows for a spouse's exact figure. ------------
EARNED_INCOME = """
Source                                                                        Type                                                   Amount

Charles Schwab                                                                RMD - Required Minimum Distribution                    $82,887.00

Charles Schwab                                                                RMD - Required Minimum Distribution -                  N/A
                                                                              Spouse
"""
inc = parse_earned_income(EARNED_INCOME)
assert len(inc) == 2, f"expected 2 earned-income rows, got {len(inc)}: {inc}"
assert inc[0]["source"] == "Charles Schwab" == inc[1]["source"], inc
assert inc[0]["income_type"] == "RMD - Required Minimum Distribution", inc[0]
assert inc[0]["amount"] == "$82,887.00" and inc[0]["amount_val"] == 82887, inc[0]
# The wrapped "Spouse" must land back on the Type column, not get dropped.
assert inc[1]["income_type"] == "RMD - Required Minimum Distribution - Spouse", inc[1]
assert inc[1]["amount"] == "N/A" and inc[1]["amount_val"] is None, inc[1]

assert parse_earned_income("None disclosed.\n") == []

# --- Schedule A: real asset rows from filing 10075331 --------------------
# Covers: a same-line value+income range pair, "None" income with no amount
# cell at all, and the hard case -- a linked sub-asset ("214 Wyoming Ave LLC
# => / Wyoming Office Building") where BOTH the value and income brackets
# wrap onto the continuation line at once, so their open halves ("$250,001 -"
# and "$5,001 -") sit side by side on line one and must be paired with the
# right half on line two by column, not by reading order.
ASSETS = """
Asset                                                        Owner Value of Asset          Income Type(s) Income                 Tx. >
                                                                                                                                 $1,000?

214 Wyoming Ave LLC ⇒                                                  $250,001 -          Rent                $5,001 -
Wyoming Office Building [RP]                                           $500,000                                $15,000

L         : Wyoming, PA, US


Etrade Brokerage Account ⇒                                             $1,001 - $15,000    Dividends           $1 - $200
Delta Air Lines, Inc. Common Stock (DAL) [ST]


Etrade Brokerage Account ⇒                                             $1,001 - $15,000    None
DexCom, Inc. - Common Stock (DXCM) [ST]
"""
assets = parse_assets(ASSETS)
assert len(assets) == 3, f"expected 3 assets, got {len(assets)}: {assets}"

wyo = assets[0]
assert wyo["asset_name"] == "214 Wyoming Ave LLC Wyoming Office Building [RP]", wyo
assert wyo["value_min"] == 250001 and wyo["value_range"] == "$250,001 - $500,000", wyo
assert wyo["income_type"] == "Rent", wyo
assert wyo["income_min"] == 5001 and wyo["income_range"] == "$5,001 - $15,000", wyo

delta = assets[1]
assert "Delta Air Lines" in delta["asset_name"], delta
assert delta["value_min"] == 1001 and delta["value_range"] == "$1,001 - $15,000", delta
assert delta["income_type"] == "Dividends", delta
assert delta["income_min"] == 1 and delta["income_range"] == "$1 - $200", delta

dexcom = assets[2]
assert dexcom["income_type"] == "None", dexcom
assert dexcom["income_min"] is None and dexcom["income_range"] == "", dexcom
assert dexcom["value_min"] == 1001, dexcom

# --- Schedule A: "Undetermined" value (a defined-benefit pension, filing
# 10076619) -- never priced as $0, never invented as a number. -------------
UNDETERMINED = """
Asset                                                   Owner Value of Asset          Income Type(s) Income                      Tx. >
                                                                                                                                 $1,000?

State of Arizona Pension [PE]                                JT         Undetermined           Tax-Deferred
"""
pension = parse_assets(UNDETERMINED)
assert len(pension) == 1, pension
assert pension[0]["owner"] == "Joint", pension[0]
assert pension[0]["value_min"] is None, pension[0]
assert pension[0]["value_range"] == "Undetermined", pension[0]
assert pension[0]["income_type"] == "Tax-Deferred", pension[0]

# A short value bracket can leave only one space before the next column
# starts ("$50,000 Dividends"), which the column splitter would otherwise
# swallow whole since it only breaks on runs of 2+ spaces.
TIGHT_SPACING = """
Asset                                                                   Owner Value of Asset              Income Type(s) Income      Tx. >
                                                                                                                                     $1,000?

Haven at P83 LLC ⇒                                                     $15,001 - $50,000 Dividends             $2,501 -
Haven at P83 Property [RP]                                                                                     $5,000
"""
tight = parse_assets(TIGHT_SPACING)
assert len(tight) == 1, tight
assert tight[0]["value_range"] == "$15,001 - $50,000", tight[0]
assert tight[0]["income_type"] == "Dividends", tight[0]
assert tight[0]["income_range"] == "$2,501 - $5,000", tight[0]

# The page-footer disclaimer that follows the last real Schedule A row must
# not be mistaken for an asset -- it has no value bracket at all.
FOOTER = ASSETS + (
    "\n\n* For the complete list of asset type abbreviations, please visit "
    "https://fd.house.gov/reference/asset-type-codes.aspx.\n")
assert len(parse_assets(FOOTER)) == 3, "the footer note was parsed as an asset"

# --- Schedule E: real positions from filing 10075331, including a "C:"
# comment line and a page-break header reprint with no blank line around it
# (both must be stripped, not treated as a new/garbled record). ------------
POSITIONS = """
Position                                               Name of Organization

Limited Partner                                        Haven at Town Center LLC

Managing Member                                        KUHCON Holdings LLC

Trustee                                                Rhoda M Kuharchik 2021 Irrev Trust FBO Dodie Bresnahan
C          : No longer held.
\x0cPosition                                         Name of Organization
Limited Partner                                  WHC MF Fund 1 LLC

Managing Member                                  TomVest LLC
"""
pos = parse_positions(POSITIONS)
assert len(pos) == 5, f"expected 5 positions, got {len(pos)}: {pos}"
assert pos[0] == {"position": "Limited Partner", "organization": "Haven at Town Center LLC"}, pos[0]
assert pos[2]["organization"] == "Rhoda M Kuharchik 2021 Irrev Trust FBO Dodie Bresnahan", pos[2]
assert "No longer held" not in pos[2]["position"], pos[2]
assert pos[3] == {"position": "Limited Partner", "organization": "WHC MF Fund 1 LLC"}, pos[3]

assert parse_positions("None disclosed.\n") == []

# --- Section splitting: the garbled schedule-title bug ("Schedule D:
# Liabilities" renders as "S D: L") must still key correctly off the letter,
# and a `\f` page break glued directly onto the next header must not hide it.
DOC = f"""
S                  C: E                 I
{EARNED_INCOME}
S                 D: L
{LIABILITIES}
\x0cS                 E: P
{POSITIONS}
S                 F: A
Date                  Parties To                                        Terms of Agreement
"""
sec = _sections(DOC)
assert set("CDEF") <= set(sec), sec.keys()
assert parse_earned_income(sec["C"]) == inc
assert parse_liabilities(sec["D"]) == liab
assert len(parse_positions(sec["E"])) == 5

parsed = parse_fdr(DOC)
assert len(parsed["earned_income"]) == 2
assert len(parsed["liabilities"]) == 2
assert len(parsed["assets"]) == 0     # no "A" section in this synthetic doc
assert len(parsed["positions"]) == 5

print("ok: FDR schedule D/C/A/E parsers -- wrapped brackets, owner codes, "
      "comments, page breaks, Undetermined values, tight-spacing money cells")
