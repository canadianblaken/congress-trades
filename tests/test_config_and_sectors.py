#!/usr/bin/env python3
"""Checks for the two things that broke in practice: the SEC-acceptable User-Agent
shape, and the SIC -> sector rollup.

  python3 tests/test_config_and_sectors.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from congress_trades.config import Config, user_agent
from congress_trades.sectors import sector_for_sic

# sec.gov answers 403 when the User-Agent carries a bare URL; keep the plain
# "name/version (contact)" shape.
ua = user_agent(Config(contact="someone@example.com"))
assert ua == "congress-trades/0.1 (someone@example.com)", ua
assert "http" not in ua, "a URL in the UA makes sec.gov 403"
assert "example.com" in user_agent(Config(contact=" example.com ")), "contact is trimmed"

# SIC rollups: specific codes beat the major group, and unknowns stay honest.
assert sector_for_sic("3674") == "Semiconductors"
assert sector_for_sic("4911") == "Utilities & Power"
assert sector_for_sic("3585") == "HVAC & Refrigeration"
assert sector_for_sic("6798") == "Real Estate (REIT)"
assert sector_for_sic("2911") == "Petroleum Refining"      # major group 29
assert sector_for_sic("1311") == "Mining & Energy Extraction"
assert sector_for_sic("6726") == "Funds & Holding Companies"
assert sector_for_sic("") == "Unclassified"
assert sector_for_sic("nonsense") == "Unclassified"
assert sector_for_sic("99999") == "Public Administration"

print("ok: user-agent shape and SIC sector rollup")
