#!/usr/bin/env python3
"""Tests for STOCK Act late-filing compliance module.

Covers day-counting logic with specific boundary cases:
  - exactly 45 days is NOT late
  - 46 days IS late
  - 1 day lag is never late
  - negative lag (a typo'd year) is excluded, and nothing else is

  python3 tests/test_compliance.py
"""
import sys
from pathlib import Path
import datetime as dt

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from congress_trades import compliance

# Test 1: Day-counting boundary – exactly 45 days is NOT late.
tx_date_45 = dt.date(2024, 1, 1)
dis_date_45 = dt.date(2024, 2, 15)  # Jan 1 + 45 days = Feb 15
lag_45 = (dis_date_45 - tx_date_45).days
assert lag_45 == 45, f"expected 45 days, got {lag_45}"
assert lag_45 <= compliance.STATUTORY_DAYS, "45 days should NOT be flagged as late"

# Test 2: 46 days IS late.
tx_date_46 = dt.date(2024, 1, 1)
dis_date_46 = dt.date(2024, 2, 16)  # Jan 1 + 46 days = Feb 16
lag_46 = (dis_date_46 - tx_date_46).days
assert lag_46 == 46, f"expected 46 days, got {lag_46}"
assert lag_46 > compliance.STATUTORY_DAYS, "46 days should be flagged as late"

# Test 3: 1 day lag is never late.
tx_date_1 = dt.date(2024, 1, 1)
dis_date_1 = dt.date(2024, 1, 2)
lag_1 = (dis_date_1 - tx_date_1).days
assert lag_1 == 1, f"expected 1 day, got {lag_1}"
assert lag_1 <= compliance.STATUTORY_DAYS, "1 day should NOT be flagged as late"

# Test 4: 0 day lag (same day) is never late.
tx_date_0 = dt.date(2024, 1, 1)
dis_date_0 = dt.date(2024, 1, 1)
lag_0 = (dis_date_0 - tx_date_0).days
assert lag_0 == 0, f"expected 0 days, got {lag_0}"
assert lag_0 <= compliance.STATUTORY_DAYS, "0 days should NOT be flagged as late"

# Test 5: there is no upper bound on lag. A ceiling anywhere in the multi-year
# tail deletes real filings -- Richard W. Allen disclosed 2017 transactions in a
# single 2023 filing -- so guard against one being reintroduced.
assert not hasattr(compliance, "MAX_SANE_LAG"), \
    "an upper lag bound is back; it silently drops the longest real delays"

# Test 6: Load function excludes bad data.
# We can't construct fixture rows without a database, so test against the real DB.
from congress_trades.config import CONFIG

rows, dropped = compliance.load(cfg=CONFIG)
assert rows, "no rows loaded from database"
assert dropped >= 0, "dropped count must be non-negative"

# All loaded rows must have valid lags.
for r in rows:
    assert r["lag"] >= 0, f"negative lag: {r}"

# The multi-year tail must survive the filter, or the module is hiding its
# own most significant rows.
assert max(r["lag"] for r in rows) > 2000, \
    "longest delays were filtered out; they are real and are the point"

# Test 7: is_late classification is correct.
for r in rows:
    expected_late = r["lag"] > compliance.STATUTORY_DAYS
    assert r["is_late"] == expected_late, \
        f"is_late mismatch for {r['member']}: {r['lag']} days, " \
        f"is_late={r['is_late']}, expected {expected_late}"

# Test 9: by_member() aggregation consistency.
by_mem = compliance.by_member(rows)
for name, mem_stats in by_mem.items():
    # Late count should not exceed total trades.
    assert mem_stats["late_count"] <= mem_stats["total_trades"], \
        f"{name}: late_count {mem_stats['late_count']} > " \
        f"total_trades {mem_stats['total_trades']}"

    # Late share should be in [0, 1].
    if mem_stats["total_trades"] > 0:
        expected_share = mem_stats["late_count"] / mem_stats["total_trades"]
        assert abs(mem_stats["late_share"] - expected_share) < 1e-9, \
            f"{name}: late_share mismatch"

# Test 10: Run selftest against the real database.
compliance.selftest(cfg=CONFIG)

# Test 11: Markdown output is well-formed.
report = compliance.build(cfg=CONFIG)
md = compliance.to_markdown(report)
assert isinstance(md, str), "to_markdown() should return a string"
assert len(md) > 0, "markdown output is empty"
assert "STOCK Act" in md, "title missing from markdown"
assert "45-day" in md, "statutory window not mentioned in markdown"

print("ok: day-counting boundaries, data quality, aggregation consistency, "
      "markdown rendering")
