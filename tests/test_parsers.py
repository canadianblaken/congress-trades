#!/usr/bin/env python3
"""Self-checks for the two disclosure parsers -- the pieces that silently rot when the
Clerk tweaks a PDF layout or the Senate changes its table markup.

  python3 tests/test_parsers.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from congress_trades.collect import normalize, parse_house_ptr, parse_senate_report

# --- House: real `pdftotext -layout` output from filing 20035143, trimmed ------------
HOUSE = """
          SP          Bloom Energy Corporation Class A         P                07/24/2026 07/24/2026            $1,000,001 -
                      Common Stock (BE) [ST]                                                                     $5,000,000

          SP          Bloom Energy Corporation Class A         P                07/28/2026 07/28/2026            $500,001 -
                      Common Stock (BE) [OP]                                                                     $1,000,000

          JT          Apple Inc. (AAPL) [ST]                   S                08/01/2026 08/02/2026            $15,001 -
                                                                                                                 $50,000

                      Some Municipal Bond, untickered          P                08/03/2026 08/04/2026            $1,001 -
                                                                                                                 $15,000
"""
h = parse_house_ptr(HOUSE)
assert len(h) == 4, f"expected 4 house transactions, got {len(h)}: {h}"
assert [r["ticker"] for r in h] == ["BE", "BE", "AAPL", ""], h
assert [r["tx_type"] for r in h] == ["buy", "buy", "sell", "buy"], h
assert [r["owner"] for r in h] == ["Spouse", "Spouse", "Joint", "Self"], h
assert h[0]["amount_min"] == 1000001 and h[2]["amount_min"] == 15001, h
assert h[0]["tx_date"] == "07/24/2026" and h[0]["disclosed"] == "07/24/2026", h[0]
assert "Bloom Energy" in h[0]["asset_name"], h[0]

# --- Senate: real efdsearch report markup, trimmed ----------------------------------
SENATE = """
<table><tr><th>#</th><th>Transaction Date</th><th>Owner</th><th>Ticker</th><th>Asset Name</th>
<th>Asset Type</th><th>Type</th><th>Amount</th><th>Comment</th></tr>
<tr><td>1</td><td>08/19/2026</td><td>Spouse</td><td>NVDA</td><td>NVIDIA Corp</td>
<td>Stock</td><td>Purchase</td><td>$50,001 - $100,000</td><td>--</td></tr>
<tr><td>2</td><td>08/07/2026</td><td>Self</td><td>--</td><td>W.L. Gore &amp; Associates, Inc.</td>
<td>Non-Public Stock</td><td>Sale (Full)</td><td>$100,001 - $250,000</td><td>--</td></tr>
<tr><td>3</td><td>08/05/2026</td><td>Self</td><td>T</td><td>AT&amp;T Inc</td>
<td>Stock</td><td>Purchase</td><td>$1,001 - $15,000</td><td>--</td></tr></table>
"""
s = parse_senate_report(SENATE)
assert len(s) == 3, f"expected 3 senate transactions, got {len(s)}: {s}"
assert [r["ticker"] for r in s] == ["NVDA", "", "T"], s
assert [r["tx_type"] for r in s] == ["buy", "sell", "buy"], s
assert s[1]["asset_name"] == "W.L. Gore & Associates, Inc.", s[1]  # entities unescaped
assert s[0]["amount_min"] == 50001, s[0]
assert parse_senate_report("<table><tr><td>no</td></tr></table>") == []

# --- normalize: the $15k floor and ISO dates ----------------------------------------
n = normalize(h + s, 15001)
assert all(r["amount_min"] >= 15001 for r in n), n
assert len(n) == 5, f"floor should drop the two $1,001 rows, kept {len(n)}"
assert n[0]["tx_date"] == "2026-07-24", n[0]
assert normalize(h, 10**9) == []

print("ok: house PDF + senate HTML parsers, owner/ticker/type/amount, floor and ISO dates")
