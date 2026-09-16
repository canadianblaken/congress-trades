#!/usr/bin/env python3
"""Self-check for the LDA filing parser -- the piece that breaks if lda.gov
changes its JSON shape. Real API responses, trimmed, pasted as fixtures rather
than fetched live so this runs offline and fast.

  python3 tests/test_lobbying.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from congress_trades.lobbying import ISSUE_SECTORS, parse_filing, sectors_for_issue

# --- real record: lda.gov/api/v1/filings/7d0af0e8-eba3-4c5b-8ca8-5668aa454325/ ------
# Fetched 2026-09-16, client=AMAZON. Two issue codes on one filing, both aimed at
# both chambers, and a description that names actual bills (S. 130, S. 4746) --
# proof this data CAN carry a bill number, in free text, never a member name.
AMAZON = json.loads("""
{
  "filing_uuid": "7d0af0e8-eba3-4c5b-8ca8-5668aa454325",
  "filing_type": "Q2", "filing_type_display": "2nd Quarter - Report",
  "filing_year": 2026, "filing_period": "second_quarter",
  "filing_document_url": "https://lda.gov/filings/public/filing/7d0af0e8-eba3-4c5b-8ca8-5668aa454325/print/",
  "income": "50000.00", "expenses": null, "dt_posted": "2026-07-06T16:15:01-04:00",
  "registrant": {"id": 401036920, "name": "BLOOM STRATEGIC COUNSEL"},
  "client": {"client_id": 125, "name": "AMAZON", "general_description": "Online retailer", "state": "WA"},
  "lobbying_activities": [
    {"general_issue_code": "LBR", "general_issue_code_display": "Labor Issues/Antitrust/Workplace",
     "description": "Issues related to competition in technology issues; S. 130, Competition and Antitrust Law Reform Act; S. 4746, American Innovation and Choice Online Act",
     "lobbyists": [{"lobbyist": {"first_name": "SETH", "last_name": "BLOOM"}, "covered_position": "General Counsel, Senate Antitrust Subcommittee"}],
     "government_entities": [{"id": 2, "name": "HOUSE OF REPRESENTATIVES"}, {"id": 1, "name": "SENATE"}]},
    {"general_issue_code": "CPI", "general_issue_code_display": "Computer Industry",
     "description": "Issues related to competition in technology issues; S. 130, Competition and Antitrust Law Reform Act; S. 4746, American Innovation and Choice Online Act",
     "lobbyists": [{"lobbyist": {"first_name": "SETH", "last_name": "BLOOM"}, "covered_position": "General Counsel, Senate Antitrust Subcommittee"}],
     "government_entities": [{"id": 2, "name": "HOUSE OF REPRESENTATIVES"}, {"id": 1, "name": "SENATE"}]}
  ]
}
""")

f, acts = parse_filing(AMAZON)
assert f["filing_uuid"] == "7d0af0e8-eba3-4c5b-8ca8-5668aa454325", f
assert f["client_name"] == "AMAZON" and f["registrant_name"] == "BLOOM STRATEGIC COUNSEL", f
assert f["income"] == 50000.0, f            # numeric string -> float
assert f["expenses"] is None, f             # JSON null -> None, never 0
assert len(acts) == 2, acts
assert [a["issue_code"] for a in acts] == ["LBR", "CPI"], acts
assert acts[0]["activity_idx"] == 0 and acts[1]["activity_idx"] == 1, acts
assert acts[0]["chambers"] == "House,Senate", acts[0]     # both chambers named
assert acts[0]["agencies"] == "", acts[0]                 # no non-chamber entity here
assert "S. 130" in acts[0]["description"], acts[0]        # a bill number, in free text
# The one thing this data must never be made to imply: no member name anywhere
# in the parsed row, even though the filing carries a lobbyist's former Hill title.
for a in acts:
    assert "member" not in a and "bioguide" not in a, a

# --- real record: agency-only entities (no chamber named), long text truncated ------
# lda.gov/api/v1/filings/, client=STATE OF LOC NATION..., fetched 2026-09-16.
AGENCY_ONLY = json.loads("""
{
  "filing_uuid": "c2d195d2-8df2-4b75-8e95-7014cacd5e81",
  "filing_type": "Q2", "filing_type_display": "2nd Quarter - Report",
  "filing_year": 2025, "filing_period": "second_quarter",
  "filing_document_url": "https://lda.gov/filings/public/filing/c2d195d2-8df2-4b75-8e95-7014cacd5e81/print/",
  "income": "20000000.00", "expenses": null, "dt_posted": "2025-04-06T03:26:59-04:00",
  "registrant": {"id": 401108853, "name": "LOC COMMUNITY ASSOCIATION"},
  "client": {"client_id": 61287, "name": "STATE OF LOC NATION GLOBAL PUBLIC BENEFIT CORPORATION", "state": "DE"},
  "lobbying_activities": [
    {"general_issue_code": "CIV", "general_issue_code_display": "Civil Rights/Civil Liberties",
     "description": "IMMEDIATE ACTION DEMAND: PASSAGE OF HR 40 WITH RESTITUTION MANDATE",
     "lobbyists": [], "government_entities": [
        {"id": 185, "name": "Bureau of Engraving & Printing"},
        {"id": 8, "name": "Congressional Budget Office (CBO)"},
        {"id": 2, "name": "HOUSE OF REPRESENTATIVES"},
        {"id": 183, "name": "Office of the Comptroller of the Currency (OCC)"},
        {"id": 1, "name": "SENATE"}]}
  ]
}
""")

f2, acts2 = parse_filing(AGENCY_ONLY)
assert f2["income"] == 20_000_000.0, f2
assert acts2[0]["chambers"] == "House,Senate", acts2[0]
assert acts2[0]["agencies"] == "Bureau of Engraving & Printing,Congressional Budget Office (CBO)," \
       "Office of the Comptroller of the Currency (OCC)", acts2[0]
assert acts2[0]["issue_code"] == "CIV", acts2[0]

# --- a filing with no lobbying_activities at all (a bare registration can have none) -
EMPTY = {"filing_uuid": "x", "filing_type": "RR", "filing_type_display": "Registration",
        "filing_year": 2026, "filing_period": "third_quarter",
        "filing_document_url": "", "income": None, "expenses": None, "dt_posted": "",
        "registrant": {}, "client": {}, "lobbying_activities": []}
f3, acts3 = parse_filing(EMPTY)
assert f3["client_name"] == "" and f3["registrant_name"] == "", f3
assert acts3 == [], acts3

# --- issue -> sector mapping is a lookup, not a guess that can crash -----------------
assert "Healthcare Services" in sectors_for_issue("HCR")
assert "Healthcare Services" in ISSUE_SECTORS["HCR"]
assert sectors_for_issue("hcr") == sectors_for_issue("HCR"), "case should not matter"
assert sectors_for_issue("") == () and sectors_for_issue("NOPE") == ()
# Deliberately-omitted broad codes must stay out, same posture as the committee map.
assert "BUD" not in ISSUE_SECTORS and "FOR" not in ISSUE_SECTORS

print("ok: LDA filing parser, chamber/agency split, bill-number-in-free-text, "
      "issue->sector lookup, no member ever attached")
