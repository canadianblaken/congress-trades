"""The reports and jobs the portal can run, and the one job slot they share."""
from __future__ import annotations

import re
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

from ..config import CONFIG
from .api import _ro

# Each report, with the query parameters it is allowed to take. Anything not
# listed here is never passed through to the subprocess, so a crafted URL cannot
# smuggle arguments into the CLI.
REPORTS: dict[str, dict] = {
    "digest":    {"title": "Digest",    "args": {"days": int, "floor": int},
                  "defaults": {"days": 90, "floor": CONFIG.default_floor},
                  "blurb": "Prompt-sized brief of the last 90 days."},
    "scorecard": {"title": "Scoreboard", "args": {"floor": int, "horizon": str,
                                                  "limit": int, "min-trades": int},
                  "defaults": {"floor": CONFIG.default_floor, "horizon": "90",
                               "limit": 0, "min-trades": 10},
                  "blurb": "Members ranked by benchmark-adjusted record."},
    "backtest":  {"title": "Backtest",  "args": {"horizon": str, "floor": int,
                                                 "walk-forward": bool},
                  "defaults": {"horizon": "90", "floor": 0},
                  "blurb": "Would following the disclosures actually have paid?"},
    "lag":       {"title": "Filing lag", "args": {"floor": int}, "defaults": {"floor": 1},
                  "blurb": "Does a late filing predict a better trade?"},
    "timing":    {"title": "Committee timing",
                  "args": {"floor": int, "window": int, "sector-matched": bool},
                  "defaults": {"floor": 1, "window": 30},
                  "blurb": "Do members trade around their own hearings? Tick "
                           "sector-matched for the arm that requires the hearing "
                           "to be about the industry traded."},
    "mix":       {"title": "Asset mix", "args": {"floor": int}, "defaults": {"floor": 0},
                  "blurb": "Who is trading and who is parking."},
    "alerts":    {"title": "Alerts",    "args": {"days": int}, "defaults": {"days": 14},
                  "blurb": "What crossed a bar recently. Never marks anything seen."},
    "compliance": {"title": "Compliance", "args": {}, "defaults": {},
                   "blurb": "Filings past the STOCK Act's 45-day deadline, by member "
                            "and the most extreme cases."},
    "annual":    {"title": "Annual reports", "args": {}, "defaults": {},
                  "blurb": "Debts, outside income and board seats from the annual "
                           "disclosures fetched so far."},
    "finance":   {"title": "PAC money", "args": {}, "defaults": {},
                  "blurb": "Committee jurisdiction x PAC money x trades: who takes "
                           "money from the industries they oversee, and trades them."},
    "lobbying":  {"title": "Lobbying", "args": {}, "defaults": {},
                  "blurb": "LDA lobbying filings against the sectors members trade."},
    "judiciary": {"title": "Judges", "args": {}, "defaults": {},
                  "blurb": "Federal judges' disclosed holdings: what the bulk "
                           "snapshot covers."},
    "jurisdiction": {"title": "Committee jurisdiction", "args": {}, "defaults": {},
                     "blurb": "Which industries each committee oversees. A table "
                              "generated once by a model and committed as data; "
                              "regenerating it is a CLI job, because the point of "
                              "it is reading the diff."},
    "parser-qa": {"title": "Parser QA", "args": {}, "defaults": {}, "pre": True,
                  "blurb": "Does the House parser still read the filings? Counts "
                           "transaction headers against rows returned across every "
                           "cached filing, and shows what model audits have found. "
                           "Calls no model itself."},
}

# The long-running commands, with their argv fixed here. Nothing from a request
# reaches the subprocess except a `since` date matched against a strict pattern,
# so a crafted POST cannot add arguments. Every one of these writes to the
# database, which is why only one runs at a time and they share the slot with a
# refresh rather than getting one each.
JOBS: dict[str, dict] = {
    "refresh": {
        "title": "Refresh data", "verb": "Refreshing",
        "argv": ["-v", "all"], "needs": (),
        "blurb": "Collect, enrich, classify, price and render. 15-25 minutes on "
                 "a cold cache; reports stay readable while it runs."},
    "resolve": {
        "title": "Resolve untickered assets", "verb": "Resolving",
        "argv": ["resolve", "--apply"], "needs": ("model",),
        "blurb": "Label the asset names no pattern could place, and write the "
                 "tickers that survive all four checks into the trades table. "
                 "Run `prices` afterwards to score what it recovers."},
    "topics-fetch": {
        "title": "Fetch hearing titles", "verb": "Fetching",
        "argv": ["topics", "--stage", "fetch"], "needs": ("key",), "since": True,
        "blurb": "Meeting titles from Congress.gov, which the shipped snapshot "
                 "omits. About a minute per 60 meetings, resumable, and cached "
                 "forever once fetched."},
    "topics-tag": {
        "title": "Tag hearing titles", "verb": "Tagging",
        "argv": ["topics", "--stage", "tag"], "needs": ("model", "titles"),
        "blurb": "Read each title and record which industries it bears on. "
                 "Needed before the sector-matched timing arm can find anything."},
    "parser-qa": {
        "title": "Audit the parser", "verb": "Auditing",
        "argv": ["parser-qa", "--sample", "25"], "needs": ("model", "filings"),
        "blurb": "Hand 25 cached filings to a model with the rows the parser "
                 "produced, and ask which transactions it missed. A sampled "
                 "regression net, not a pipeline stage: it never edits a trade. "
                 "The Parser QA report shows what it has found."},
}

# Written on start, removed on exit, so `portal stop` knows what to signal.
PIDFILE = Path(__file__).resolve().parents[2] / ".portal.pid"

# Long-running collection state, shared across request threads.
_refresh = {"running": False, "started": 0.0, "line": "", "rc": None, "finished": 0.0,
            "proc": None, "job": "refresh"}
_lock = threading.Lock()


# --- running the CLI ---------------------------------------------------------

def _run(args: list[str], timeout: int = 180) -> tuple[int, str]:
    try:
        p = subprocess.run([sys.executable, "-m", "congress_trades", *args],
                           capture_output=True, text=True, timeout=timeout,
                           cwd=str(Path(__file__).resolve().parents[2]))
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 1, f"timed out after {timeout}s"
    except OSError as e:
        return 1, f"could not run: {e}"


def _report_args(name: str, q: dict) -> list[str]:
    """Only parameters declared in REPORTS, only of the declared type."""
    spec = REPORTS[name]["args"]
    out: list[str] = []
    for key, kind in spec.items():
        vals = q.get(key)
        if not vals:
            continue
        raw = vals[0].strip()
        if kind is bool:
            if raw in ("1", "true", "on"):
                out.append(f"--{key}")
            continue
        if kind is int:
            if not re.fullmatch(r"-?\d{1,7}", raw):
                continue
        else:
            if not re.fullmatch(r"[A-Za-z0-9._-]{1,24}", raw):
                continue
        out += [f"--{key}", raw]
    # `alerts` must never record: a recording call marks everything seen, and the
    # next reader -- including the nightly job -- then comes back empty.
    if name == "alerts":
        out.append("--dry-run")
    # A report must not call a model: it runs in a request thread on a 180s
    # timeout and takes no lock. --scan is the free half; the sampled audit that
    # does call a model is a JOB.
    if name == "parser-qa":
        out.append("--scan")
    return out


def _refresh_worker(argv: list[str]) -> None:
    try:
        p = subprocess.Popen([sys.executable, "-u", "-m", "congress_trades", *argv],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             cwd=str(Path(__file__).resolve().parents[2]))
        _refresh["proc"] = p
        assert p.stdout is not None
        for line in p.stdout:
            line = line.strip()
            if line:
                _refresh["line"] = line
        p.wait()
        _refresh["rc"] = p.returncode
    except OSError as e:
        _refresh["rc"], _refresh["line"] = 1, f"could not start: {e}"
    finally:
        _refresh["running"] = False
        _refresh["proc"] = None
        _refresh["finished"] = time.time()


def start_job(name: str = "refresh", since: str = "") -> tuple[bool, str]:
    """(started, why-not). Refuses rather than queues: these jobs are minutes to
    hours long, and a queued second one would surprise whoever started it."""
    spec = JOBS.get(name)
    if not spec:
        return False, "no such job"
    missing = [n for n in spec["needs"] if not _prereq()[n]["ok"]]
    if missing:
        return False, _prereq()[missing[0]]["why"]
    argv = list(spec["argv"])
    if spec.get("since") and since:
        # The only request value that reaches a subprocess anywhere in this file.
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", since):
            return False, "since must be an ISO date, e.g. 2025-01-01"
        argv += ["--since", since]
    with _lock:
        if _refresh["running"]:
            return False, f"{JOBS[_refresh['job']]['title']} is already running"
        _refresh.update(running=True, started=time.time(), line="starting...",
                        rc=None, job=name)
    threading.Thread(target=_refresh_worker, args=(argv,), daemon=True).start()
    return True, ""


def start_refresh() -> bool:
    """Kept for the no-JavaScript page and anything already posting to /refresh."""
    return start_job("refresh")[0]


def _prereq() -> dict:
    """What each job needs, and why it is not available when it is not.

    Checked here rather than left to the subprocess so the UI can say what to do
    before you press a button and wait for it to fail.
    """
    from .. import llm
    try:
        s = llm.settings()
        model = {"ok": True, "why": "",
                 "detail": f"{s.provider}: {s.model} at {s.base}"}
    except SystemExit as e:
        model = {"ok": False, "why": str(e).split(".")[0] + ".",
                 "detail": "no model configured"}
    import os
    key = bool(os.getenv("CONGRESS_API_KEY", "").strip())
    try:
        with _ro() as conn:
            titled = conn.execute(
                "SELECT count(*) FROM committee_meetings "
                "WHERE title IS NOT NULL AND title != ''").fetchone()[0]
    except sqlite3.Error:
        titled = 0
    try:
        from .. import parserqa
        n_cached = len(parserqa.cached_docs(CONFIG))
    except Exception:
        n_cached = 0
    return {
        "model": model,
        "key": {"ok": key, "detail": "set" if key else "not set",
                "url": "https://api.congress.gov/sign-up/",
                "why": "CONGRESS_API_KEY is not set. It is free and instant at "
                       "https://api.congress.gov/sign-up/ — put it in .env."},
        "titles": {"ok": titled > 0, "detail": f"{titled:,} meetings have a title",
                   "why": "No meeting has a title yet. Fetch hearing titles first."},
        "filings": {"ok": n_cached > 0,
                    "detail": f"{n_cached:,} House filing texts cached",
                    "why": "No House filing text is cached yet, and the audit "
                           "reads the cache rather than fetching. Refresh the "
                           "data first."},
    }


