"""A local portal over everything this repo can already tell you.

`publish` renders three views into one static file. The other commands only ever
reach a terminal, which is where most of the analysis actually lives -- the
backtest, the filing-lag curve, the committee-timing test. This serves all of it
in one place, on localhost, with no new dependencies:

    python3 -m congress_trades.portal          # then open http://127.0.0.1:8777

Reports are run as subprocesses rather than imported, so what you read here is
byte-identical to what the CLI prints, and a crash in one report cannot take the
server down with it.

Two tables define everything a request can reach. REPORTS holds the read-only
commands with the parameters each may take; JOBS holds the long-running ones that
write. Nothing outside those tables is ever passed to a subprocess -- the single
exception is the `since` date for a title fetch, which is matched against a
strict pattern first -- so a crafted URL cannot smuggle arguments into the CLI.

Every job is serialised behind one slot and runs in a background thread, because
they all write and `prices` holds one sqlite write transaction open for its whole
pass: a second writer would die with "database is locked" partway through
collection. A second job is refused with the name of the one already running
rather than queued, since these take minutes to hours and a queued job would
surprise whoever started it. Read-only reports keep working throughout -- they
see the pre-transaction state until it commits.

Jobs whose prerequisites are missing are disabled with the reason attached, which
is why _prereq() checks for a configured model, an API key and fetched titles
here rather than leaving it to the subprocess to fail several minutes in.

Split by job: server (HTTP), jobs (reports and the job slot), models (the model
picker), api (JSON for the app), pages (HTML). The front end is in ../web/.
"""
from .server import main, serve, stop  # noqa: F401  the entry points run.sh uses
