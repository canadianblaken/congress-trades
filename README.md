# congress-trades

US congressional stock-trade disclosures, collected from the primary sources and
rendered as a single self-contained HTML page.

No API keys. No paid data feed. Just the government's own filings, parsed --
plus an optional `digest`/`advise` pair that hands the result to an LLM of your
choosing, with the limits of this data spelled out in the prompt.

![status](https://img.shields.io/badge/data-public%20domain-blue) ![python](https://img.shields.io/badge/python-3.10%2B-blue)

## What it does

Members of Congress must disclose securities transactions within 45 days under the
STOCK Act. Those filings are public but awkward to use: the House publishes PDFs
behind a yearly ZIP index, the Senate hides an HTML table behind a terms-acceptance
handshake, and neither tells you who the person actually is.

This collects both chambers, resolves each filer to a real legislator, and produces
one page you can open in a browser:

- **Member view** — click a person for their photo, bio, party, seat, committee
  assignments, filing-lag record, party-unity score, and every trade they disclosed.
  A **Beyond trades** block adds their late-filing record, PAC money from the
  sectors their committees oversee, and annual-report debts, income and positions.
  Click a bar in their monthly chart to list just that month's buys or sells.
- **Movers view** — an activity chart whose bars list that month's trades, net
  buying by industry, the names the most members converged on, and the *lone large
  positions* a single member took that nobody else touched.
- **Scoreboard view** — every member ranked by how their disclosed trades actually
  turned out against the index, buys and sells both, with their best and worst call
  spelled out, each record split into halves, and a warning on any member whose
  alpha is really one concentrated bet.
- **Filters** — chamber, time window, and a disclosure-size floor, applied live.

Everything is inlined into the output file. There is no server and no API.

There is also a **local portal** (`./run.sh portal`) that serves every report the
CLI can print, plus a Maintenance tab for the jobs that collect and write.

## Data sources

| Source | Used for | Auth |
|---|---|---|
| [House Clerk](https://disclosures-clerk.house.gov/) | House PTR filings, and the annual FDRs in the same ZIP (debts, outside income, holdings, board seats) | none |
| [Senate eFD](https://efdsearch.senate.gov/search/home/) | Senate PTR filings | terms handshake |
| [unitedstates/congress-legislators](https://github.com/unitedstates/congress-legislators) | member identity, party, seat, committees | none |
| [Wikipedia REST](https://en.wikipedia.org/api/rest_v1/) | biography and portrait | none |
| [SEC EDGAR](https://www.sec.gov/) | ticker → SIC industry classification | contact in User-Agent |
| [Voteview](https://voteview.com/) | roll-call votes, party unity, DW-NOMINATE | none |
| [Yahoo Finance chart API](https://finance.yahoo.com/) | daily closes, for forward returns | none |
| [FEC bulk data](https://www.fec.gov/data/browse-data/?tab=bulk-data) | candidate identity, receipts, PAC contributions | none |
| [lda.gov](https://lda.gov/api/v1/filings/) | LD-2 quarterly lobbying disclosures | none |
| [CourtListener bulk snapshots](https://storage.courtlistener.com/bulk-data/) | federal judges' financial disclosures | none |

Two sources are deliberately **absent**, both for the same reason — there is no
lawful automated path to them, and this project does not manufacture one:

- **Federal tax returns.** Not public for members of Congress. No source exists.
- **Executive branch OGE Form 278e**, and the judiciary's own CETA database at
  `pub.jefs.uscourts.gov`. The former is request-only. The latter requires a
  fresh identity registration on every visit behind a reCAPTCHA, so the judicial
  data here comes from CourtListener's sanctioned bulk snapshots instead — which
  is also why it stops in 2022 (see `judiciary.py`). CourtListener's own site is
  not crawled: its robots.txt disallows automated agents and points them at the
  bulk data, which is what this reads.

## What you need

Nothing, for almost all of it. No account, no paid feed, no API key:

| | |
|---|---|
| House Clerk, Senate eFD, Wikipedia, Voteview, Yahoo prices | no auth at all |
| `CONGRESS_CONTACT` | **not a key** — your own email, sent in the User-Agent. The SEC answers 403 to anonymous automated clients, so `sectors` and `all` stop with an explanation without it |
| `CONGRESS_API_KEY` | optional. Extends committee meetings past the shipped snapshot, and is the only way to get meeting *titles* for `topics`. Free and instant at [api.congress.gov](https://api.congress.gov/sign-up/) |
| `CONGRESS_LLM_*` / `CONGRESS_OLLAMA_*` | optional. `advise`, `resolve` and `topics`; either a local Ollama or any OpenAI-compatible endpoint |

So collection, pricing, the scorecard, the backtests, filing lag, committee
timing, alerts, the rendered page and the MCP server all work with one email
address and no signup anywhere.

Settings go in a `.env` beside this README — copy `.env.example`. Both `run.sh`
and `python -m congress_trades` read it, and anything already exported in your
shell wins over the file, so `CONGRESS_OLLAMA_MODEL=gemma4:12b python -m
congress_trades resolve` still overrides it for one run.

## Install

```bash
git clone <your-fork-url> congress-trades
cd congress-trades
pip install -r requirements.txt
```

Needs **Python 3.10 or newer**. Linux distributions ship that already; macOS
does not — its built-in `python3` is 3.9, so `brew install python@3.12` (or any
3.10+). `./run.sh` finds a newer interpreter on its own and says which it picked,
or you can name one with `PYTHON=/path/to/python3.12 ./run.sh`.

Also needs **`pdftotext`** (from Poppler) on your PATH — House filings are PDFs:

```bash
sudo dnf install poppler-utils      # Fedora/RHEL
sudo apt install poppler-utils      # Debian/Ubuntu
brew install poppler                # macOS
```

## Use

```bash
export CONGRESS_CONTACT="you@example.com"   # required by sec.gov, see below
python -m congress_trades all
open out/congress.html
```

### The first run is slow. Budget for it.

A fresh clone ships with **no data** — the database, the cached filings and the
rendered page are all gitignored, because they are rebuildable and large. So the
first run does real work:

| Step | Roughly | Why |
|---|---|---|
| `backfill` | a few minutes | one ZIP per year from the House Clerk, then a PDF per filing |
| `enrich` | a minute or two | roster, then one Wikipedia lookup per member |
| `sectors` | under a minute | SEC EDGAR, one lookup per unseen ticker |
| `prices` | **10–20 minutes** | one price series per ticker, ~1,300 of them, deliberately rate-limited |

`prices` is the long one — expect the cold `all` above to take 15-25 minutes — and
the scorecard and every return in the digest stay **empty until it finishes**.
Nothing is broken at that point; there is simply nothing to score yet.

It is a one-time cost. Filings are immutable and are never fetched twice, and each
price series is cached as it arrives and re-read once a day at most, so later runs
take seconds. Interrupting `prices` loses no downloads for the same reason — but
it does write no rows, since the whole pass commits as one transaction. Re-run it
and it replays from the cache in a fraction of the time.

Individual steps:

```bash
python -m congress_trades backfill              # collect filings
python -m congress_trades enrich                # resolve members, bios, committees
python -m congress_trades enrich --rotate 20    # re-check only the 20 stalest
python -m congress_trades sectors               # SEC industry classification
python -m congress_trades votes                 # party unity + DW-NOMINATE
python -m congress_trades repair-tickers        # recover tickers the parser dropped
python -m congress_trades prices                # forward returns per disclosure
python -m congress_trades publish               # render the page
python -m congress_trades digest                # prompt-sized brief of the trends
python -m congress_trades scorecard             # rank members by record vs the index
python -m congress_trades backtest              # would following them have paid?
python -m congress_trades lag                   # alpha by how late it was disclosed
python -m congress_trades committees            # committee meeting dates (needs a key)
python -m congress_trades timing                # do they trade around their hearings?
python -m congress_trades timing --sector-matched   # ...counting only hearings on the industry traded
python -m congress_trades alerts                # only what crossed a bar since last run
python -m congress_trades mix                   # who is trading and who is parking
python -m congress_trades compliance            # filings past the STOCK Act's 45-day deadline
python -m congress_trades annual --fetch        # annual reports: debts, outside income, board seats
python -m congress_trades finance               # committee jurisdiction x PAC money x trades
python -m congress_trades lobbying --fetch      # LDA filings against the sectors members trade
python -m congress_trades judiciary --fetch     # federal judges' disclosed holdings

# model-backed, never part of `all`, configured in .env — see .env.example
python -m congress_trades llm                   # check the model, including JSON schema
python -m congress_trades advise                # send that brief to a model
python -m congress_trades resolve               # label the untickered assets
python -m congress_trades topics                # hearing titles, then tag them by industry

python -m congress_trades.portal                # or ./run.sh portal — all of the above in a page
```

### Keeping it current

Filings land on weekdays; member biographies barely change. A reasonable split:

```cron
10 3 * * 1  cd /path/to/congress-trades && python -m congress_trades backfill --quiet
10 4 * * *  cd /path/to/congress-trades && python -m congress_trades enrich --rotate 20 --quiet && python -m congress_trades publish
```

`--rotate N` re-checks the N least-recently-updated members, so a daily run cycles
the whole roster over about a week while staying well inside Wikipedia's rate limit.

## Flags

```bash
python -m congress_trades flags           # also the Flags tab in the portal
```

One row per member with four facts side by side, each from the report that owns
its rule: trades filed past the 45-day deadline, trades in sectors their own
committees oversee, PAC money from those sectors, and large trades ($100k+) no
other member made in that ticker within 30 days. There is deliberately no
combined score — the four are not on a common scale, and a ranked "most
suspicious" list would claim more than disclosure data can. Rows are ordered by
how many flags apply, and one instance is enough to raise a flag, so read the
numbers rather than the count.

## Alerts feed

Every alert the nightly run records is also kept with its text, reasons and a
link to the filing, and served as RSS:

```bash
python -m congress_trades alerts --rss > alerts.xml    # the last 100, newest first
```

The portal serves the same feed at `/feed.xml` and advertises it in the page
head, so pointing a feed reader at the portal's address finds it. Over
Tailscale (see "Reading it from your phone") that gives any phone feed reader
the alerts without the notification stack. The feed starts empty and fills as
alerts are recorded; `--dry-run` runs never add to it.

## Watchlist

Follow members, and tickers you own. Every trade they touch raises an alert at
any size, gets its own section at the top of the digest, and can be filtered in
the portal's Explore tab:

```bash
python -m congress_trades watch add ticker AAPL MSFT NVDA   # what you hold
python -m congress_trades watch add member pelosi            # any part of a name
python -m congress_trades watch rm ticker MSFT
python -m congress_trades watch                              # list
```

Or use the portal's **Watchlist** tab, and the watch buttons in any member or
ticker panel. The list lives in your database (`data/`, not in git), and the
static page never includes it, so sharing that page shares nothing personal.

## Member scorecard

Who is actually good at this, ranked, both directions:

```bash
python -m congress_trades scorecard                  # every qualifying member
python -m congress_trades scorecard --limit 20        # top 20 only
python -m congress_trades scorecard --horizon 30      # 30-day windows
python -m congress_trades scorecard --json
```

Also the **Scoreboard** tab on the rendered page, with each member's best and worst
single call written out ("sold WMB on 2026-07-21; it then fell 18% over 90 days
while the market did +4%").

Scoring, in both directions, against SPY over the identical window:

```
buy   alpha = stock − benchmark        they chose to hold it
sell  alpha = benchmark − stock        they chose not to, and the index was the
                                       alternative, so a name that then lagged
                                       the market is a sell that paid
```

Two benchmarks, side by side. **vs index** is SPY. **vs sector** is the trade's own
sector ETF, and it separates picking a stock from picking a sector: buying
semiconductors through a semiconductor rally beats SPY without having chosen
anything, but against SOXX the same trades read flat.

The gap between the two columns is where most of the apparent skill lives. Across
the 72 scored members, the median record is **+1.22% vs the index but only +0.54%
vs sector** — over half the edge is sector exposure, not selection. Most positive
members shrink when measured against their own sector, and several go from
positive to zero-or-negative: they rode the sector outright.

The sector mapping (`SECTOR_ETF` in `prices.py`) is a judgement call, not a
definition — these labels are SIC rollups and some buckets straddle two ETFs.
Unmapped sectors fall back to SPY. One case is outright wrong and worth knowing:
funds inherit their sponsor's SIC code, so a spot bitcoin trust files under
"Commodity Contracts Brokers & Dealers", rolls up to Banking & Finance, and gets
benchmarked against banks. **Sector alpha on a fund holding is noise** — read that
column only for operating companies.

The benchmark is the whole point. Over a 90-day window where SPY returned +7%, a
member whose buys returned +6% was *behind the market*, and every member looks
like a genius if you quote raw returns. Both legs are read at the same calendar
dates, so a holiday shifts the trade and the benchmark together.

Everything is measured from the **disclosure** date. Trade-date returns would
flatter these members considerably and mean nothing, because nobody outside the
filing knew until the filing.

What the ranking still cannot tell you, even benchmarked: windows overlap, the
set is dominated by a few prolific filers, amounts are brackets, and a median
across trades is not a portfolio return. `MIN_TRADES` is enforced because below
about ten measurable trades a "record" is one lucky quarter.

## What is actually being disclosed

```bash
python -m congress_trades mix
python -m congress_trades repair-tickers --dry-run
```

Of 33,128 disclosures, **27,424 resolve to a listed equity** and 5,704 genuinely
do not. The remainder is worth naming rather than discarding — a member whose
filings are mostly Treasuries is parking money, and no alpha figure conveys that:

| class | share |
|---|--:|
| Listed equity | 82.8% |
| Government & municipal debt | ~3% |
| Treasuries, cash & money market | ~3% |
| Corporate debt, structured notes | ~1% |
| Partnerships, private & pre-IPO, hedge funds | ~2% |
| Options, crypto, funds, real estate | ~2% |
| Other / unlabelled | ~6% |

**4,506 of those were equities all along.** The Clerk's newer template writes
"Apple Inc. - Common Stock (AAPL)" with no asset-type code after the ticker, and
the original pattern required one, so thousands of ordinary trades were stored
untickered and filtered out of every analysis. `pdftotext` also renders some
capitals in lower case — "(bLK)", "(CAg)", "(TSlA)" — which defeated a
case-sensitive match. Recovering them raised measurable trades 19%, and every
number in this README moved as a result.

Nothing is invented. A recovered candidate is accepted only if it matches a
ticker already evidenced by EDGAR or another filing, or — for symbols never seen
before — only if a price series actually exists for it. Of 301 unseen candidates,
249 were real and 52 were rejected.

Classification uses the filings' own taxonomy rather than keyword guesses:
`[ST]` stock, `[GS]` government and municipal, `[OP]` options, `[CT]` crypto,
`[PS]` private equity, `[HN]` hedge fund. That matters because municipal debt is
written "Los Angeles CA GO UTX [GS]", which no amount of matching on "general
obligation" will catch. The specific codes are authoritative; the broad ones
(`[CS]`, `[OT]`) yield to name patterns, since `[CS]` covers both a Goldman
medium-term note and a money-market fund.

## Does any of it work?

Three checks, because a ranking that cannot be falsified is a horoscope.

```bash
python -m congress_trades scorecard        # includes the halves + persistence r
python -m congress_trades backtest         # out-of-sample, with error bars
python -m congress_trades backtest --split 2025-10-01   # try another cut
```

**1. Split each record in half.** Every member is scored on the older half of their
own history and the newer half separately. The scoreboard shows both.

**2. Ask whether the halves agree.** Across members, the rank correlation between
first-half and second-half alpha is the single most useful number this project
produces. On six years of filings (72 members with a scoreable record) it comes
out at **r = −0.06**, with 53% of members keeping the same sign — a coin flip. A
member's past alpha says nothing about their next trade. The page says so above
the table rather than in a footnote.

It has been near zero at every data size: r = +0.03 on 18 months and 29 members,
−0.09 on six years and 65, −0.06 after recovering 4,500 mis-parsed trades and
reaching 72.

**3. Concentration.** One member near the top of the alpha ranking has 89% of
their scored trades in a single bitcoin ETF. That is one bet with a sample size of
one, not a 27-trade record, so any member whose top ticker exceeds half their
trades is flagged as **one bet** on the scoreboard — currently 4 of 65.

### The backtest

`backtest` ranks members using disclosures *before* a cut date and grades them only
on disclosures *after* it, so selection never sees the period being measured —
which is the exact error checks 1 and 2 exist to catch. Positions are equal-weight,
held 90 days from the disclosure date, scored as alpha vs SPY.

`--walk-forward` re-ranks every year and grades on the next, pooling all folds, so
every year is a test year exactly once and the answer does not hinge on one cut.

It reports two 90% bootstrap intervals, and the difference between them is the
point. The **naive** one resamples individual positions. The **by month** one
resamples whole calendar months, and it is the one that decides significance:
hundreds of these disclosures land in the same few weeks and ride the same market,
so treating positions as independent counts one regime as thousands of
observations. On six years the naive interval calls "every disclosure" a
significant +0.8% (+0.3% to +1.2%); clustered by month, the same number reads
−0.0% to +1.5% and the result evaporates. Same for the bottom-members row.

**One strategy clears zero, and it is the one that selects nothing.** Pooled over
five walk-forward years, following *every* disclosure returns **+0.85%** per
90-day position with a clustered interval of **[+0.07%, +1.59%]** on 4,214
positions. It holds at a 30-day horizon too: +0.45% [+0.10%, +0.78%]. Earlier,
with 19% less data, the same figure read [−0.0%, +1.5%] and did not clear.

Read it at its true size before getting excited. It is a mean per position, not a
compounded return; there are no costs, slippage or taxes in it; the beat rate is
54%, barely off a coin flip; and per fold it is positive in only three of five
years, leaning on 2025 (+1.73%, the largest fold) with 2022 and 2026 negative.
What it describes is owning a slice of everything Congress discloses — closer to
a broad diversified tilt than to a stock-picking edge, and roughly what the
public congressional-trading ETFs already do.

**Member selection still adds nothing.** Top-ranked members pool to +1.0% with an
interval spanning zero, their buys are worse than their sells, and the bottom-
ranked row is not reliably worse either. That is what r = −0.06 looks like in
practice: the aggregate carries a little, the ranking carries none.

Caveats the code states rather than hides: a sell is only actionable as a short and
shorting is not frictionless (no borrow costs or availability modelled, so those
rows are an upper bound); the reported figure is average position alpha, not a
compounded equity curve; and months are a crude cluster — overlapping
90-day holds still correlate across adjacent months, so a quarterly block
bootstrap would be stricter still.

## Filing lag

```bash
python -m congress_trades lag
```

The STOCK Act allows 45 days, and 3,177 of the measurable disclosures here are
past it. The obvious hypothesis is that members file their winners late. **It is
wrong, and the reverse is the only thing in this project that survives a
clustered interval:**

| filing lag | disclosures | median α vs index | 90% by month | |
|---|--:|--:|:--:|---|
| 0–15 days | 4,362 | **+0.8%** | +0.4% to +1.7% | significant |
| 16–30 days | 9,144 | +0.0% | −0.5% to +0.4% | |
| 31–45 days | 6,350 | **+0.7%** | +0.1% to +1.3% | |
| 46–90 days | 982 | −0.6% | −2.0% to +0.3% | |
| 91–365 days | 1,566 | **−1.7%** | −2.0% to −0.5% | significant |
| 366+ days | 1,380 | +0.0% | −2.5% to +0.2% | |

Within 45 days: **+0.4%** [+0.1%, +0.8%]. Past 45 days: **−0.6%** [−1.7%, −0.4%].
Opposite signs, both significant. Note the 16–30 day bucket is flat while both
neighbours are positive — a non-monotone shape that argues against reading a
clean mechanism into the gradient.

Read the mechanism before reading skill into it. Every return is measured from the
**disclosure** date, so a trade filed a year after execution is scored on the stock
a year after the member acted. Negative alpha in the late buckets mostly says the
disclosure had nothing actionable left in it — not that the member traded badly.
The useful direction is the other one: promptly disclosed trades are the subset
still worth looking at.

It is not a member trait either. Within each of 109 members' own histories, the
rank correlation between filing lag and alpha is **r = −0.02** with 45% positive.
Later-filed trades are not that member's better trades; the population gradient is
about which disclosures are fresh, not who files late.

This command defaults to `--floor 1` rather than the display floor: the $15k floor
keeps rebalancing noise off the page but costs four fifths of the sample, and here
the lag is what is being measured, not the trade size.

## Committee timing

The digest's committee-overlap flag only says a member traded a sector their
committee oversees, which is unsurprising — people invest in what they know. The
testable claim is about **timing**: did the trade land just before a meeting that
committee held?

```bash
python -m congress_trades timing       # works immediately, no key needed
```

**No signup required.** A snapshot of 9,639 meetings (2021-01-06 to 2026-09-29)
ships in `seed/committee-meetings.json.gz` — 58 KB, public-domain government data.
`timing` loads it automatically when the table is empty, so a fresh clone can
answer the question without a key or a two-hour fetch. Titles are omitted
deliberately: the analysis reads only date, type and committee root, and titles
were five sixths of the file.

**A key extends it.** Meetings that have already happened are final, so only the
tail goes stale. With a key, a run fetches just the gap:

```bash
export CONGRESS_API_KEY=...            # free: https://api.congress.gov/sign-up/
python -m congress_trades committees   # skips what is stored; 8 fetches, not 9,646
python -m congress_trades committees --seed     # reload the snapshot, discarding local
python -m congress_trades committees --export   # rewrite the snapshot from your db
```

The first full collection is ~2 hours at the API's rate limit, which is exactly
why the snapshot exists.

9,644 meetings across congresses 117–119 put 17,965 priced trades within 30 days
of a meeting held by a committee that member sits on. Negative days mean the trade
came *before* the meeting.

Of nine buckets, one misses zero: −14 to −8 days, +1.2% median [+0.6%, +2.4%]. It
also survives a Bonferroni-corrected level for having tested nine buckets — but
only just, at [+0.08%, +2.98%]. Four reasons that is still not the insider thesis,
all printed with the table rather than left to the reader:

- **Returns are measured from the disclosure date, not the trade date.** A trade
  placed 10 days before a hearing has its 90-day window start whenever it was
  *filed*, up to 45 days later — the hearing is long past. This test locates trades
  relative to meetings; it cannot measure a return earned *through* one.
- **The gradient is the wrong shape.** A real information effect is strongest
  nearest the event and decays. Here the days immediately before a meeting (−7 to
  −1) are flat and a middle bucket is up. That argues noise.
- **Proximity is not jurisdiction.** The match is "sits on a committee that met
  near the trade", not "that committee had business with the company". Busy
  committees meet weekly.
- **Check the member columns.** The table reports how many members are in each
  bucket and what share its top three supply. A result carried by three people is
  a claim about three people.

The before/after split shows nothing at all: 7 days before +0.2%, after +0.2%,
further out −0.1%, every interval spanning zero.

Why the API is the fallback rather than a scrape: `docs.house.gov`'s calendar
redirects to an error page,
`senate.gov` publishes only the current week, and govinfo's CHRG sitemaps are
published transcripts whose coverage collapses recently — 183 packages for 2026
against ~1,300 for a completed year. Building on that would have meant testing
timing on a sample biased toward older trades.

## Alerts — speak only when something crosses a bar

```bash
python -m congress_trades alerts                   # marks what it emits as seen
python -m congress_trades alerts --dry-run         # look without recording
python -m congress_trades alerts --headline        # one line, for a push
```

Exits 1 on a quiet day, so a scheduled job can skip notifying entirely.
`examples/nightly-refresh.sh` is a working runner built around that:

```cron
30 5 * * *  /path/to/congress-trades/examples/nightly-refresh.sh
```

It collects, prices, re-renders, and always writes a dated note as a local
record — but runs the LLM and calls your notifier only on a day an alert fired.
Configure it by environment (`CONGRESS_NOTES`, `ALERT_WINDOW`, `ENV_FILE`, …);
the headline goes to `NOTIFY_CMD` on stdin, so it carries no home-automation
details of its own:

```bash
NOTIFY_CMD='mail -s "Congress trades" you@example.com'
NOTIFY_CMD='curl -sX POST -d @- https://ntfy.sh/your-topic'
```

Three orderings in it were learned the hard way and are commented as such:
publish is only reached if collection succeeded, so a bad night leaves
yesterday's page rather than replacing it with less; alerts are **read without
recording** first, because a recording call marks everything seen and the
headline query would then come back empty; and recording happens last, so a
crash leaves the backlog intact for the next run instead of swallowing it.

**What it will not alert on:** "a member with a good record just bought X". That
is the most tempting alert to build, and this project's own numbers say it is
noise — member alpha does not persist (r = −0.06) and top-ranked members
underperformed a follow-everyone baseline in four of five walk-forward years. An
alert on a track record would be dressing that up as a signal.

The bars are things the data supports, or that are simply facts:

| bar | why |
|---|---|
| **$500k+** | fires on size alone, rare enough to be worth it |
| **10× the member's own median** (and ≥ $50k) | unusual *for them*, not merely large |
| **$100k+ in their own committee's sector** | a fact about jurisdiction, not evidence |
| **4+ members on one name in 30 days** | convergence; re-fires only when the count rises |

Being filed promptly (≤15 days) or merely being $100k+ **annotates** an alert but
never raises one. The first version made freshness a trigger and produced five
alerts a day — most filings are prompt, so freshness says a disclosure is worth
*looking at*, not that it is remarkable. Likewise a 10× multiple means nothing if
a member's median trade sits at the reporting floor, hence the absolute floor
alongside it.

Every alert fires once; fingerprints live in the database, so re-running on the
same day is silent and this is safe on a timer. A member's same-day disclosures
are grouped in the output — one filing can carry twenty qualifying transactions
and printing each buries everything else — while fingerprints stay per-trade so
nothing is missed. Over six years of filings this averages well under one alert a
day.

## The portal

`publish` renders three views into one static file. Everything else — the
backtest, the filing-lag curve, the committee-timing test — only ever reached a
terminal. The portal serves all of it on localhost, with no new dependencies:

```bash
./run.sh portal            # http://127.0.0.1:8777
./run.sh portal stop       # or press Stop portal in the page
```

Reports run as subprocesses rather than imports, so what the page shows is
byte-identical to what the CLI prints, and a crash in one report cannot take the
server down. Every report's options are declared in a table in `portal/jobs.py`;
nothing else from a URL is ever passed through to a subprocess.

### Where the code is

`congress_trades/portal/` is split by job: `server.py` (HTTP and the
same-origin guard), `jobs.py` (the report and job tables, and the one job
slot), `models.py` (the model picker), `api.py` (the JSON the app reads) and
`pages.py` (HTML, including the pages that work without JavaScript). The front
end is ordinary files in `congress_trades/web/`: `portal.*` for this app,
`page.*` for the static page `publish` writes, and `common.js` for the helpers
both use. `./run.sh test` runs every test and module selftest.

### Sharing it read-only

```bash
./run.sh portal --read-only --port 8780          # beside your own on 8777
./run.sh portal stop --port 8780
```

A read-only portal refuses every write (refresh, jobs, stop, the model form,
watchlist changes) and hides your setup: no Maintenance tab, no model settings,
no live model check, and no watchlist -- not in the API, and not in the reports
or the alerts feed either, since a watchlist of what you hold is personal. Each
port keeps its own pid file, so the two portals start and stop independently.
It still binds to `127.0.0.1`; to share it, put that port behind Tailscale as
below, or behind a reverse proxy you control.

### Reading it from your phone

The portal binds to `127.0.0.1` only, deliberately: it has no login, and its
Maintenance tab starts jobs. To read it from your own devices, put it behind a
private network rather than opening a port. With Tailscale:

```bash
tailscale serve --bg --https=8777 8777   # https://<this-machine>.<tailnet>.ts.net:8777/
tailscale serve status
tailscale serve --https=8777 off         # stop serving just this one
```

On Linux, `serve` needs root unless you once run
`sudo tailscale set --operator=$USER`. Give it its own port as above. A bare `tailscale serve --bg 8777` takes the
root of port 443, replacing anything that machine already serves there, and
`tailscale serve reset` clears every mapping, not just this one. A sub-path
(`--set-path`) does not work either: the portal's links and API calls are
absolute.

Everything reads normally that way. The model form will refuse to save or list
models, because it only accepts requests addressed to `127.0.0.1` or
`localhost`. That's the guard that keeps another site from redirecting your API
key, so change models at the machine itself. Anyone on your tailnet can reach
the jobs and the Stop button, so do not share the node.

### Maintenance, from the page

The **Maintenance** tab runs the jobs that write, so the model-backed work does
not have to be driven from a terminal:

| | |
|---|---|
| **Model** | Pick the provider and model `advise`, `resolve` and `topics` will use — Anthropic, OpenAI, Google Gemini, xAI, Mistral, DeepSeek, Groq, OpenRouter, Together, a local Ollama, or a local OpenAI-compatible server (LiteLLM, LM Studio, vLLM). **List models** asks the provider what it serves; **Save** writes `.env` (mode 600) and applies to the next job started here. Also shows whether `CONGRESS_API_KEY` is set and how many meetings have titles. **Check model** does the live round trip described above, including the JSON-schema step. |
| **Refresh data** | `all` — collect, enrich, classify, price, render. |
| **Resolve untickered assets** | `resolve --apply`. |
| **Fetch hearing titles** | `topics --stage fetch`, with the start date as a field. |
| **Tag hearing titles** | `topics --stage tag`. |
| **Audit the parser** | `parser-qa --sample 25` — hand 25 cached filings to a model with the rows the parser produced and ask what it missed. Writes only its own findings table; it never edits a trade. |

Each of these writes to the database, so **only one runs at a time** and they
share the slot with a refresh: `prices` holds one sqlite write transaction open
for its whole pass, and a second writer would die with "database is locked"
partway through. Starting a second job is refused with the name of the one
already running rather than queued, because these take minutes to hours and a
queued job would surprise whoever started it later.

The model form is guarded the way a page holding an API key has to be: it accepts
only requests from the portal's own page (same `Host`, same `Origin`, a JSON body),
so another site open in the same browser cannot point your key somewhere else.
Cloud endpoints are fixed in `portal/models.py` and never taken from the request; only the
local and custom entries accept a URL. The key is never sent back to the page.

A job whose prerequisites are missing is disabled with the reason on the button —
no model configured, no API key, no titles fetched yet — rather than failing a
few minutes in. The buttons are plain forms, so the page works without
JavaScript; the single-page app posts to the same endpoints.

Progress appears in the bar at the top of every page, naming whichever job is
running. There is no cancel button: stopping the portal stops the job with it.

The **Committee timing** report has a `sector-matched` tick-box, which is the
narrower arm described above.

Two read-only reports come with those jobs. **Parser QA** runs the free half of
the audit — the exact scan over every cached filing — and shows what the sampled
model audits have found so far; it is declared preformatted in the report table,
because its aligned columns *are* the output and a markdown pass would collapse
them. **Committee jurisdiction** shows the committed table. Regenerating that
table is deliberately not a portal job: the point of `--generate` is reading the
diff, and the portal keeps only a job's last line of output.

## An MCP server, for agents

```bash
python -m congress_trades.mcp_server        # JSON-RPC over stdio
```

Five tools: `query_trades`, `member_scorecard`, `run_backtest`, `filing_lag`,
`digest`. Register it with any MCP client — for Claude Code:

```bash
claude mcp add congress-trades -- python3 -m congress_trades.mcp_server
```

The point of it is not query access, it is **guardrails**. 33,000 rows sliced
freely is a multiple-comparisons machine: an agent that can cut the data fifty ways
will find a member who beats their sector 80% of the time on twelve trades and
report it as a finding. So:

- Every response carrying a return also carries its sample size and a
  month-clustered 90% interval, and computes `significant` itself rather than
  leaving it to the caller's judgement.
- A slice under 8 measurable trades **refuses to average at all** and says why.
- A slice with enough trades but too few calendar months to resample returns an
  explicit warning that the figure must not be quoted as a result.
- `run_backtest` strips the naive per-position interval before replying, so only
  the clustered one is quotable.
- The server sends its findings-so-far as instructions on `initialize`: alpha does
  not persist, two thirds of the edge is sector, no strategy clears zero. An agent
  is told not to re-derive those as news, and to say how many slices it tried.

## Feeding it to an AI

`digest` compresses the database into roughly 90 lines an LLM can read in one
prompt — the same aggregates the Movers view computes in JS, plus forward returns
and a per-member track record:

```bash
python -m congress_trades digest --days 90            # markdown
python -m congress_trades digest --days 90 --json     # same numbers, machine-readable
```

Sections: **convergence** (ranked by distinct members on one name, because several
members independently landing on the same mid-cap beats one large index buy),
**sector net flow**, **lone large positions** (one member alone, sized against
that member's own median trade), **committee overlap** (a trade in a sector the
member's own committee has jurisdiction over), and **track record** (share of
closed 90-day windows that moved the way the member traded).

`advise` posts that digest to a model. Three providers, picked with
`CONGRESS_LLM_PROVIDER` — or from the portal's Maintenance tab, which writes the
same variables:

```bash
# a local Ollama — its native API, not the /v1 shim
export CONGRESS_LLM_PROVIDER=ollama
export CONGRESS_OLLAMA_MODEL=qwen3.8:27b            # required; `ollama list` shows yours
export CONGRESS_OLLAMA_BASE=http://127.0.0.1:11434  # default
export CONGRESS_OLLAMA_NUM_CTX=8192                 # default

# or Claude, through the official SDK (pip install anthropic)
export CONGRESS_LLM_PROVIDER=anthropic
export CONGRESS_LLM_MODEL=claude-opus-5-5           # required
export CONGRESS_LLM_KEY=sk-ant-...                  # or skip it: see "Signing in" below

# or any OpenAI-compatible /chat/completions endpoint —
# OpenAI, Google Gemini, xAI, Mistral, DeepSeek, OpenRouter, Groq, Together,
# LiteLLM and vLLM all speak it
export CONGRESS_LLM_PROVIDER=openai                 # default
export CONGRESS_LLM_BASE=http://127.0.0.1:4000/v1   # default; any compatible host
export CONGRESS_LLM_MODEL=reason                    # required
export CONGRESS_LLM_KEY=...                         # if the endpoint wants one

python -m congress_trades advise --days 90
python -m congress_trades advise --dry-run          # print the prompt, call nothing
```

#### Signing in to Anthropic instead of pasting a key

Anthropic's CLI, `ant`, signs you in through the browser and leaves a profile the
SDK reads on its own, so no key has to sit in `.env`. It is still your API account
and its usage billing — a Claude Pro or Max subscription cannot power third-party
tools, and the same goes for ChatGPT and Gemini consumer plans.

```bash
pip install anthropic                     # the SDK this provider uses

# install ant: macOS
brew install anthropics/tap/ant
# Linux: the release tarball (see github.com/anthropics/anthropic-cli/releases)
V=1.38.0; curl -fsSL "https://github.com/anthropics/anthropic-cli/releases/download/v$V/ant_${V}_linux_amd64.tar.gz" \
  | tar -xz -C ~/.local/bin ant

ant auth login                            # opens the browser; --no-browser on a headless box
ant auth status                           # which credential won
```

Then choose Anthropic in the portal and leave the key blank, or set only
`CONGRESS_LLM_PROVIDER=anthropic` and `CONGRESS_LLM_MODEL`. An exported
`ANTHROPIC_API_KEY` outranks the login, even when empty, so unset it if the login
seems ignored. Refresh tokens eventually expire; when a working setup starts
failing authentication, run `ant auth login` again first.

To check what you configured actually works — including the part `advise` does not
need but `resolve` and `topics` depend on:

```bash
python -m congress_trades llm
```

It prints the resolved settings, confirms the endpoint answers and the model is
pulled, does one plain completion, and then one constrained by a JSON schema with
an enum. That last step is the one worth running after any change: an endpoint
that silently ignores a schema still answers, plausibly, and the damage shows up
much later as labels outside their vocabulary. `--selftest` does the shape checks
without calling anything.

Ollama also answers the OpenAI shape on `:11434/v1`, so the second form works
against it too. The native client exists because the batch commands below need
three things the shim does not give: a context size set per request (the shim
serves the Modelfile's `num_ctx`, often 4096, and silently drops the overflow),
decoding constrained to a JSON schema so a label cannot fall outside its
vocabulary, and `think: false` for models that would otherwise spend the budget
reasoning about a one-word answer.

### Resolving what the parser could not

`repair-tickers` recovers a symbol the filing spelled out in brackets. What is
left after it is 382 distinct asset names no pattern reaches, and they are two
different piles under one label: instruments that need world knowledge
(`ATHERTON MICH CMNTY SCH` is municipal debt, `BANK AMERICA CORP SER N MTN` is
corporate debt), and ordinary listed equities whose filing never wrote the
symbol (`Analog Devices`, `AMD`, `BRK-B - Berkshire Hathaway Inc Class B`).
The second pile is excluded from every return, scorecard and backtest here,
because an untickered row cannot be priced.

```bash
python -m congress_trades resolve --dry-run    # show the proposals, write nothing
python -m congress_trades resolve              # store labels; no ticker writes
python -m congress_trades resolve --apply      # also write verified tickers to trades
python -m congress_trades resolve --reverify   # re-run the gates, calling no model
```

The model's raw proposal is stored alongside the verdict, so `--reverify` replays
the gates over what it already said. The gates are the part that keeps changing —
each real run turned up another way for a plausible symbol to be wrong — and
re-asking a model to re-test a rule it has no part in would be slow,
non-deterministic, and would confuse a gate change with a different answer.

A wrong ticker is far worse than no ticker: it gets priced, scored and
attributed to a member as a trade they never made. Asked about a Birmingham
bond during development a model answered `BIRMINGHAM ALA GO WTS SER. 2018`;
asked about `GOLDMAN SACHS GROUP INC` it answered `GOOGL`. So nothing the model
says about a symbol is trusted, only treated as a candidate, and four gates
stand between a candidate and the trades table:

1. the label has to be an instrument that *has* a symbol — a bond issued by a
   listed company is still a bond;
2. the string has to be shaped like a symbol;
3. SEC's own `company_tickers` file has to register it, and SEC's name for it
   has to match the filing text specifically — one generic word like
   "Financial" in common is not identification, and a finance subsidiary is
   refused outright, because `General Motors Financial Company` is not
   `General Motors Co` however alike the names read;
4. the price source has to return a real series for it.

A symbol the filing itself wrote is the exception to gate 3: that is the
filing's own assertion, the same evidence `repair-tickers` accepts, and the
model only noticed it. Everything refused is kept with the reason, which is the
most useful thing in the table for judging whether the pass is working.

Labels are held to a lower bar — they are stored in `asset_labels`, never merged
into the trades table, and `mix` consults them only for rows the deterministic
classifier gave up on. The filing's own asset-type code and the keyword patterns
stay authoritative. Drop the table and you are back to parsed fact.

### What each hearing was about

`timing` locates a trade relative to any meeting of any committee the member
sits on, and its own caveat says why that is weak: proximity is not
jurisdiction. It is weaker than it sounds — **94% of priced trades by a member
with a seat fall within 30 days of one of their own committee's meetings**,
because busy committees meet weekly. A treatment group of almost everybody
cannot show anything.

The fix needs subject matter, which means meeting titles, and the shipped
snapshot omits them on purpose. Two stages:

```bash
python -m congress_trades topics --stage fetch --since 2025-01-01   # needs CONGRESS_API_KEY
python -m congress_trades topics --stage tag                        # needs a model
python -m congress_trades timing --sector-matched                   # the narrower arm
```

Fetching measures about 0.95s per meeting on an idle machine — roughly 45 minutes
for 2025 onward, two and a half hours for the full back catalogue. It is resumable
and cached on disk forever, because a meeting that has happened never changes, so
an interrupted run costs nothing to restart. `--since` limits it to the years you
actually score. Don't run it beside `tag`: a loaded local model slows the fetch
several times over.

Tagging asks a model which industries a hearing bears on, from the same
vocabulary `ticker_sectors` uses, so a tag can match a trade. It must be able to
answer *none*: most meetings are nominations, budgets, agency oversight or
procedure and touch no traded industry at all. A tagger that found a sector in
everything would rebuild the exact dilution this exists to remove, so the run
warns if more than 60% of meetings come back with one.

Then `timing --sector-matched` counts a meeting only where its subject overlaps
the industry of the company traded.

On the 2025-onward run here, 2,732 titles tagged with `qwen3.8:27b` put 535
meetings on some industry and 2,197 on none, and the treatment group fell from
8,755 trades to 782 — from nearly every eligible trade to about one in eleven,
which is the dilution this exists to fix. Nothing in that arm has an interval
that misses zero, **but its medians are not smaller than the wider arm's — in
several buckets they are larger.** That is lost power, not a refuted effect, and
the report says so rather than letting a null at n≈84 read as absence. The
period is not the explanation: restricting the wider arm to the same 2025+ window
leaves both of its corrected-significant buckets intact.

Note also that this is a **second family of
tests**: the Bonferroni correction inside the report covers the buckets within
one arm, not the choice between arms. Running both and reporting whichever looks
better is precisely the failure that correction exists to stop.

### Checking the brief against the digest

The system prompt shapes what `advise` writes. Nothing read the result, which
left the failure this project actually worries about unmeasured: a brief that
reads exactly like every other one while citing a ticker, a figure or a ranking
the data never supported.

```bash
python -m congress_trades advise --check
```

The brief prints as usual, then a second pass audits it against the digest it
was built from. Two halves, deliberately unequal:

**Tickers and figures are checked by string, not by a model.** A symbol that
appears nowhere in the digest was supplied by the model, and a comparison of
text cannot itself hallucinate. On the first live run this caught
`WMB (iShares Core MBS ETF)` — WMB is Williams Companies, and the fund name was
invented wholesale.

**Everything else is judgement**, so a model is asked and then held to the same
bar `resolve` holds a proposed ticker to: every finding must quote the draft
verbatim, and a finding whose quote is not in the draft is discarded and
counted. A model asked to find fault will find some, and an invented quotation
is how an audit starts manufacturing the very thing it exists to catch.

It reports the kinds that matter here — a ranking presented as predictive when
persistence is ~0, a bracket quoted as a position size, alpha quoted as skill,
a recommendation without its disconfirming note — and it never edits the brief.

The first version of the auditor flagged six claims that were the draft
correctly hedging (*"this could be rebalancing"*), which the `advise` prompt
explicitly requires. Speculation offered as an alternative is now excluded by
name: a false positive in an audit is worse than in most places, because it
teaches the reader to skim the findings.

### Which industries a committee oversees

`committee overlap` needs to know what a committee has jurisdiction over. That
used to be seventeen hand-written rows matched as substrings against whatever
name a seat carried, and its own docstring called it *editorial, not official*.
Two things were wrong beyond the admission:

- **It matched the wrong string.** A seat's name is the full committee name for
  a full committee but a bare label for a subcommittee — and those repeat.
  Three different committees have a subcommittee called *Health*, three more
  have one called *Energy*. A substring test cannot tell them apart, and it gave
  Appropriations' *Homeland Security* subcommittee a jurisdiction the list had
  deliberately withheld from Appropriations.
- **Seventeen needles never covered the 221 committees and subcommittees**
  members actually sit on. Everything unlisted silently contributed nothing.

So the table is generated once, reviewed, and committed as data in
`seed/committee_sectors.json`. Nothing calls a model at analysis time —
`sectors_for_seat` is a dict lookup keyed on the committee id.

```bash
python -m congress_trades jurisdiction              # the committed table
python -m congress_trades jurisdiction --generate   # ask a model, diff, commit nothing
python -m congress_trades jurisdiction --write      # commit what --generate proposed
```

`--generate` parks its raw answer in the cache so `--write` promotes it without
asking again: asking twice could return something other than what you reviewed,
which would make the review meaningless.

The model does not get the last word. Three rules are applied to its answer,
because each is a judgement about this project rather than about jurisdiction:

- **The broad committees stay empty** — Appropriations, Budget, Rules, Ethics,
  House Administration, Oversight, Foreign Affairs. Their reach is so wide that
  tagging them flags nearly every trade. That was the original list's deliberate
  omission, kept as a rule applied to the answer rather than as a request in a
  prompt.
- **No committee keeps more than five sectors.** More than that is a wildcard,
  not a jurisdiction.
- **"Unclassified" is never a jurisdiction.** It marks a ticker whose SIC lookup
  failed, and letting it through would match every trade the lookup missed.

Reviewing the diff is the point, and it earned its place immediately. Both
generating runs produced bodies tagged *Agriculture* whose own justification was
about something else — *"Defense procurement and military equipment oversight"*.
`Agriculture` is the vocabulary's first value alphabetically, and constrained
decoding has to emit *something* from the enum, so a model that cannot map its
reasoning onto the vocabulary falls back to the first one. Every such tag came
back at **low confidence** and none of them was correct, so low confidence is now
dropped rather than discounted.

One survived at *high* confidence — *Space and Aeronautics → Agriculture*,
justified as "NASA oversight and space policy" — and is recorded in `OVERRIDES`
with its reason, so a regeneration applies the correction again instead of
losing it. Aerospace is SIC 372/376, which rolls up to Transportation Equipment:
the same sector a trade in one of those companies would carry.

The result: **68 of 221 bodies carry a jurisdiction.** The other 153 carry none,
which is the correct answer for appropriations, budget, rules, ethics,
administration, oversight and foreign affairs. It is still editorial, and the
committed file says so in its own header. It exists to prompt a look, never as
a finding.

### Does the parser still read the filings?

Every number here rests on `parse_house_ptr` reading a PDF that `pdftotext`
mangled first, and the mangling is severe — headings collapse to bare letters,
`Filing Status: New` comes out as `F      S      : New`, and a page break drops a
repeated column header into the middle of a transaction. Commit `19d6694` is
what that costs unnoticed: **4,506 equity trades parsed as untickered**, excluded
from every return, scorecard and backtest, and described in this README as
"municipal bonds, notes, funds — wealth preservation" until someone looked.

Nothing would have caught that but reading the filings. So this reads them:

```bash
python -m congress_trades parser-qa --scan        # the exact pass, all filings, no model
python -m congress_trades parser-qa --sample 25   # ...and ask a model about 25
python -m congress_trades parser-qa --doc 20030803
```

Two passes that fail differently:

**`scan` uses no model.** For each cached text it counts the transaction headers
the parser's own pattern finds and compares that to the rows the parser
returned. The pattern is how the parser locates a transaction, so a text where
it matches more often than rows came back is a text the parser is dropping from.
Exact, free, and it runs over all 910 cached filings.

**`audit` hands a model the raw text beside the rows the parser produced** and
asks one question: which transactions here are not in that list? This is the
half that sees what the pattern cannot, because `scan` counts with the same
pattern the parser uses and is blind by construction to a layout that pattern
never matches.

A model asked "what is missing" will always find something, so nothing it claims
is believed. Each claimed row is checked back against the document — the date it
cites must be in the text, the asset it names must be in the text, and it must
not already be among the parsed rows — and only survivors are reported. Refuted
claims stay in the count, because a pass that claims forty misses and verifies
none has told you about the model, not about the parser.

This is deliberately **not a pipeline stage**. It reads the cache, writes only
its own `parser_audits` table, never edits a trade, and no collection run
depends on it. A regression net, not a parser.

**It found two live bugs on its first run, and both are now fixed.** Across the
910 cached filings they cost **306 transactions**, every one of them an
under-count — no wrong trade was ever recorded:

- **241 transactions in 90 filings, lost to block splitting.** The parser split
  the text on blank lines and took *one* transaction per block. But when a page
  break lands inside a transaction the Clerk reprints the column header there
  with no blank line, so two transactions share a block and the second was
  discarded. Filing `20033446` alone lost 23 of its 473. Found by `scan`.
- **65 transactions in 40 filings with type `E` (exchange).** The pattern
  matched only `[PS]`. The deterministic scan was blind to this by construction
  — it counts with the same pattern — and a model audit of 20 sampled filings
  surfaced it on filing `20035106`, where the parser returned one row and the
  document held two. Exactly the division of labour the two passes exist for.

Fixing the first also corrected **26 asset names**: reading the *first* line
before a match rather than the last picked up the previous transaction's
trailing `Filing Status: New` — mangled by pdftotext into `F S : New` — as the
asset name of 26 real trades, leaving them untickered and unscoreable.

Both passes now come back clean, and `tests/test_checks.py` carries all three
layouts so a regression says so.

### What this data cannot tell you

Read this before treating any output as a signal. It is also in the system prompt,
because a model left to itself will turn convergence counts into confident picks:

- **Filings lag the trade by up to 45 days.** Every return is measured from the
  *disclosure* date — the earliest a reader could have acted — not the trade date.
- **Amounts are brackets, not position sizes.** `$50k` means a reported range.
- **There is no market benchmark here.** A good hit rate in a rising market is not
  skill. Nothing is alpha-adjusted.
- **Many disclosures are spouse-directed or index funds** the filer never chose.
- Track-record rows under ~20 closed windows are noise, not a record.

Prices come from Yahoo's public chart endpoint (no key). Only the derived
per-disclosure returns are stored; the raw daily series is cached under `cache/`
and discarded, because ten years of closes for ~1,300 tickers is millions of rows
for the handful of dates that matter.

Self-check, no framework:

```bash
python -m congress_trades digest --selftest
```

## Configuration

All via environment variables; every one has a working default except the first.

| Variable | Default | Meaning |
|---|---|---|
| `CONGRESS_CONTACT` | *(unset)* | Email in the User-Agent. **sec.gov 403s without it.** |
| `CONGRESS_DB` | `./data/congress.db` | SQLite database |
| `CONGRESS_CACHE` | `./cache` | Cached filings, rosters and bios |
| `CONGRESS_OUT` | `./out/congress.html` | Rendered page |
| `CONGRESS_YEARS` | `2026,2025` | House filing years to collect |
| `CONGRESS_MIN_AMOUNT` | `0` | Bracket floor at *collection* time |
| `CONGRESS_DEFAULT_FLOOR` | `15001` | Floor the page *selects* by default |
| `CONGRESS_NUMBER` | `119` | Congress to score votes for |
| `CONGRESS_API_KEY` | *(unset)* | Congress.gov; meeting dates past the snapshot, and all meeting titles |
| `CONGRESS_LLM_PROVIDER` | `openai` | `openai`, `anthropic` or `ollama` |
| `CONGRESS_LLM_BASE` | `http://127.0.0.1:4000/v1` | provider `openai`: any `/chat/completions` host |
| `CONGRESS_LLM_MODEL` | *(unset)* | providers `openai` and `anthropic`: required |
| `CONGRESS_LLM_KEY` | *(unset)* | `openai`: bearer token, if wanted; `anthropic`: API key, optional after `ant auth login` |
| `CONGRESS_LLM_PRESET` | *(unset)* | written by the portal's model form; which entry it shows |
| `CONGRESS_OLLAMA_BASE` | `http://127.0.0.1:11434` | provider `ollama`: the native port, not `/v1` |
| `CONGRESS_OLLAMA_MODEL` | *(unset)* | provider `ollama`: required |
| `CONGRESS_OLLAMA_NUM_CTX` | `8192` | context per request; raise for long batches |
| `CONGRESS_OLLAMA_THINK` | `0` | leave off for labelling work |
| `CONGRESS_OLLAMA_KEEP_ALIVE` | `5m` | how long Ollama holds the model in memory |

A `.env` beside the README supplies any of these — copy `.env.example`.
It can hold API keys (the portal's model form writes them there, mode 600), so
keep it out of anything that syncs or backs up to a shared place, and never
commit it — it is in `.gitignore` for that reason. It is read
by `run.sh` and by `python -m congress_trades` alike, and a variable already
exported in your shell always wins over the file, so you can override it for a
single run.

Collection stores every disclosed bracket and the page filters for display, so you
can change the floor without re-collecting. If you raise `CONGRESS_MIN_AMOUNT`,
rebuild the table rather than merging — see the note on `row_idx` in `db.py`.

## Please be a good citizen

These are small government servers, not a CDN. The request pacing in this code is
deliberate, and tuned from actually being rate-limited:

- **SEC** — 0.15s between requests (their stated ceiling is 10/s) and a contact in
  the User-Agent. A bare URL in the UA gets you a 403.
- **Wikipedia** — 0.6s spacing with exponential backoff. At 0.15s it returns 429.
- **Senate eFD** — 0.6s between report fetches, single session, sequential.
  **Do not parallelize this.** It requires accepting a terms notice to get a session
  cookie, and hammering it is exactly the behaviour that gets scrapers blocked.

If you fork this, keep the throttles.

## Caveats worth knowing

- **Disclosures lag trades by up to 45 days.** The Movers view therefore windows on
  *disclosure* date — what became public in the period — while member timelines use
  the transaction date. A trade-date window would show almost nothing recent.
- **Options are listed but never scored.** A bought put is a bet the price
  falls, and a call's return is leveraged, so scoring either as a share trade is
  wrong. About 750 option trades appear everywhere trades do, with no alpha;
  `assets.is_option` decides, and callable bonds ("NOTE CALL MAKE") and covered-
  call ETFs are not options.
- **Amounts are brackets, not values.** Members report ranges (`$1,001 - $15,000`).
  Rankings use the lower bound. Nobody discloses an exact figure.
- **SIC is an old taxonomy.** It is authoritative and free, but classifies Apple as
  "Machinery & Computer Equipment" and Amazon as "Retail". Accurate for industrials,
  odd for megacap tech. The precise SEC label is kept alongside the rollup.
- **Committee jurisdiction is a heuristic.** The committee → sector table in
  `seed/committee_sectors.json` is editorial, generated once by a model from
  committee names and reviewed, not official rules. It exists to prompt a look,
  never to assert a finding. Broad committees (Appropriations, Budget, Rules,
  Ethics, House Administration, Oversight, Foreign Affairs) are deliberately
  excluded because they would match nearly everything. See
  [Which industries a committee oversees](#which-industries-a-committee-oversees).
- **The headline findings predate the parser fix.** The figures quoted through
  this README — 33,128 disclosures, the walk-forward interval, the timing
  buckets — were computed on a six-year backfill. The parser fix adds about 3%
  more House rows and corrects 26 asset names, so those numbers will move
  slightly when re-derived; re-running `backfill` over the full year range and
  then `prices` is what re-derives them. Every correction is an *under-count
  being fixed*, so a conclusion reversing is unlikely rather than impossible.
  Re-run on the two years this working copy holds, none did: the filing-lag
  curve kept every sign and every significance mark, and the before/after
  meeting split was unchanged in direction. One timing bucket (−14 to −8 days)
  did drop from corrected-significant to uncorrected-only, which is the kind of
  movement a bucket sitting on the threshold does when the sample grows.
- **Exchanges are not directional and are not scored.** The filings carry
  transaction type `E`, and the Senate writes "Exchange" outright; both mean a
  corporate action — a spinoff, a merger conversion — not a view on a price.
  They are stored and displayed, and `alpha()` returns `None` for them. They
  count toward neither side of a net flow.
- **Filings contain errors.** Real ones seen in this data: a transaction dated
  `12/26/2026` but notified in January 2026, and a notification date of `03/28/1935`.
  Rows are stored as filed; the House filing date is taken from the Clerk's index
  rather than the hand-typed PDF column, because a wrong disclosure date silently
  hides a trade.
- **No executive branch.** Cabinet and White House officials file comparable reports
  (OGE Form 278e / 278-T), and the index is public — but the documents themselves
  are request-only, so there is no automated path to their transactions.
- **"Votes with the president" is absent** because no free source publishes it. That
  is CQ's proprietary score. Party unity here is computed from raw Voteview roll
  calls: the share of a member's yea/nay votes matching their own party's majority.

## This is disclosure data, not advice

It reports what members told the government they did. It is not investment advice,
and committee service or a well-timed trade is not evidence of wrongdoing.

## Tests

```bash
python3 tests/test_parsers.py
python3 tests/test_config_and_sectors.py
python -m congress_trades digest --selftest      # aggregate invariants
python -m congress_trades scorecard --selftest   # alpha signs, ranking, concentration
python -m congress_trades backtest --selftest    # no-lookahead + CI sanity
python -m congress_trades lag --selftest         # bucket partition + sane lags
python -m congress_trades timing --selftest      # widening correction + buckets
python -m congress_trades alerts --selftest      # idempotence + no qualifier-only fires
python -m congress_trades mix --selftest         # class partition + no invented tickers
python3 tests/test_llm_and_resolve.py            # .env precedence, providers, ticker gates
python3 tests/test_checks.py                     # jurisdiction rules, brief audit, parser net
python -m congress_trades resolve --selftest     # every gate, no model and no network
python -m congress_trades topics --selftest      # tag vocabulary matches the trades'
python -m congress_trades jurisdiction --selftest # the rules a generated table must obey
python -m congress_trades advise --selftest      # ticker, figure and quote gates
python -m congress_trades parser-qa --selftest   # the scan, and 6 verification gates
python -m congress_trades llm --selftest         # provider settings, no model called
python -m congress_trades llm                    # LIVE round trip against your endpoint
python -c 'from congress_trades import mcp_server; mcp_server.selftest()'
```

Nothing in the test suite calls a model or touches the network. The model layer is
tested on its shapes — which variables each provider reads, and which proposed
tickers each of the gates refuses.

The parser tests are the ones that matter: they run against real filing layouts, and
they are what will fail first if the Clerk changes a PDF template or the Senate
changes its table markup. `tests/test_checks.py` carries the same filing layouts
that the parser audit found real losses in, including one that the deterministic
scan is blind to on purpose — so if `_H_TYPE` ever learns to match an exchange,
that test says so rather than quietly passing.

Beyond the parsers, the three passes that audit rather than produce are tested on
what they *refuse*: a jurisdiction the rules overrule, a quote the auditor
invented, a missing transaction the document does not corroborate. That is the
part that decides whether a model's answer reaches the data.

## Data licensing

Federal filings are public domain. Voteview asks that its data be cited — see
[voteview.com](https://voteview.com/about). This repository ships code that fetches
these sources, not the data itself.

## License

MIT — see [LICENSE](LICENSE).
