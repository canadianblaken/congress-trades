"""Command line entry point.

    python -m congress_trades all            # collect, enrich, classify, score, render
    python -m congress_trades backfill
    python -m congress_trades enrich --rotate 20
    python -m congress_trades publish
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from . import (advise, alerts, assets, backtest, collect, committees, digest,
               jurisdiction, lag, llm, parserqa, pipeline, prices, resolve,
               scorecard, topics)
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

    p = sub.add_parser("prices", help="forward returns per disclosure from daily closes")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--quiet", action="store_true")

    p = sub.add_parser("digest", help="prompt-sized markdown/JSON brief of the trends")
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--floor", type=int, default=CONFIG.default_floor)
    p.add_argument("--json", action="store_true")
    p.add_argument("--selftest", action="store_true")

    p = sub.add_parser("scorecard", help="rank members by benchmark-adjusted record")
    p.add_argument("--floor", type=int, default=CONFIG.default_floor)
    p.add_argument("--horizon", choices=("30", "90"), default="90")
    p.add_argument("--min-trades", type=int, default=scorecard.MIN_TRADES)
    p.add_argument("--limit", type=int, default=0, help="0 = every qualifying member")
    p.add_argument("--json", action="store_true")
    p.add_argument("--selftest", action="store_true")

    p = sub.add_parser("backtest", help="would following the disclosures have paid?")
    p.add_argument("--split", default=backtest.SPLIT,
                   help="members are ranked before this date, graded after it")
    p.add_argument("--horizon", choices=("30", "90"), default="90")
    p.add_argument("--floor", type=int, default=0)
    p.add_argument("--walk-forward", action="store_true",
                   help="re-rank every year and grade on the next, pooling folds")
    p.add_argument("--json", action="store_true")
    p.add_argument("--selftest", action="store_true")

    p = sub.add_parser("lag", help="does filing lag relate to how the trade did?")
    p.add_argument("--floor", type=int, default=1,
                   help="1 (default) uses every measurable trade, not the display floor")
    p.add_argument("--json", action="store_true")
    p.add_argument("--selftest", action="store_true")

    p = sub.add_parser("committees", help="committee meeting dates: top up from the "
                       "API, or load the shipped snapshot")
    p.add_argument("--seed", action="store_true",
                   help="load the committed snapshot instead of fetching (no key)")
    p.add_argument("--export", action="store_true",
                   help="rewrite the committed snapshot from the database")
    p.add_argument("--quiet", action="store_true")

    p = sub.add_parser("llm", help="check the configured model: reachable, and "
                       "can it honour a JSON schema?")
    p.add_argument("--selftest", action="store_true",
                   help="shape checks only, calling no model")

    p = sub.add_parser("topics", help="what each committee meeting was about: fetch "
                       "the titles, then tag them by industry with a model")
    p.add_argument("--stage", choices=("fetch", "tag", "both"), default="both",
                   help="'fetch' needs CONGRESS_API_KEY; 'tag' needs a model")
    p.add_argument("--since", default="",
                   help="only fetch titles for meetings on or after this ISO date "
                        "— the full back catalogue is roughly two hours")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--refresh", action="store_true", help="re-tag titles already stored")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--selftest", action="store_true")

    p = sub.add_parser("jurisdiction", help="which industries each committee "
                       "oversees: a table generated once by a model and "
                       "committed as data")
    p.add_argument("--generate", action="store_true",
                   help="ask a model about every committee on the roster and "
                        "diff the answer against the committed table")
    p.add_argument("--write", action="store_true",
                   help="commit what --generate proposed to "
                        "seed/committee_sectors.json, calling no model again")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--selftest", action="store_true")

    p = sub.add_parser("parser-qa", help="does the House parser still read the "
                       "filings? An exact scan of every cached text, then a "
                       "sampled model audit")
    p.add_argument("--scan", action="store_true",
                   help="the exact pass only: compare transaction headers found "
                        "against rows returned, calling no model")
    p.add_argument("--sample", type=int, default=25,
                   help="how many cached filings to ask a model about (0 = all)")
    p.add_argument("--doc", default="", help="audit one filing by DocID")
    p.add_argument("--seed", type=int, default=0,
                   help="the sample is deterministic, so a finding can be reproduced")
    p.add_argument("--refresh", action="store_true",
                   help="re-audit filings this model has already seen")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--selftest", action="store_true")

    p = sub.add_parser("timing", help="do members trade around their own hearings?")
    p.add_argument("--floor", type=int, default=1)
    p.add_argument("--window", type=int, default=30)
    p.add_argument("--json", action="store_true")
    p.add_argument("--selftest", action="store_true")
    p.add_argument("--sector-matched", action="store_true",
                   help="count only meetings whose subject touches the industry "
                        "traded (needs `topics`); the narrower, more meaningful arm")

    p = sub.add_parser("alerts", help="only what crossed a bar since last run")
    p.add_argument("--days", type=int, default=14)
    p.add_argument("--dry-run", action="store_true",
                   help="look without marking anything as seen")
    p.add_argument("--headline", action="store_true", help="one line, for a push")
    p.add_argument("--json", action="store_true")
    p.add_argument("--selftest", action="store_true")

    p = sub.add_parser("repair-tickers", help="recover tickers an older parser "
                       "dropped from the asset name")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--quiet", action="store_true")

    p = sub.add_parser("mix", help="asset mix: who is trading and who is parking")
    p.add_argument("--floor", type=int, default=0)
    p.add_argument("--json", action="store_true")
    p.add_argument("--selftest", action="store_true")

    p = sub.add_parser("resolve", help="label the untickered assets with a model, "
                       "and recover the tickers it gets right")
    p.add_argument("--scope", choices=("unlabelled", "all"), default="unlabelled",
                   help="'unlabelled' asks only about names no pattern could place; "
                        "'all' re-asks about every untickered name, to check the "
                        "model against the patterns")
    p.add_argument("--limit", type=int, default=0, help="stop after N names")
    p.add_argument("--dry-run", action="store_true", help="show the proposals, write nothing")
    p.add_argument("--apply", action="store_true",
                   help="also write verified tickers into the trades table")
    p.add_argument("--refresh", action="store_true", help="re-ask about names already stored")
    p.add_argument("--reverify", action="store_true",
                   help="re-run the gates over proposals already stored, calling "
                        "no model — use after the verification rules change")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--selftest", action="store_true")

    p = sub.add_parser("advise", help="send the digest to a model (see CONGRESS_LLM_PROVIDER)")
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--floor", type=int, default=CONFIG.default_floor)
    p.add_argument("--dry-run", action="store_true", help="print the prompt, call nothing")
    p.add_argument("--check", action="store_true",
                   help="audit the reply against the digest afterwards: symbols "
                        "and figures it invented, and claims the data does not "
                        "support")
    p.add_argument("--selftest", action="store_true",
                   help="shape checks only, calling no model")

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
    if args.cmd == "prices":
        return prices.compute(CONFIG, args.limit, args.quiet)
    if args.cmd == "digest":
        if args.selftest:
            digest.selftest(CONFIG)
            return 0
        d = digest.build(args.days, args.floor, CONFIG)
        if args.json:
            d.pop("_sectors_by_ticker", None)
            print(json.dumps(d, indent=2, default=list))
        else:
            sys.stdout.write(digest.to_markdown(d))
        return 0
    if args.cmd == "scorecard":
        if args.selftest:
            scorecard.selftest(CONFIG)
            return 0
        rows = scorecard.members(args.floor, args.horizon, args.min_trades, CONFIG)
        if args.json:
            print(json.dumps(rows[:args.limit] if args.limit else rows, indent=2))
        else:
            sys.stdout.write(scorecard.to_markdown(rows, args.horizon, args.limit))
        return 0
    if args.cmd == "backtest":
        if args.selftest:
            backtest.selftest(CONFIG)
            return 0
        if args.walk_forward:
            d = backtest.walk_forward(args.horizon, args.floor, cfg=CONFIG)
            render_bt = backtest.wf_to_markdown
        else:
            d = backtest.run(args.split, args.horizon, args.floor, cfg=CONFIG)
            render_bt = backtest.to_markdown
        if args.json:
            print(json.dumps(d, indent=2))
        else:
            sys.stdout.write(render_bt(d))
        return 0
    if args.cmd == "lag":
        if args.selftest:
            lag.selftest(CONFIG)
            return 0
        d = lag.build(args.floor, CONFIG)
        if args.json:
            d.pop("within", None) and None
            print(json.dumps(d, indent=2, default=str))
        else:
            sys.stdout.write(lag.to_markdown(d))
        return 0
    if args.cmd == "committees":
        if args.export:
            return committees.export(CONFIG, args.quiet)
        if args.seed:
            return committees.seed(CONFIG, force=True, quiet=args.quiet)
        return committees.collect(CONFIG, quiet=args.quiet)
    if args.cmd == "jurisdiction":
        if args.selftest:
            jurisdiction.selftest(CONFIG)
            return 0
        return jurisdiction.run(CONFIG, args.generate, args.write, args.limit,
                                args.quiet)
    if args.cmd == "parser-qa":
        if args.selftest:
            parserqa.selftest(CONFIG)
            return 0
        return parserqa.run(CONFIG, args.scan, args.sample, args.doc, args.seed,
                            args.refresh, args.dry_run, args.quiet)
    if args.cmd == "timing":
        if args.selftest:
            committees.selftest(CONFIG)
            return 0
        d = committees.build(args.floor, args.window, CONFIG,
                             args.sector_matched)
        if args.json:
            print(json.dumps(d, indent=2, default=str))
        else:
            sys.stdout.write(committees.to_markdown(d))
        return 0
    if args.cmd == "alerts":
        if args.selftest:
            alerts.selftest(CONFIG)
            return 0
        found = alerts.find(args.days, CONFIG, record=not args.dry_run)
        if args.json:
            print(json.dumps(found, indent=2, default=str))
        elif args.headline:
            h = alerts.headline(found)
            if h:
                print(h)
        else:
            sys.stdout.write(alerts.to_text(found, args.days))
        # Exit 1 on a quiet day, so a cron line can skip notifying at all.
        return 0 if found else 1
    if args.cmd == "repair-tickers":
        return assets.repair_tickers(CONFIG, args.dry_run, args.quiet)
    if args.cmd == "mix":
        if args.selftest:
            assets.selftest(CONFIG)
            return 0
        d = assets.mix(args.floor, CONFIG)
        if args.json:
            print(json.dumps(d, indent=2, default=list))
        else:
            sys.stdout.write(assets.to_markdown(d))
        return 0
    if args.cmd == "llm":
        if args.selftest:
            llm.selftest()
            return 0
        return llm.check()
    if args.cmd == "topics":
        if args.selftest:
            topics.selftest(CONFIG)
            return 0
        return topics.run(CONFIG, args.stage, args.since, args.limit,
                          args.refresh, args.dry_run, args.quiet)
    if args.cmd == "resolve":
        if args.selftest:
            resolve.selftest(CONFIG)
            llm.selftest()
            return 0
        if args.reverify:
            return resolve.reverify(CONFIG, args.apply, args.dry_run, args.quiet)
        return resolve.run(CONFIG, args.scope, args.limit, args.dry_run,
                           args.apply, args.refresh, args.quiet)
    if args.cmd == "advise":
        if args.selftest:
            advise.selftest()
            return 0
        return advise.run(args.days, args.floor, args.dry_run, cfg=CONFIG,
                          check=args.check)
    if args.cmd == "publish":
        return render(CONFIG)
    if args.cmd == "all":
        for rc in (pipeline.backfill(CONFIG, quiet=args.quiet),
                   assets.repair_tickers(CONFIG, quiet=args.quiet),
                   pipeline.enrich(CONFIG, rotate=args.rotate, quiet=args.quiet),
                   pipeline.tag_sectors(CONFIG, quiet=args.quiet),
                   pipeline.score_votes(CONFIG),
                   prices.compute(CONFIG, quiet=args.quiet)):
            if rc:
                return rc
        return render(CONFIG)
    return 1


if __name__ == "__main__":
    try:
        rc = main()
    except collect.MissingTool as e:
        # A traceback would bury the one line that tells you what to install.
        print(f"error: {e}", file=sys.stderr)
        rc = 2
    raise SystemExit(rc)
