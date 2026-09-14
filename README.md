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
- **Movers view** — net buying by industry, the names the most members converged on,
  and the *lone large positions* a single member took that nobody else touched.
- **Filters** — chamber, time window, and a disclosure-size floor, applied live.

Everything is inlined into the output file. There is no server and no API.

## Data sources

| Source | Used for | Auth |
|---|---|---|
| [House Clerk](https://disclosures-clerk.house.gov/) | House PTR filings (yearly ZIP + PDFs) | none |
| [Senate eFD](https://efdsearch.senate.gov/search/home/) | Senate PTR filings | terms handshake |
| [unitedstates/congress-legislators](https://github.com/unitedstates/congress-legislators) | member identity, party, seat, committees | none |
| [Wikipedia REST](https://en.wikipedia.org/api/rest_v1/) | biography and portrait | none |
| [SEC EDGAR](https://www.sec.gov/) | ticker → SIC industry classification | contact in User-Agent |
| [Voteview](https://voteview.com/) | roll-call votes, party unity, DW-NOMINATE | none |
| [Yahoo Finance chart API](https://finance.yahoo.com/) | daily closes, for forward returns | none |

## Install

```bash
git clone <your-fork-url> congress-trades
cd congress-trades
pip install -r requirements.txt
```

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

The first run collects a couple of years of filings and takes a few minutes, mostly
fetching PDFs. Filings are immutable, so they are cached and never fetched twice —
later runs take seconds.

Individual steps:

```bash
python -m congress_trades backfill              # collect filings
python -m congress_trades enrich                # resolve members, bios, committees
python -m congress_trades enrich --rotate 20    # re-check only the 20 stalest
python -m congress_trades sectors               # SEC industry classification
python -m congress_trades votes                 # party unity + DW-NOMINATE
python -m congress_trades prices                # forward returns per disclosure
python -m congress_trades publish               # render the page
python -m congress_trades digest                # prompt-sized brief of the trends
python -m congress_trades advise                # send that brief to an LLM
```

### Keeping it current

Filings land on weekdays; member biographies barely change. A reasonable split:

```cron
10 3 * * 1  cd /path/to/congress-trades && python -m congress_trades backfill --quiet
10 4 * * *  cd /path/to/congress-trades && python -m congress_trades enrich --rotate 20 --quiet && python -m congress_trades publish
```

`--rotate N` re-checks the N least-recently-updated members, so a daily run cycles
the whole roster over about a week while staying well inside Wikipedia's rate limit.

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

`advise` posts that digest to any OpenAI-compatible `/chat/completions` endpoint —
OpenAI, LiteLLM, Ollama, vLLM, OpenRouter, Groq, Together all speak it, and
Anthropic models reach it through LiteLLM:

```bash
export CONGRESS_LLM_BASE=http://127.0.0.1:4000/v1   # default; any compatible host
export CONGRESS_LLM_MODEL=reason                    # required
export CONGRESS_LLM_KEY=...                         # if the endpoint wants one
python -m congress_trades advise --days 90
python -m congress_trades advise --dry-run          # print the prompt, call nothing
```

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
- **Amounts are brackets, not values.** Members report ranges (`$1,001 - $15,000`).
  Rankings use the lower bound. Nobody discloses an exact figure.
- **SIC is an old taxonomy.** It is authoritative and free, but classifies Apple as
  "Machinery & Computer Equipment" and Amazon as "Retail". Accurate for industrials,
  odd for megacap tech. The precise SEC label is kept alongside the rollup.
- **Committee jurisdiction is a heuristic.** The committee → sector map in
  `legislators.py` is editorial, matched on committee name, not official rules. It
  exists to prompt a look, never to assert a finding. Broad committees
  (Appropriations, Budget, Rules, Oversight) are deliberately excluded because they
  would match nearly everything.
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
```

The parser tests are the ones that matter: they run against real filing layouts, and
they are what will fail first if the Clerk changes a PDF template or the Senate
changes its table markup.

## Data licensing

Federal filings are public domain. Voteview asks that its data be cited — see
[voteview.com](https://voteview.com/about). This repository ships code that fetches
these sources, not the data itself.

## License

MIT — see [LICENSE](LICENSE).
