#!/usr/bin/env python3
"""Self-checks for the judiciary bulk-CSV parsers -- the pieces most likely to
silently rot if a future Free Law Project snapshot changes column order, or if
someone "fixes" the CSV corruption this module works around and the recovery
logic starts dropping (or duplicating) real rows without anyone noticing.

  python3 tests/test_judiciary.py

Every fixture below is real text pulled from the 2026-06-30 bulk snapshot
(com-courtlistener-storage.s3-us-west-2.amazonaws.com/bulk-data/), not
invented -- including the corrupted one, which is the actual trigger: row 100
of financial-disclosures-2026-06-30.csv.bz2 has a literal, un-doubled ASCII
quote inside its free-text addendum ('Under "Investments and Trusts," the
"VMBH...'), which desyncs csv's column boundaries until the next quote
resyncs it three garbage "rows" later.
"""
import csv
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from congress_trades.judiciary import (
    norm_tx_type, parse_courts, parse_disclosures, parse_investments,
    parse_judge_courts, parse_people,
)

# --- the real corruption, verbatim from the 2026-06-30 snapshot ---------------------
# Row 99 (clean, multi-line addendum but properly quoted) is included as context;
# row 100's addendum breaks quoting; three garbage pseudo-rows follow before EOF.
CORRUPT_DISCLOSURES = """id,date_created,date_modified,year,download_filepath,filepath,thumbnail,thumbnail_status,page_count,sha1,report_type,is_amended,addendum_content_raw,addendum_redacted,has_been_extracted,person_id
"99","2021-01-01 00:00:59.614072+00","2021-01-01 00:01:58.275489+00","2003","https://com-courtlistener-storage.s3-us-west-2.amazonaws.com/financial-disclosures/judicial-watch/Glen M Williams Financial Disclosure Report for 2003.pdf","us/federal/judicial/financial-disclosures/3485/glen-morgan-williams-disclosure.2003_1.pdf","us/federal/judicial/financial-disclosures/3485/glen-morgan-williams-disclosure.2003-thumbnail_1.png","1","9","06595a04e6f7887d40a56fde42a770ab4b36e605","-1","f","(Indicate part of Report. }

This stock on line 17 (Ciena Corporation) was booght by

hn 5-16-2002 and should have been

placed on the 2002 Disclosure Report , this was ommitted in error,","f","t","3485"
"100","2021-01-01 00:01:54.401312+00","2021-01-01 00:02:34.481616+00","2003","https://com-courtlistener-storage.s3-us-west-2.amazonaws.com/financial-disclosures/judicial-watch/Harry T Edwards Financial Disclosure Report for 2003.pdf","us/federal/judicial/financial-disclosures/975/harry-thomas-edwards-disclosure.2003_1.pdf","us/federal/judicial/financial-disclosures/975/harry-thomas-edwards-disclosure.2003-thumbnail_1.png","1","8","878aa85a898f17884f3906e7341953f9f869d902","-1","f","Under \\"Investments and Trusts,” the \\"VMBH Lang Tenn Fund” on the Report for 2002 is the same as \\"Vanguard Insured Long Term Fund\\" an the Repert

fur 2003.

—
","f","t","975"
"""

raw_rows = list(csv.DictReader(io.StringIO(CORRUPT_DISCLOSURES)))
# csv itself already shows the damage: 5 logical rows for 2 real disclosures.
assert len(raw_rows) == 5, f"fixture should desync into 5 logical rows, got {len(raw_rows)}"
assert [r["id"] for r in raw_rows][2:] == ["fur 2003.", "—", ',f"'], \
    "the three garbage ids this corruption produces have changed -- re-paste the fixture"

disc, dropped = parse_disclosures(raw_rows)
assert dropped == 3, f"expected the 3 garbage rows dropped, got {dropped}"
assert [d["id"] for d in disc] == [99, 100]
assert disc[0]["person_id"] == 3485 and disc[0]["year"] == 2003
# Row 100 survives (numeric id + real timestamp precede the break), but the
# corruption shifts its trailing columns off the end, and DictReader pads a
# short row with None -- so its person_id is lost rather than wrong. That is
# the honest outcome: an unattributable disclosure, not a fabricated one.
assert disc[1]["id"] == 100 and disc[1]["person_id"] is None, \
    "row 100's person_id should be lost to the corruption, not invented"

# --- investments: real rows tied to disclosure 1108 (Harry S. Mattice, 2009) --------
# Not corrupted the way the index is -- no long free-text field -- so these parse
# cleanly straight from the source.
INVESTMENTS = """id,date_created,date_modified,page_number,description,redacted,income_during_reporting_period_code,income_during_reporting_period_type,gross_value_code,gross_value_method,transaction_during_reporting_period,transaction_date_raw,transaction_date,transaction_value_code,transaction_gain_code,transaction_partner,has_inferred_values,financial_disclosure_id
"36213","2021-01-04 03:23:51.128984+00","2021-01-04 03:23:51.129057+00","4","RidgecWorth Mid-Cap Value Eqy. Fund","f","","","","","Sold (part)","","","","","","t","1108"
"4558608","2021-02-10 00:23:41.274143+00","2021-02-10 00:23:41.274169+00","4","Fidelity Cash Reserves","f","A","Int/Div","K","T","","","","","","","f","20670"
"""
inv_rows = list(csv.DictReader(io.StringIO(INVESTMENTS)))
inv, orphaned = parse_investments(inv_rows, valid_ids={1108})
assert orphaned == 1, "the Fidelity row points at disclosure 20670, which isn't in valid_ids here"
assert len(inv) == 1 and inv[0]["disclosure_id"] == 1108
assert inv[0]["tx_type"] == "sell", "'Sold (part)' should normalize to sell"
assert inv[0]["description"] == "RidgecWorth Mid-Cap Value Eqy. Fund"

# Real free-text transaction verbs seen across the full 2026-06-30 snapshot
# (1,901,720 rows), transcribed by hand and by OCR across decades of forms.
for raw, expect in [("Buy", "buy"), ("Buy (add'l)", "buy"), ("BUY", "buy"), ("buy", "buy"),
                    ("Sold", "sell"), ("Sold (part)", "sell"), ("Sell", "sell"),
                    ("sell", "sell"), ("Redeemed", "sell"), ("Matured", "sell"),
                    ("Exempt", "other"), ("Open", "other"), ("Closed", "other"),
                    ("", "")]:
    assert norm_tx_type(raw) == expect, f"{raw!r} -> {norm_tx_type(raw)!r}, expected {expect!r}"

# --- courts + judge/court linkage: real rows for Robert Peter Aguilar (N.D. Cal.) ---
COURTS = """id,pacer_court_id,pacer_has_rss_feed,pacer_rss_entry_types,date_last_pacer_contact,fjc_court_id,date_modified,in_use,has_opinion_scraper,has_oral_argument_scraper,position,citation_string,short_name,full_name,url,start_date,end_date,jurisdiction,notes,parent_court_id
"cand",,,"",,"","2016-09-08 20:38:41+00","t","t","f","366.97","N.D. Cal.","N.D. California","District Court, N.D. California","http://www.cand.uscourts.gov/","1886-01-01",,"FD","",
"""
POSITIONS = """id,date_created,date_modified,position_type,job_title,sector,organization_name,location_city,location_state,date_nominated,date_elected,date_recess_appointment,date_referred_to_judicial_committee,date_judicial_committee_action,judicial_committee_action,date_hearing,date_confirmation,date_start,date_granularity_start,date_termination,termination_reason,date_granularity_termination,date_retirement,nomination_process,vote_type,voice_vote,votes_yes,votes_no,votes_yes_percent,votes_no_percent,how_selected,has_inferred_values,appointer_id,court_id,person_id,predecessor_id,school_id,supervisor_id
"177","2016-04-20 15:15:55.673834+00","2016-04-20 15:15:55.673862+00","jud","","","","","","1980-04-03","","","1980-04-15","1980-06-17","","","1980-06-18","1980-06-18","%Y-%m-%d","1996-06-24","retire_vol","%Y-%m-%d","1996-04-15","","s","t","","","","","a_pres","f","39","cand","65","","",""
"172","2016-04-20 15:15:55.603571+00","2016-04-20 15:15:55.6036+00","","U.S. Army Reserve","","","","","","","","","","","","","","1986-01-01","%Y","1997-01-01","","%Y","","","","","","","","","f","","","64","","",""
"""
courts = parse_courts(list(csv.DictReader(io.StringIO(COURTS))))
assert courts["cand"] == "District Court, N.D. California"
jc = parse_judge_courts(list(csv.DictReader(io.StringIO(POSITIONS))), courts)
assert jc[65] == ("cand", "District Court, N.D. California")
assert 64 not in jc, "a non-judicial position_type ('') must not produce a court match"

PEOPLE = """id,date_created,date_modified,date_completed,fjc_id,slug,name_first,name_middle,name_last,name_suffix,date_dob,date_granularity_dob,date_dod,date_granularity_dod,dob_city,dob_state,dob_country,dod_city,dod_state,dod_country,gender,religion,ftm_total_received,ftm_eid,has_photo,is_alias_of_id
"65","2016-04-20 15:15:55.647722+00","2020-11-25 16:30:09.267808+00","","15","robert-peter-aguilar","Robert","Peter","Aguilar","","1931-01-01","%Y","","","Madera","CA","United States","","","United States","m","","","","f",""
"""
people = parse_people(list(csv.DictReader(io.StringIO(PEOPLE))))
assert people[65]["name_last"] == "Aguilar" and people[65]["name_first"] == "Robert"

print("ok: disclosure-index corruption recovery (5 logical rows -> 2 real, 1 "
      "unattributable), investment parsing + tx-type normalization, court and "
      "judge-position linkage, people identity -- all against real 2026-06-30 "
      "bulk-snapshot fixtures")
