"""Command line entry point.

    python -m congress_trades all            # collect, enrich, classify, score, render
    python -m congress_trades backfill
    python -m congress_trades enrich --rotate 20
    python -m congress_trades publish
"""
from __future__ import annotations

import argparse
import logging
import sys

from . import pipeline
from .config import CONFIG
from .publish import render


def _require_contact() -> bool:
    """sec.gov answers 403 to a User-Agent without real contact details, so stop with
    an explanation rather than letting the user chase a mystery HTTP error."""
    if CONFIG.contact.strip():
        return True
    print(
        "CONGRESS_CONTACT is not set.\n\n"
        "  sec.gov requires automated clients to identify themselves and will\n"
        "  answer 403 without it. Set it to an email address you control:\n\n"
        "      export CONGRESS_CONTACT=\"you@example.com\"\n\n"
        "  Only the SEC industry classification needs this. `backfill`, `enrich`,\n"
        "  `votes` and `publish` all work without it.",
        file=sys.stderr)
    return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="congress-trades",
        description="Collect US congressional stock-trade disclosures and render them.")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("backfill", help="collect filings from the House Clerk and Senate eFD")
    p.add_argument("--chamber", choices=("house", "senate", "both"), default="both")
    p.add_argument("--years", help="comma separated, e.g. 2026,2025")
    p.add_argument("--quiet", action="store_true")

    p = sub.add_parser("enrich", help="resolve filers, attach bios and committee seats")
    p.add_argument("--rotate", type=int, default=0,
                   help="re-check only the N stalest members (for a daily schedule)")
    p.add_argument("--no-wiki", action="store_true")
    p.add_argument("--refresh-roster", action="store_true",
                   help="force a roster re-download (after an election)")
    p.add_argument("--quiet", action="store_true")

    p = sub.add_parser("sectors", help="classify tickers by SEC industry code")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--quiet", action="store_true")

    p = sub.add_parser("votes", help="party unity and DW-NOMINATE from Voteview")
    p.add_argument("--congress", type=int, default=0)

    sub.add_parser("publish", help="render the self-contained HTML page")

    p = sub.add_parser("all", help="run every step in order")
    p.add_argument("--rotate", type=int, default=0)
    p.add_argument("--quiet", action="store_true")

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(message)s")

    if args.cmd in ("sectors", "all") and not _require_contact():
        return 2

    if args.cmd == "backfill":
        return pipeline.backfill(CONFIG, args.chamber,
                                 args.years.split(",") if args.years else None, args.quiet)
    if args.cmd == "enrich":
        return pipeline.enrich(CONFIG, args.rotate, args.no_wiki,
                               args.refresh_roster, args.quiet)
    if args.cmd == "sectors":
        return pipeline.tag_sectors(CONFIG, args.limit, args.quiet)
    if args.cmd == "votes":
        return pipeline.score_votes(CONFIG, args.congress)
    if args.cmd == "publish":
        return render(CONFIG)
    if args.cmd == "all":
        for rc in (pipeline.backfill(CONFIG, quiet=args.quiet),
                   pipeline.enrich(CONFIG, rotate=args.rotate, quiet=args.quiet),
                   pipeline.tag_sectors(CONFIG, quiet=args.quiet),
                   pipeline.score_votes(CONFIG)):
            if rc:
                return rc
        return render(CONFIG)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
