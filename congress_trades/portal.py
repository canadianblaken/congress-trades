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
"""
from __future__ import annotations

import bisect
import html
import json
import re
import sqlite3
import subprocess
import sys
import threading
import time
import datetime as dt
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, quote

from . import prices
from .config import CONFIG

HOST = "127.0.0.1"
PORT = 8777

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
PIDFILE = Path(__file__).resolve().parent.parent / ".portal.pid"

# Long-running collection state, shared across request threads.
_refresh = {"running": False, "started": 0.0, "line": "", "rc": None, "finished": 0.0,
            "proc": None, "job": "refresh"}
_lock = threading.Lock()


# --- running the CLI ---------------------------------------------------------

def _run(args: list[str], timeout: int = 180) -> tuple[int, str]:
    try:
        p = subprocess.run([sys.executable, "-m", "congress_trades", *args],
                           capture_output=True, text=True, timeout=timeout,
                           cwd=str(Path(__file__).resolve().parent.parent))
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
                             cwd=str(Path(__file__).resolve().parent.parent))
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
    from . import llm
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
        from . import parserqa
        n_cached = len(parserqa.cached_docs(CONFIG))
    except Exception:
        n_cached = 0
    return {
        "model": model,
        "key": {"ok": key, "detail": "set" if key else "not set",
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


# --- database summary --------------------------------------------------------

def stats() -> dict:
    db = CONFIG.db_path
    if not db.exists():
        return {"contact": bool(CONFIG.contact)}
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        conn.row_factory = sqlite3.Row
        one = lambda q: (conn.execute(q).fetchone() or [0])[0]
        out = {
            "trades": one("SELECT COUNT(*) FROM congress_trades"),
            "members": one("SELECT COUNT(*) FROM congress_members"),
            "priced": one("SELECT COUNT(*) FROM trade_returns WHERE ret_90 IS NOT NULL"),
            "tickers": one("SELECT COUNT(DISTINCT ticker) FROM congress_trades WHERE ticker != ''"),
            "latest": one("SELECT MAX(disclosed) FROM congress_trades"),
            "contact": bool(CONFIG.contact),
        }
        conn.close()
        return out
    except sqlite3.Error as e:
        return {"error": str(e)}


# --- the smallest markdown renderer that reads these reports correctly -------

def md_to_html(md: str) -> str:
    """Handles what the reports actually emit: headings, pipe tables, bold, code,
    blockquotes and paragraphs. Everything is escaped before any tag is added."""
    lines = md.split("\n")
    out: list[str] = []
    i = 0

    def inline(s: str) -> str:
        s = html.escape(s)
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"(?<![*\w])\*([^*]+)\*(?![*\w])", r"<em>\1</em>", s)
        return s

    def is_sep(s: str) -> bool:
        return bool(re.fullmatch(r"\s*\|?[\s:|-]*-[\s:|-]*\|?\s*", s)) and "-" in s

    def cells(s: str) -> list[str]:
        s = s.strip()
        if s.startswith("|"):
            s = s[1:]
        if s.endswith("|"):
            s = s[:-1]
        return [c.strip() for c in s.split("|")]

    while i < len(lines):
        ln = lines[i]

        if not ln.strip():
            i += 1
            continue

        m = re.match(r"^(#{1,6})\s+(.*)$", ln)
        if m:
            lvl = min(len(m.group(1)) + 1, 6)   # page already owns <h1>
            out.append(f"<h{lvl}>{inline(m.group(2))}</h{lvl}>")
            i += 1
            continue

        if re.fullmatch(r"\s*([-*_])\s*(\1\s*){2,}", ln):
            out.append("<hr>")
            i += 1
            continue

        # table: a header row followed by a separator row
        if "|" in ln and i + 1 < len(lines) and is_sep(lines[i + 1]):
            head = cells(ln)
            i += 2
            body: list[list[str]] = []
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                body.append(cells(lines[i]))
                i += 1
            th = "".join(f"<th>{inline(c)}</th>" for c in head)
            rows = []
            for r in body:
                r += [""] * (len(head) - len(r))
                tds = "".join(
                    f'<td class="{_numclass(c)}">{inline(c)}</td>' for c in r[:len(head)])
                rows.append(f"<tr>{tds}</tr>")
            out.append('<div class="tbl-scroll"><table><thead><tr>' + th +
                       "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>")
            continue

        if ln.lstrip().startswith(">"):
            buf = []
            while i < len(lines) and lines[i].lstrip().startswith(">"):
                buf.append(lines[i].lstrip()[1:].strip())
                i += 1
            out.append("<blockquote>" + inline(" ".join(buf)) + "</blockquote>")
            continue

        if re.match(r"^\s*([-*+]|\d+\.)\s+", ln):
            ordered = bool(re.match(r"^\s*\d+\.\s+", ln))
            items = []
            while i < len(lines) and re.match(r"^\s*([-*+]|\d+\.)\s+", lines[i]):
                items.append(re.sub(r"^\s*([-*+]|\d+\.)\s+", "", lines[i]))
                i += 1
            tag = "ol" if ordered else "ul"
            out.append(f"<{tag}>" + "".join(f"<li>{inline(x)}</li>" for x in items) + f"</{tag}>")
            continue

        buf = []
        while i < len(lines) and lines[i].strip() and not re.match(r"^\s*(#{1,6}\s|>|[-*+]\s)", lines[i]) \
                and not ("|" in lines[i] and i + 1 < len(lines) and is_sep(lines[i + 1])):
            buf.append(lines[i])
            i += 1
        if buf:
            out.append("<p>" + inline(" ".join(buf)) + "</p>")

    return "\n".join(out)


def _numclass(c: str) -> str:
    t = c.replace(",", "").strip()
    if re.fullmatch(r"[+-]?\$?\d*\.?\d+%?", t):
        if t.startswith("+") and t not in ("+0%", "+0.0%"):
            return "num pos"
        if t.startswith("-"):
            return "num neg"
        return "num"
    return ""


# --- page shell --------------------------------------------------------------

CSS = """
:root { color-scheme:dark;
  --bg:#0e1116; --panel:#12161c; --line:#222831; --line-2:#2a3038;
  --ink:#d6dae0; --ink-2:#9aa4b2; --ink-3:#6b7480; --white:#fff;
  --pos:#3987e5; --neg:#e66767; --link:#6cb6ff; }
* { box-sizing:border-box; }
body { background:var(--bg); color:var(--ink); margin:0 auto; max-width:1240px;
  font:15px/1.55 -apple-system,Segoe UI,Roboto,sans-serif; padding:1.6rem 1.2rem 4rem; }
a { color:var(--link); text-decoration:none; } a:hover { text-decoration:underline; }
h1 { color:var(--white); font-size:1.6rem; margin:.2rem 0 .3rem;
  border-bottom:2px solid var(--line-2); padding-bottom:.4rem; }
h2 { color:var(--white); font-size:1.15rem; margin:1.6rem 0 .4rem; }
h3 { color:var(--ink); font-size:1rem; margin:1.2rem 0 .3rem; }
p { margin:.55rem 0; } code { background:var(--panel); padding:.1rem .3rem; border-radius:3px;
  font-size:12.5px; } hr { border:0; border-top:1px solid var(--line); margin:1.4rem 0; }
blockquote { border-left:3px solid var(--line-2); margin:.6rem 0; padding:.1rem 0 .1rem .8rem;
  color:var(--ink-2); }
.sub { color:var(--ink-3); font-size:13px; margin:.4rem 0 1.2rem; }
.nav { display:flex; flex-wrap:wrap; gap:.3rem; margin:0 0 1.2rem; }
.nav a { background:var(--panel); border:1px solid var(--line-2); color:var(--ink-2);
  padding:.32rem .7rem; border-radius:6px; font-size:13.5px; }
.nav a[aria-current=page] { background:#1b2430; color:var(--white); border-color:#3b4657; }
.stats { display:flex; flex-wrap:wrap; gap:1.6rem; margin:0 0 1.4rem; padding:.9rem 1.1rem;
  background:var(--panel); border:1px solid var(--line); border-radius:8px; }
.stat b { display:block; color:var(--white); font-size:1.35rem; font-weight:600;
  line-height:1.2; font-variant-numeric:tabular-nums; }
.stat span { color:var(--ink-3); font-size:12px; text-transform:uppercase;
  letter-spacing:.04em; }
table { border-collapse:collapse; width:100%; font-size:13px; margin:.4rem 0 .2rem; }
th,td { text-align:left; padding:.36rem .55rem; border-bottom:1px solid var(--line); }
th { color:var(--ink-3); font-weight:600; font-size:12px; text-transform:uppercase;
  letter-spacing:.03em; white-space:nowrap; }
td.num { text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }
td.pos { color:var(--pos); } td.neg { color:var(--neg); }
tbody tr:hover { background:#161b22; }
.tbl-scroll { overflow-x:auto; }
.cards { display:grid; gap:.8rem; grid-template-columns:repeat(auto-fill,minmax(250px,1fr)); }
.card { background:var(--panel); border:1px solid var(--line); border-radius:8px;
  padding:.85rem 1rem; }
.card b { color:var(--white); display:block; margin-bottom:.2rem; }
.card span { color:var(--ink-3); font-size:13px; }
.bar { display:flex; align-items:center; gap:.7rem; flex-wrap:wrap; margin:0 0 1.2rem;
  padding:.7rem .9rem; background:var(--panel); border:1px solid var(--line);
  border-radius:8px; font-size:13px; }
button { background:#1b2430; color:var(--white); border:1px solid #3b4657; cursor:pointer;
  padding:.4rem .9rem; border-radius:6px; font-size:13.5px; font-family:inherit; }
button:hover:not(:disabled) { background:#232e3d; }
button:disabled { opacity:.55; cursor:default; }
.live { color:var(--ink-2); font-variant-numeric:tabular-nums; }
.err { border-left:3px solid var(--neg); background:#1a1315; padding:.7rem .9rem;
  border-radius:0 6px 6px 0; margin:.8rem 0; }
.note { color:var(--ink-3); font-size:12.5px; }
iframe { width:100%; height:78vh; border:1px solid var(--line); border-radius:8px;
  background:var(--bg); }
form.opts { display:flex; gap:.5rem; align-items:center; flex-wrap:wrap; font-size:13px;
  color:var(--ink-2); margin:0 0 1rem; }
form.opts input,form.opts select { background:var(--bg); color:var(--ink);
  border:1px solid var(--line-2); border-radius:5px; padding:.25rem .4rem;
  font-family:inherit; font-size:13px; width:5.5rem; }
"""

REFRESH_JS = """
const btn=document.getElementById('rf'),live=document.getElementById('live');
async function poll(){
  try{
    const r=await fetch('/status'); const s=await r.json();
    if(s.running){ btn.disabled=true; btn.textContent='Refreshing...';
      live.textContent=s.line||''; setTimeout(poll,1500); }
    else{ btn.disabled=false; btn.textContent='Refresh data';
      if(s.rc===0){ live.textContent='done - reloading'; setTimeout(()=>location.reload(),700); }
      else if(s.rc!==null){ live.textContent='failed: '+(s.line||'see terminal'); } }
  }catch(e){ live.textContent='lost contact with the server'; }
}
if(btn){ btn.onclick=async()=>{ btn.disabled=true; live.textContent='starting...';
  await fetch('/refresh',{method:'POST'}); poll(); };
  fetch('/status').then(r=>r.json()).then(s=>{ if(s.running) poll(); }); }
"""


def shell(title: str, body: str, current: str = "") -> bytes:
    nav = ['<a href="/"%s>Overview</a>' % (' aria-current="page"' if current == "" else "")]
    nav.append('<a href="/page"%s>Full page</a>'
               % (' aria-current="page"' if current == "page" else ""))
    for key, spec in REPORTS.items():
        cur = ' aria-current="page"' if current == key else ""
        nav.append(f'<a href="/r/{key}"{cur}>{html.escape(spec["title"])}</a>')
    nav.append('<a href="/jobs"%s>Maintenance</a>'
               % (' aria-current="page"' if current == "jobs" else ""))
    running = _refresh["running"]
    verb = JOBS.get(_refresh["job"], {}).get("verb", "Running")
    bar = (f'<div class="bar"><button id="rf"{" disabled" if running else ""}>'
           f'{verb + "..." if running else "Refresh data"}</button>'
           f'<span class="live" id="live">{html.escape(_refresh["line"] if running else "")}</span>'
           '<span class="note">Collection holds the database for 15-20 minutes on a cold '
           'cache; reports stay readable while it runs.</span></div>')
    return (f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>{html.escape(title)} - Congress Trades</title><style>{CSS}</style></head><body>
<h1>{html.escape(title)}</h1>
<nav class="nav">{''.join(nav)}</nav>
{bar}
{body}
<script>{REFRESH_JS}</script></body></html>""").encode("utf-8")


def overview() -> str:
    s = stats()
    if not s:
        return ('<div class="err"><b>No database yet.</b><p>Nothing has been collected. '
                'Press <b>Refresh data</b> above, or run <code>./run.sh</code> in a '
                'terminal to watch it work. The first pass takes 15-25 minutes, almost '
                'all of it rate-limited price fetches.</p></div>')
    if "error" in s:
        return f'<div class="err">Could not read the database: {html.escape(s["error"])}</div>'
    tiles = [("trades", "disclosures"), ("members", "members"),
             ("priced", "with 90d returns"), ("tickers", "distinct tickers")]
    st = "".join(f'<div class="stat"><b>{s.get(k, 0):,}</b><span>{lab}</span></div>'
                 for k, lab in tiles)
    if s.get("latest"):
        st += f'<div class="stat"><b>{html.escape(str(s["latest"]))}</b><span>latest disclosure</span></div>'
    cards = "".join(
        f'<a class="card" href="/r/{k}"><b>{html.escape(v["title"])}</b>'
        f'<span>{html.escape(v["blurb"])}</span></a>' for k, v in REPORTS.items())
    page = CONFIG.out_html
    pagenote = (f'<p>The rendered page is at <a href="/page">Full page</a> '
                f'(<code>{html.escape(str(page))}</code>).</p>' if page.exists() else
                '<p class="note">The rendered page does not exist yet; refresh to build it.</p>')
    return (f'<div class="stats">{st}</div>{pagenote}'
            f'<h2>Reports</h2><div class="cards">{cards}</div>'
            '<h2>Reading any of this</h2>'
            '<p>Returns are measured from the <b>disclosure</b> date, not the trade date, '
            'and the <b>vs sector</b> column is the honest one: across the scored members '
            'roughly half the apparent edge is sector exposure rather than stock selection. '
            'Sector alpha on fund holdings is noise, because a fund inherits its sponsor\'s '
            'SIC code. Past alpha barely predicts future alpha - the scorecard prints the '
            'correlation, and it is near zero.</p>')


def jobs_page(msg: str = "") -> str:
    """The no-JavaScript maintenance page. Plain forms, one button each."""
    pre = _prereq()
    running = _refresh["running"]

    def line(key: str, label: str) -> str:
        v = pre[key]
        cls = "num pos" if v["ok"] else "num neg"
        return (f'<div><b>{html.escape(label)}</b> <span class="{cls}">'
                f'{"ok" if v["ok"] else "not ready"}</span> &mdash; '
                f'{html.escape(v["detail"])}</div>')

    out = [f'<div class="err">{html.escape(msg)}</div>' if msg else "",
           '<div class="chartbox"><h2>Model</h2>',
           '<p class="cap">What <code>advise</code>, <code>resolve</code> and '
           '<code>topics</code> will use. Set it in <code>.env</code>; a shell '
           'export beats the file.</p>',
           line("model", "Model"), line("key", "Congress.gov key"),
           line("titles", "Meeting titles"),
           '<p class="note">Run <code>./run.sh llm</code> in a terminal for a live '
           'round trip, including the JSON-schema step that <code>resolve</code> '
           'and <code>topics</code> depend on.</p></div>',
           '<h2>Jobs</h2>',
           '<p class="note">Each writes to the database, so only one runs at a '
           'time and they share the slot with a refresh.</p>']
    for name, spec in JOBS.items():
        missing = [n for n in spec["needs"] if not pre[n]["ok"]]
        why = pre[missing[0]]["why"] if missing else ""
        dis = " disabled" if missing or running else ""
        since = ('<input name="since" value="2025-01-01" style="max-width:9rem" '
                 'aria-label="fetch from this date"> ') if spec.get("since") else ""
        note = (f'<p class="note neg">{html.escape(why)}</p>' if why else
                '<p class="note">Another job is running.</p>' if running else "")
        out += [f'<div class="chartbox"><h2>{html.escape(spec["title"])}</h2>'
                f'<p class="cap">{html.escape(spec["blurb"])}</p>{note}'
                f'<form method="post" action="/job/{name}?html=1">{since}'
                f'<button type="submit"{dis}>{html.escape(spec["title"])}</button>'
                '</form></div>']
    if running:
        out.append(f'<p class="note">Running: {html.escape(_refresh["line"])}. '
                   'Reload to see progress.</p>')
    return "".join(out)


def options_form(name: str, q: dict) -> str:
    spec = REPORTS[name]["args"]
    if not spec:
        return ""
    bits = []
    for key, kind in spec.items():
        val = html.escape((q.get(key) or [""])[0])
        lab = html.escape(key.replace("-", " "))
        if kind is bool:
            ck = " checked" if val in ("1", "true", "on") else ""
            bits.append(f'<label>{lab} <input type="checkbox" name="{key}" value="1"{ck}></label>')
        elif key == "horizon":
            opts = "".join(f'<option{" selected" if val == o else ""}>{o}</option>'
                           for o in ("30", "90"))
            bits.append(f'<label>{lab} <select name="{key}">{opts}</select></label>')
        else:
            bits.append(f'<label>{lab} <input name="{key}" value="{val}" inputmode="numeric"></label>')
    return (f'<form class="opts" method="get" action="/r/{name}">' + "".join(bits) +
            '<button type="submit">Apply</button></form>')


def report_page(name: str, q: dict) -> str:
    args = _report_args(name, q)
    rc, out = _run([name, *args])
    body = options_form(name, q)
    if _refresh["running"]:
        body += ('<p class="note">A refresh is running, so these numbers are the '
                 'pre-refresh state until it commits.</p>')
    if rc != 0 and not out.strip():
        return body + '<div class="err">The command produced nothing and exited non-zero.</div>'
    if rc != 0:
        body += f'<div class="err">Exited {rc}. Output follows.</div>'
    return body + md_to_html(out)


# --- live data API -----------------------------------------------------------

SORTS = {"date": "COALESCE(NULLIF(t.disclosed,''), t.tx_date)", "tx": "t.tx_date",
         "member": "t.member", "ticker": "t.ticker", "amount": "t.amount_min",
         "ret": "r.ret_90", "alpha": "(r.ret_90 - r.bench_90)"}


def _ro():
    conn = sqlite3.connect(f"file:{CONFIG.db_path}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def api_trades(q: dict) -> dict:
    """Filtered, sorted, paged trades straight from sqlite -- this is what makes the
    explorer live: every control is a query, not a re-render of a baked payload."""
    if not CONFIG.db_path.exists():
        return {"rows": [], "total": 0, "note": "no database yet"}
    where, params = ["1=1"], []
    text = (q.get("q") or [""])[0].strip()
    if text:
        where.append("(t.member LIKE ? OR t.ticker LIKE ? OR t.asset_name LIKE ?)")
        params += [f"%{text}%"] * 3
    for key, col in (("member", "t.member"), ("ticker", "t.ticker"),
                     ("chamber", "t.chamber"), ("type", "t.tx_type"), ("owner", "t.owner")):
        v = (q.get(key) or [""])[0].strip()
        if v:
            where.append(f"{col} = ?")
            params.append(v)
    floor = (q.get("floor") or ["0"])[0]
    if re.fullmatch(r"\d{1,9}", floor) and int(floor):
        where.append("t.amount_min >= ?")
        params.append(int(floor))
    since = (q.get("since") or [""])[0]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", since):
        where.append("COALESCE(NULLIF(t.disclosed,''), t.tx_date) >= ?")
        params.append(since)
    # A string bound, so "2026-02-31" closes a month whatever its length.
    until = (q.get("until") or [""])[0]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", until):
        where.append("COALESCE(NULLIF(t.disclosed,''), t.tx_date) <= ?")
        params.append(until)
    if (q.get("tickered") or [""])[0] == "1":
        where.append("t.ticker != ''")

    sort = SORTS.get((q.get("sort") or ["date"])[0], SORTS["date"])
    desc = "ASC" if (q.get("dir") or ["desc"])[0] == "asc" else "DESC"
    try:
        limit = min(max(int((q.get("limit") or ["100"])[0]), 1), 500)
        offset = max(int((q.get("offset") or ["0"])[0]), 0)
    except ValueError:
        limit, offset = 100, 0

    sql_from = ("FROM congress_trades t LEFT JOIN trade_returns r ON r.trade_id = t.id "
                "LEFT JOIN congress_members m ON m.member = t.member WHERE " + " AND ".join(where))
    try:
        conn = _ro()
        total = conn.execute(f"SELECT COUNT(*) {sql_from}", params).fetchone()[0]
        rows = conn.execute(
            f"""SELECT t.id, t.chamber, t.member, m.full_name, m.party, t.ticker,
                       t.asset_name, t.tx_type, t.tx_date, t.disclosed, t.amount_range,
                       t.amount_min, t.doc_url, r.ret_90, r.bench_90
                {sql_from} ORDER BY {sort} {desc} NULLS LAST LIMIT ? OFFSET ?""",
            [*params, limit, offset]).fetchall()
        out = [dict(r) for r in rows]
        conn.close()
    except sqlite3.Error as e:
        return {"rows": [], "total": 0, "error": str(e)}
    for r in out:
        if r["ret_90"] is not None and r["bench_90"] is not None:
            a = r["ret_90"] - r["bench_90"]
            r["alpha"] = -a if (r["tx_type"] or "").lower().startswith("s") else a
        else:
            r["alpha"] = None
    return {"rows": out, "total": total, "limit": limit, "offset": offset}


def api_facets() -> dict:
    """The values the filter controls offer. Read once per load, so the explorer
    never invents a member or ticker that is not actually in this database."""
    if not CONFIG.db_path.exists():
        return {"members": [], "tickers": []}
    try:
        conn = _ro()
        members = [dict(r) for r in conn.execute(
            """SELECT t.member, COALESCE(m.full_name,'') full_name, COALESCE(m.party,'') party,
                      COALESCE(m.chamber,t.chamber) chamber, COUNT(*) n
                 FROM congress_trades t LEFT JOIN congress_members m ON m.member=t.member
                GROUP BY t.member ORDER BY n DESC""").fetchall()]
        tickers = [dict(r) for r in conn.execute(
            """SELECT ticker, COUNT(*) n FROM congress_trades WHERE ticker != ''
                GROUP BY ticker ORDER BY n DESC LIMIT 400""").fetchall()]
        conn.close()
        return {"members": members, "tickers": tickers}
    except sqlite3.Error as e:
        return {"members": [], "tickers": [], "error": str(e)}


def _price_trend(sym: str, marks: list[dict]) -> dict:
    """The cached close series for one name, bucketed to keep the line readable.

    Daily closes over ten years are 2,500 points in a 150px panel chart: past
    roughly a year the noise stops being information, so longer spans collapse to
    the last close of each week or month. The disclosures ride on the same
    buckets, which is the whole point of drawing them together -- you are looking
    for whether Congress moved before the line did."""
    s = prices.cached(sym)
    if not s:
        return {}
    days = sorted(d for d in s if isinstance(s.get(d), (int, float)))
    if len(days) < 8:
        return {}
    # Start a little before the first disclosure: the run-up is context, but the
    # decade of price history before anyone in Congress touched the name is not.
    first = min((m["d"] for m in marks if m["d"]), default="")
    if first:
        try:
            start = (dt.date.fromisoformat(first) - dt.timedelta(days=120)).isoformat()
            days = [d for d in days if d >= start] or days
        except ValueError:
            pass
    span = (dt.date.fromisoformat(days[-1]) - dt.date.fromisoformat(days[0])).days
    bucket = "day" if span <= 420 else "week" if span <= 2600 else "month"

    def key(d: str) -> str:
        if bucket == "day":
            return d
        if bucket == "month":
            return d[:7]
        day = dt.date.fromisoformat(d)
        return (day - dt.timedelta(days=day.weekday())).isoformat()

    closes: dict[str, tuple[str, float]] = {}
    for d in days:                         # last close wins, so each bucket ends where it ended
        closes[key(d)] = (d, float(s[d]))
    labels = sorted(closes)
    buys = [0] * len(labels)
    sells = [0] * len(labels)
    for m in marks:
        if not m["d"]:
            continue
        # A disclosure landing on a Saturday or a holiday has no bucket of its own,
        # so it attaches to the next one that exists -- the same forward walk the
        # return calculation uses. Exact matching silently drops a third of them.
        i = bisect.bisect_left(labels, key(m["d"]))
        if i >= len(labels):
            i = len(labels) - 1            # disclosed after the last cached close
        buys[i] += m["buys"] or 0
        sells[i] += m["sells"] or 0
    return {"bucket": bucket, "labels": labels,
            "close": [round(closes[k][1], 4) for k in labels],
            "asof": [closes[k][0] for k in labels],
            "buys": buys, "sells": sells}


def api_ticker(sym: str, q: dict) -> dict:
    """Everything known about one name, for the click-through from a chart bar.

    Deliberately not windowed the way the chart is: you arrive here asking what the
    whole record on this ticker looks like, and a panel that silently inherited the
    chart's date filter would answer a different question than the one asked."""
    sym = (sym or "").strip().upper()
    if not sym or not re.fullmatch(r"[A-Z0-9.\-]{1,12}", sym):
        return {"error": "bad symbol"}
    if not CONFIG.db_path.exists():
        return {"error": "no database"}
    try:
        conn = _ro()
        agg = conn.execute(
            """SELECT COUNT(*) n,
                      SUM(CASE WHEN LOWER(tx_type) LIKE 'b%' THEN 1 ELSE 0 END) buys,
                      SUM(CASE WHEN LOWER(tx_type) LIKE 's%' THEN 1 ELSE 0 END) sells,
                      SUM(CASE WHEN LOWER(tx_type) LIKE 'b%' THEN amount_min ELSE 0 END) buy_vol,
                      SUM(CASE WHEN LOWER(tx_type) LIKE 's%' THEN amount_min ELSE 0 END) sell_vol,
                      COUNT(DISTINCT member) members,
                      MIN(COALESCE(NULLIF(disclosed,''), tx_date)) first_seen,
                      MAX(COALESCE(NULLIF(disclosed,''), tx_date)) last_seen
                 FROM congress_trades WHERE ticker = ?""", (sym,)).fetchone()
        if not agg or not agg["n"]:
            conn.close()
            return {"ticker": sym, "agg": {"n": 0}}
        sector = conn.execute(
            "SELECT sector FROM ticker_sectors WHERE ticker = ?", (sym,)).fetchone()
        name = conn.execute(
            """SELECT asset_name FROM congress_trades
                WHERE ticker = ? AND asset_name != '' ORDER BY LENGTH(asset_name)
                LIMIT 1""", (sym,)).fetchone()
        # Median, not mean, to match how the scorecard reports a record.
        alphas = [r[0] for r in conn.execute(
            """SELECT CASE WHEN LOWER(t.tx_type) LIKE 's%'
                           THEN r.bench_90 - r.ret_90 ELSE r.ret_90 - r.bench_90 END
                 FROM congress_trades t JOIN trade_returns r ON r.trade_id = t.id
                WHERE t.ticker = ? AND r.ret_90 IS NOT NULL AND r.bench_90 IS NOT NULL""",
            (sym,)).fetchall() if r[0] is not None]
        members = [dict(r) for r in conn.execute(
            """SELECT t.member, COALESCE(m.full_name,'') full_name,
                      COALESCE(m.party,'') party, COALESCE(m.chamber,t.chamber) chamber,
                      COUNT(*) n,
                      SUM(CASE WHEN LOWER(t.tx_type) LIKE 'b%' THEN 1 ELSE 0 END) buys,
                      SUM(CASE WHEN LOWER(t.tx_type) LIKE 's%' THEN 1 ELSE 0 END) sells,
                      SUM(CASE WHEN LOWER(t.tx_type) LIKE 'b%' THEN t.amount_min
                               ELSE -t.amount_min END) net
                 FROM congress_trades t LEFT JOIN congress_members m ON m.member = t.member
                WHERE t.ticker = ? GROUP BY t.member ORDER BY n DESC LIMIT 15""",
            (sym,)).fetchall()]
        recent = [dict(r) for r in conn.execute(
            """SELECT t.member, COALESCE(m.full_name,'') full_name, t.tx_type, t.tx_date,
                      t.disclosed, t.amount_range, t.doc_url, r.ret_90, r.bench_90
                 FROM congress_trades t LEFT JOIN congress_members m ON m.member = t.member
                 LEFT JOIN trade_returns r ON r.trade_id = t.id
                WHERE t.ticker = ?
                ORDER BY COALESCE(NULLIF(t.disclosed,''), t.tx_date) DESC LIMIT 12""",
            (sym,)).fetchall()]
        months = [dict(r) for r in conn.execute(
            """SELECT substr(COALESCE(NULLIF(disclosed,''), tx_date),1,7) ym,
                      SUM(CASE WHEN LOWER(tx_type) LIKE 'b%' THEN 1 ELSE 0 END) buys,
                      SUM(CASE WHEN LOWER(tx_type) LIKE 's%' THEN 1 ELSE 0 END) sells
                 FROM congress_trades WHERE ticker = ? AND ym != ''
                GROUP BY ym ORDER BY ym""", (sym,)).fetchall()]
        marks = [dict(r) for r in conn.execute(
            """SELECT COALESCE(NULLIF(disclosed,''), tx_date) d,
                      SUM(CASE WHEN LOWER(tx_type) LIKE 'b%' THEN 1 ELSE 0 END) buys,
                      SUM(CASE WHEN LOWER(tx_type) LIKE 's%' THEN 1 ELSE 0 END) sells
                 FROM congress_trades
                WHERE ticker = ? AND COALESCE(NULLIF(disclosed,''), tx_date) != ''
                GROUP BY d ORDER BY d""", (sym,)).fetchall()]
        conn.close()
    except sqlite3.Error as e:
        return {"error": str(e)}
    a = dict(agg)
    a["net"] = (a["buy_vol"] or 0) - (a["sell_vol"] or 0)
    med = None
    if alphas:
        alphas.sort()
        k = len(alphas)
        med = alphas[k // 2] if k % 2 else (alphas[k // 2 - 1] + alphas[k // 2]) / 2
    for r in recent:
        r["alpha"] = (None if r["ret_90"] is None or r["bench_90"] is None else
                      (r["bench_90"] - r["ret_90"]
                       if (r["tx_type"] or "").lower().startswith("s")
                       else r["ret_90"] - r["bench_90"]))
    return {"ticker": sym, "asset_name": name["asset_name"] if name else "",
            "sector": sector["sector"] if sector else "", "agg": a,
            "alpha_med": med, "alpha_n": len(alphas),
            "members": members, "recent": recent, "months": months,
            "price": _price_trend(sym, marks)}


def api_member(name: str) -> dict:
    """Everything known about one person, for the drill-down panel."""
    try:
        conn = _ro()
        row = conn.execute(
            "SELECT * FROM congress_members WHERE member = ?", (name,)).fetchone()
        prof = dict(row) if row else {}
        if prof.get("bioguide"):
            prof["committees"] = [dict(r) for r in conn.execute(
                "SELECT name, rank FROM member_committees WHERE bioguide = ?"
                if _has_col(conn, "member_committees", "rank") else
                "SELECT name FROM member_committees WHERE bioguide = ?",
                (prof["bioguide"],)).fetchall()]
        agg = conn.execute(
            """SELECT COUNT(*) n,
                      SUM(CASE WHEN LOWER(t.tx_type) LIKE 'b%' THEN 1 ELSE 0 END) buys,
                      SUM(CASE WHEN LOWER(t.tx_type) LIKE 's%' THEN 1 ELSE 0 END) sells,
                      SUM(t.amount_min) vol,
                      AVG(CASE WHEN r.ret_90 IS NOT NULL AND r.bench_90 IS NOT NULL
                          THEN (CASE WHEN LOWER(t.tx_type) LIKE 's%'
                                THEN r.bench_90 - r.ret_90 ELSE r.ret_90 - r.bench_90 END)
                          END) alpha,
                      AVG(JULIANDAY(t.disclosed) - JULIANDAY(t.tx_date)) lag
                 FROM congress_trades t LEFT JOIN trade_returns r ON r.trade_id=t.id
                WHERE t.member = ?""", (name,)).fetchone()
        prof["agg"] = dict(agg) if agg else {}
        prof["top"] = [dict(r) for r in conn.execute(
            """SELECT ticker, COUNT(*) n,
                      SUM(CASE WHEN LOWER(tx_type) LIKE 'b%' THEN amount_min
                               WHEN LOWER(tx_type) LIKE 's%' THEN -amount_min
                               ELSE 0 END) net
                 FROM congress_trades
                WHERE member = ? AND ticker != '' GROUP BY ticker
                ORDER BY n DESC LIMIT 12""", (name,)).fetchall()]
        conn.close()
        return prof
    except sqlite3.Error as e:
        return {"error": str(e)}


TOP_METRICS = {
    "net":     "ABS(buy_vol - sell_vol)",
    "volume":  "(buy_vol + sell_vol)",
    "count":   "n",
    "members": "members",
}

DATED = "COALESCE(NULLIF(t.disclosed,''), t.tx_date)"


def _window(q: dict) -> tuple[list[str], list]:
    """Shared filters for the aggregate views: window, floor, chamber."""
    where, params = ["t.ticker != ''"], []
    days = (q.get("days") or ["365"])[0]
    if re.fullmatch(r"\d{1,5}", days) and int(days):
        cut = (dt.date.today() - dt.timedelta(days=int(days))).isoformat()
        where.append(f"{DATED} >= ?")
        params.append(cut)
    floor = (q.get("floor") or ["0"])[0]
    if re.fullmatch(r"\d{1,9}", floor) and int(floor):
        where.append("t.amount_min >= ?")
        params.append(int(floor))
    ch = (q.get("chamber") or [""])[0]
    if ch in ("House", "Senate"):
        where.append("t.chamber = ?")
        params.append(ch)
    return where, params


def api_top(q: dict) -> dict:
    """Most-traded names, by whichever measure the reader asked for.

    Net flow is buying minus selling, so it is ordered by absolute size: the point
    is what moved, and a name Congress dumped is as informative as one it bought.
    Amounts are disclosed brackets, so every dollar figure is a floor, never exact."""
    if not CONFIG.db_path.exists():
        return {"rows": [], "metric": "net"}
    metric = (q.get("metric") or ["net"])[0]
    order = TOP_METRICS.get(metric, TOP_METRICS["net"])
    where, params = _window(q)
    try:
        limit = min(max(int((q.get("limit") or ["18"])[0]), 3), 40)
    except ValueError:
        limit = 18
    sql = f"""
        SELECT ticker,
               SUM(CASE WHEN LOWER(t.tx_type) LIKE 'b%' THEN t.amount_min ELSE 0 END) buy_vol,
               SUM(CASE WHEN LOWER(t.tx_type) LIKE 's%' THEN t.amount_min ELSE 0 END) sell_vol,
               SUM(CASE WHEN LOWER(t.tx_type) LIKE 'b%' THEN 1 ELSE 0 END) buys,
               SUM(CASE WHEN LOWER(t.tx_type) LIKE 's%' THEN 1 ELSE 0 END) sells,
               COUNT(*) n, COUNT(DISTINCT t.member) members
          FROM congress_trades t
         WHERE {' AND '.join(where)}
         GROUP BY ticker HAVING n > 0
         ORDER BY {order} DESC LIMIT ?"""
    try:
        conn = _ro()
        rows = [dict(r) for r in conn.execute(sql, [*params, limit]).fetchall()]
        conn.close()
    except sqlite3.Error as e:
        return {"rows": [], "error": str(e)}
    for r in rows:
        r["net"] = (r["buy_vol"] or 0) - (r["sell_vol"] or 0)
        r["volume"] = (r["buy_vol"] or 0) + (r["sell_vol"] or 0)
        r["count"] = r["n"]
    return {"rows": rows, "metric": metric if metric in TOP_METRICS else "net"}


def api_timeline(q: dict) -> dict:
    """Buying and selling per month -- the shape of the trend behind the top names."""
    if not CONFIG.db_path.exists():
        return {"rows": []}
    where, params = _window(q)
    where[0] = "1=1"                       # the timeline counts untickered rows too
    sql = f"""
        SELECT substr({DATED},1,7) ym,
               SUM(CASE WHEN LOWER(t.tx_type) LIKE 'b%' THEN t.amount_min ELSE 0 END) buy_vol,
               SUM(CASE WHEN LOWER(t.tx_type) LIKE 's%' THEN t.amount_min ELSE 0 END) sell_vol,
               SUM(CASE WHEN LOWER(t.tx_type) LIKE 'b%' THEN 1 ELSE 0 END) buys,
               SUM(CASE WHEN LOWER(t.tx_type) LIKE 's%' THEN 1 ELSE 0 END) sells,
               COUNT(*) n, COUNT(DISTINCT t.member) members
          FROM congress_trades t
         WHERE {' AND '.join(where)} AND substr({DATED},1,7) != ''
         GROUP BY ym ORDER BY ym"""
    try:
        conn = _ro()
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        conn.close()
        # The window's first month is usually partial; a click-through needs the cut.
        cut = next((p for w, p in zip(where[1:], params) if w.startswith(DATED)), "")
        return {"rows": rows, "since": cut}
    except sqlite3.Error as e:
        return {"rows": [], "error": str(e)}


def _has_col(conn, table: str, col: str) -> bool:
    try:
        return any(r[1] == col for r in conn.execute(f"PRAGMA table_info({table})"))
    except sqlite3.Error:
        return False


# --- HTTP --------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "congress-portal"
    # Set once the server object exists, so a request can ask it to stop.
    srv = None

    def log_message(self, fmt, *a):            # one quiet line, not two noisy ones
        if not self.path.startswith(("/status", "/api/")):
            sys.stderr.write(f"  {self.command} {self.path}\n")

    def _send(self, body: bytes, ctype="text/html; charset=utf-8", code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass                                # the tab moved on; not our problem

    def _json(self, obj, code=200):
        self._send(json.dumps(obj, default=str).encode("utf-8"),
                   "application/json; charset=utf-8", code)

    def do_POST(self):
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)
        if path == "/refresh":
            started = start_refresh()
            self._json({"started": started, "running": _refresh["running"]})
            return
        if path.startswith("/job/"):
            name = path[len("/job/"):]
            since = (q.get("since") or [""])[0].strip()
            as_html = (q.get("html") or [""])[0] == "1"
            if as_html and not since:
                # The no-JavaScript form sends its fields in the body.
                n = int(self.headers.get("Content-Length") or 0)
                if 0 < n <= 4096:
                    body = parse_qs(self.rfile.read(n).decode("utf-8", "replace"))
                    since = (body.get("since") or [""])[0].strip()
            started, why = start_job(name, since)
            if as_html:
                self.send_response(303)
                self.send_header("Location",
                                 "/jobs" if started else "/jobs?msg=" + quote(why))
                self.end_headers()
                return
            self._json({"started": started, "why": why, "job": _refresh["job"],
                        "running": _refresh["running"]}, 200 if started else 409)
            return
        if path == "/shutdown":
            # A refresh is a long, resumable job, but stopping mid-`prices` throws
            # away an uncommitted transaction -- so say so rather than silently
            # killing it. The caller re-posts with ?force=1 to mean it.
            force = (parse_qs(urlparse(self.path).query).get("force") or [""])[0] == "1"
            if _refresh["running"] and not force:
                self._json({"stopped": False, "refreshing": True,
                            "note": "a refresh is running; stopping now discards its "
                                    "uncommitted pass"}, 409)
                return
            self._json({"stopped": True})
            threading.Thread(target=_shutdown, daemon=True).start()
            return
        self._json({"error": "not found"}, 404)

    def do_GET(self):
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)

        if path == "/status":
            job = _refresh["job"]
            self._json({"running": _refresh["running"], "line": _refresh["line"],
                        "rc": _refresh["rc"], "job": job,
                        "title": JOBS.get(job, {}).get("title", job),
                        "verb": JOBS.get(job, {}).get("verb", "Running"),
                        "elapsed": int(time.time() - _refresh["started"])
                        if _refresh["started"] else 0})
            return
        if path == "/api/jobs":
            pre = _prereq()
            self._json({
                "jobs": [{"name": k, "title": v["title"], "blurb": v["blurb"],
                          "needs": list(v["needs"]), "since": bool(v.get("since")),
                          "ready": all(pre[n]["ok"] for n in v["needs"]),
                          "why": next((pre[n]["why"] for n in v["needs"]
                                       if not pre[n]["ok"]), "")}
                         for k, v in JOBS.items()],
                "prereq": pre,
                "running": _refresh["running"], "current": _refresh["job"]})
            return
        if path == "/api/llm":
            # The live round trip, not the shape checks: it is the only thing
            # that catches an endpoint which answers but ignores a JSON schema,
            # which is exactly what resolve and topics depend on.
            rc, out = _run(["llm"], timeout=180)
            self._json({"rc": rc, "text": out})
            return
        if path == "/api/stats":
            self._json(stats()); return
        if path == "/api/facets":
            self._json(api_facets()); return
        if path == "/api/trades":
            self._json(api_trades(q)); return
        if path == "/api/top":
            self._json(api_top(q)); return
        if path == "/api/timeline":
            self._json(api_timeline(q)); return
        if path == "/api/ticker":
            self._json(api_ticker((q.get("symbol") or [""])[0], q)); return
        if path == "/api/member":
            self._json(api_member((q.get("name") or [""])[0])); return
        if path.startswith("/api/report/"):
            name = path[len("/api/report/"):]
            if name not in REPORTS:
                self._json({"error": "unknown report"}, 404); return
            rc, out = _run([name, *_report_args(name, q)])
            # Some reports are aligned columns rather than markdown, and running
            # those through the table parser collapses the alignment that IS the
            # output. They declare that in REPORTS rather than being guessed at.
            body = (f"<pre>{html.escape(out)}</pre>" if REPORTS[name].get("pre")
                    else md_to_html(out))
            self._json({"rc": rc, "html": body, "text": out,
                        "refreshing": _refresh["running"]})
            return

        if path == "/":
            self._send(APP)
            return
        if path == "/page":
            f = CONFIG.out_html
            if not f.exists():
                self._send(shell("Full page",
                                 '<div class="err">The rendered page does not exist yet. '
                                 'Refresh to build it.</div>', "page"))
                return
            self._send(f.read_bytes())
            return

        # Server-rendered reports: the no-JavaScript fallback, and what a bookmark
        # or `curl` gets. The app above uses /api/report/* instead.
        if path.startswith("/r/"):
            name = path[3:]
            if name not in REPORTS:
                self._send(shell("Not found", '<div class="err">No such report.</div>'), code=404)
                return
            self._send(shell(REPORTS[name]["title"], report_page(name, q), name))
            return
        if path == "/jobs":
            self._send(shell("Maintenance", jobs_page(
                (q.get("msg") or [""])[0][:200]), "jobs"))
            return
        if path == "/overview":
            self._send(shell("Congress Trades", overview(), ""))
            return

        self._send(shell("Not found", '<div class="err">No such page.</div>'), code=404)


def _shutdown() -> None:
    """Called off the request thread: shutdown() blocks until serve_forever returns,
    and serve_forever cannot return while it is waiting on the handler calling it.

    A refresh subprocess would outlive us and keep the database locked with nothing
    watching it, so it is stopped first. Losing it costs nothing that is not
    resumable: filings and price series are already cached on disk, and the pass
    that dies was uncommitted anyway."""
    time.sleep(0.2)
    stop_refresh()
    if Handler.srv is not None:
        Handler.srv.shutdown()


def stop_refresh() -> None:
    p = _refresh.get("proc")
    if p is None or p.poll() is not None:
        return
    try:
        p.terminate()
        try:
            p.wait(timeout=8)
        except subprocess.TimeoutExpired:
            p.kill()
    except OSError:
        pass


def stop(quiet: bool = False) -> int:
    """Signal a running portal, identified by the pid file."""
    import os
    import signal
    if not PIDFILE.exists():
        if not quiet:
            print("no portal is running (no pid file)")
        return 1
    try:
        pid = int(PIDFILE.read_text().strip())
    except (ValueError, OSError):
        PIDFILE.unlink(missing_ok=True)
        print("stale pid file removed")
        return 1
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        PIDFILE.unlink(missing_ok=True)
        print(f"no process {pid}; stale pid file removed")
        return 1
    except PermissionError:
        print(f"not allowed to signal {pid}")
        return 1
    for _ in range(40):
        time.sleep(0.1)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            print(f"stopped {pid}")
            return 0
    print(f"{pid} did not exit; send SIGKILL yourself if you mean it")
    return 1


def serve(host: str = HOST, port: int = PORT) -> int:
    try:
        httpd = ThreadingHTTPServer((host, port), Handler)
    except OSError as e:
        print(f"cannot bind {host}:{port}: {e}")
        if PIDFILE.exists():
            print(f"  another portal may already be running -- "
                  f"`./run.sh portal stop` (pid {PIDFILE.read_text().strip()})")
        return 1
    Handler.srv = httpd
    try:
        PIDFILE.write_text(str(__import__("os").getpid()))
    except OSError:
        pass
    s = stats()
    print(f"portal: http://{host}:{port}")
    if s and "error" not in s:
        print(f"  {s.get('trades', 0):,} trades, {s.get('members', 0):,} members, "
              f"{s.get('priced', 0):,} priced")
    else:
        print("  no database yet -- hit Refresh in the portal, or run ./run.sh")
    if not CONFIG.contact:
        print("  warning: CONGRESS_CONTACT is unset, so a refresh will stop at `sectors`."
              "\n           Start via ./run.sh portal, or export it first.")
    print("  ctrl-c to stop")
    import signal
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(
        target=httpd.shutdown, daemon=True).start())
    try:
        httpd.serve_forever()
        print("stopped")
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        stop_refresh()
        httpd.server_close()
        PIDFILE.unlink(missing_ok=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("stop", "--stop"):
        return stop()
    host, port = HOST, PORT
    for i, a in enumerate(argv):
        if a == "--port" and i + 1 < len(argv):
            port = int(argv[i + 1])
        elif a.startswith("--port="):
            port = int(a.split("=", 1)[1])
        elif a == "--host" and i + 1 < len(argv):
            host = argv[i + 1]
        elif a.startswith("--host="):
            host = a.split("=", 1)[1]
    return serve(host, port)



# --- the app -----------------------------------------------------------------
# One page, hash-routed. Every control is a query against sqlite rather than a
# filter over a baked payload, so nothing here goes stale between refreshes and
# the whole database stays reachable without reloading.

APP = ("""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Congress Trades</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
<style>""" + CSS + """
.app { min-height:40vh; }
.chartbox { background:var(--panel); border:1px solid var(--line); border-radius:8px;
  padding:1rem 1.1rem 1.2rem; margin:0 0 1.1rem; }
.chartbox h2 { margin:0 0 .15rem; font-size:1.05rem; }
.chartbox .cap { color:var(--ink-3); font-size:12.5px; margin:0 0 .8rem; }
.canvas-wrap { position:relative; width:100%; }
.toggle { display:flex; gap:.3rem; flex-wrap:wrap; margin:0 0 .9rem; }
.toggle button { padding:.28rem .65rem; font-size:12.5px; background:var(--bg);
  border:1px solid var(--line-2); color:var(--ink-2); }
.toggle button[aria-pressed=true] { background:#1b2430; color:var(--white);
  border-color:#3b4657; }
details.tv { margin:.7rem 0 0; } details.tv summary { cursor:pointer; color:var(--ink-3);
  font-size:12.5px; } details.tv table { margin-top:.5rem; }
.nochart { color:var(--ink-3); font-size:13px; padding:1rem 0; }
.gloss { border-bottom:1px dotted var(--ink-3); cursor:help; }
.gloss:focus { outline:1px solid var(--link); outline-offset:2px; }
#tipbox { position:fixed; z-index:40; max-width:330px; background:#0b0e13;
  border:1px solid #2a3038; border-radius:7px; padding:.6rem .75rem; font-size:12.5px;
  line-height:1.45; color:var(--ink); box-shadow:0 10px 30px rgba(0,0,0,.5);
  pointer-events:none; opacity:0; transition:opacity .12s; }
#tipbox.on { opacity:1; }
#tipbox b { color:var(--white); display:block; margin-bottom:.15rem; }
canvas.clickable { cursor:pointer; }
.panel .back { margin-bottom:.6rem; font-size:13px; }
.spin { color:var(--ink-3); font-size:13px; padding:1.2rem 0; }
.filters { display:grid; gap:.6rem; grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
  background:var(--panel); border:1px solid var(--line); border-radius:8px;
  padding:.8rem .9rem; margin:0 0 1rem; }
.filters label { display:block; color:var(--ink-3); font-size:11.5px;
  text-transform:uppercase; letter-spacing:.04em; margin-bottom:.2rem; }
.filters input,.filters select { width:100%; background:var(--bg); color:var(--ink);
  border:1px solid var(--line-2); border-radius:5px; padding:.35rem .45rem;
  font-family:inherit; font-size:13px; }
.filters .chk { display:flex; align-items:center; gap:.4rem; margin-top:1.1rem; }
.filters .chk input { width:auto; }
th.s { cursor:pointer; user-select:none; } th.s:hover { color:var(--ink); }
th.s[data-on]:after { content:attr(data-on); color:var(--link); margin-left:.25rem; }
.pager { display:flex; gap:.6rem; align-items:center; margin:.8rem 0; font-size:13px;
  color:var(--ink-2); }
.mlink { cursor:pointer; color:var(--link); }
.panel { position:fixed; inset:0 0 0 auto; width:min(540px,94vw); background:var(--panel);
  border-left:1px solid var(--line-2); padding:1.2rem; overflow-y:auto; z-index:9;
  box-shadow:-18px 0 40px rgba(0,0,0,.45); }
.panel h2 { margin-top:0; } .panel img { width:84px; border-radius:6px; float:right;
  margin:0 0 .5rem .7rem; }
.panel .x { position:absolute; top:.6rem; right:.8rem; }
.panel table { font-size:12.5px; } .panel td { padding:.3rem .4rem; }
.panel td:nth-child(2) { max-width:120px; overflow:hidden; text-overflow:ellipsis;
  white-space:nowrap; }
.kv { display:grid; grid-template-columns:auto 1fr; gap:.25rem .8rem; font-size:13px;
  margin:.6rem 0; } .kv b { color:var(--ink-3); font-weight:500; }
.pill { display:inline-block; background:#1b2430; border:1px solid var(--line-2);
  border-radius:999px; padding:.1rem .55rem; font-size:12px; margin:.15rem .2rem 0 0; }
</style></head><body>
<h1>Congress Trades</h1>
<nav class="nav" id="nav"></nav>
<div class="bar"><button id="rf">Refresh data</button>
  <button id="st">Stop portal</button>
  <span class="live" id="live"></span>
  <span class="note" id="stat"></span></div>
<div class="app" id="app"><div class="spin">loading...</div></div>
<script>
const $=s=>document.querySelector(s), app=$('#app');
const GLOSS={"vs index": "Median alpha measured against SPY over the identical window. The benchmark is the whole point: +6% in a window where SPY did +7% is behind the market.", "vs sector": "The same alpha measured against the trade's own sector ETF instead of SPY. It separates picking a stock from riding a sector. Unmapped sectors fall back to SPY, and on fund holdings this column is noise, because a fund inherits its sponsor's SIC code.", "beat index": "The share of this member's scored trades whose alpha against the index came out positive. A hit rate, not a size.", "alpha": "Excess return in the direction the member took: a buy scores stock minus benchmark, a sell scores benchmark minus stock. So exiting a name that then lagged the market counts as a win.", "90d ret": "The stock's own raw return over the 90 days after disclosure, before any benchmark is subtracted. Read it next to the benchmark, never alone.", "top name": "The share of this member's scored trades sitting in their single most-traded ticker. A high share means the record is really one bet.", "1st half": "The same member scored separately on the older and the newer half of their own trades. A big gap between the halves means the record is not stable.", "filing lag": "Days between the transaction and its disclosure. The STOCK Act allows 45.", "party unity": "The share of this member's yea/nay votes matching their own party's majority, computed from raw Voteview roll calls.", "DW-NOMINATE": "Voteview's ideology score on the first dimension. Negative is left, positive is right.", "net flow": "Disclosed buying minus disclosed selling for a name. Positive means Congress accumulated it on net; negative means it was sold down.", "disclosed": "The date the filing was made public. Everything here is measured from this date, not the trade date, because nobody outside the filing knew until then.", "disclosure date": "The date the filing was made public. Returns are measured from here, because trade-date returns would flatter these members and mean nothing.", "owner": "Who holds the asset: Self, Spouse, Joint or Dependent.", "amount": "Disclosures give a dollar bracket, never an exact figure. Every total here uses the bracket's lower bound, so it is a floor.", "min amount": "Only count disclosures whose bracket floor is at least this. $1,001-$15,000 is mostly rebalancing noise.", "horizon": "The forward window, in days, over which each return is measured.", "walk-forward": "Re-rank the members every year and grade them on the next one, pooling the folds. Harder to fool than a single split.", "measurable": "A disclosure with a ticker and a closed price window, so a return can actually be computed. Many filings have neither.", "distinct members": "How many different people traded the name. The convergence signal, and the hardest to skew with one heavy trader.", "dollar volume": "Disclosed dollars traded in a name, buys and sells added together. Bracket floors, so a lower bound.", "trade count": "The number of disclosures naming this stock.", "PTR": "Periodic Transaction Report: the filing a member must make within 45 days of a securities trade.", "SPY": "The S&P 500 ETF, used here as the index benchmark.", "sector ETF": "A fund tracking the trade's own industry, used as the second benchmark. The SIC-to-ETF mapping is a judgement call, not a definition.", "min trades": "Members below this many measurable trades are left out entirely: under about ten, a record is one lucky quarter.", "priced": "Disclosures for which a forward return could be computed, because the ticker resolved and the window has closed."};
const REPORTS=""" + json.dumps({k: {"title": v["title"], "blurb": v["blurb"],
                                    "args": {a: t.__name__ for a, t in v["args"].items()},
                                    "defaults": {a: str(d) for a, d in
                                                 v.get("defaults", {}).items()}}
                                for k, v in REPORTS.items()}) + """;
const esc=s=>String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const pct=v=>v==null?'':(v>0?'+':'')+(v*100).toFixed(1)+'%';
const cls=v=>v==null?'num':(v>0?'num pos':v<0?'num neg':'num');
let facets={members:[],tickers:[]};

// --- routing ---------------------------------------------------------------
const routes=()=>[['','Overview'],['trends','Trends'],['explore','Explore'],['page','Full page'],
  ...Object.entries(REPORTS).map(([k,v])=>['r/'+k,v.title]),['jobs','Maintenance']];
function nav(){
  const cur=location.hash.replace(/^#\\//,'');
  $('#nav').innerHTML=routes().map(([h,t])=>
    `<a href="#/${h}"${h===cur?' aria-current="page"':''}>${esc(t)}</a>`).join('');
}
async function route(){
  nav();
  // The panel is fixed-position, so it would otherwise hang over the next view.
  document.querySelectorAll('.panel').forEach(p=>p.remove());
  const cur=location.hash.replace(/^#\\//,'');
  if(cur==='trends') return trends();
  if(cur==='explore') return explore();
  if(cur==='page') return full();
  if(cur==='jobs') return jobs();
  if(cur.startsWith('r/')) return report(cur.slice(2));
  return overview();
}
addEventListener('hashchange',route);

// --- overview --------------------------------------------------------------
async function overview(){
  app.innerHTML='<div class="spin">loading...</div>';
  const s=await (await fetch('/api/stats')).json();
  if(!s||!s.trades){ app.innerHTML='<div class="err"><b>No data yet.</b><p>Press '+
    '<b>Refresh data</b> above. The first pass takes 15-25 minutes, almost all of it '+
    'rate-limited price fetches.</p></div>'; return; }
  const tiles=[['trades','disclosures'],['members','members'],['priced','with 90d returns'],
    ['tickers','distinct tickers']].map(([k,l])=>
    `<div class="stat"><b>${(s[k]||0).toLocaleString()}</b><span>${l}</span></div>`).join('')
    +(s.latest?`<div class="stat"><b>${esc(s.latest)}</b><span>latest disclosure</span></div>`:'');
  const cards=Object.entries(REPORTS).map(([k,v])=>
    `<a class="card" href="#/r/${k}"><b>${esc(v.title)}</b><span>${esc(v.blurb)}</span></a>`).join('');
  app.innerHTML=`<div class="stats">${tiles}</div>
    <div class="chartbox"><h2>Recent activity</h2>
      <p class="cap">Disclosures per month over the last two years, by direction.
        <a href="#/trends">Open Trends</a> for top names and a longer window.</p>
      <div class="canvas-wrap" id="w-act"><canvas id="c-act"></canvas></div></div>
    <p><a href="#/explore"><b>Explore every disclosure</b></a> - filter and sort all
    ${(s.trades||0).toLocaleString()} of them live, and click any member for their record.</p>
    <h2>Reports</h2><div class="cards">${cards}</div>
    <h2>Reading any of this</h2><p>Returns are measured from the <b>disclosure</b> date,
    not the trade date, and <b>vs sector</b> is the honest column: roughly half the apparent
    edge across scored members is sector exposure, not stock selection. Sector alpha on fund
    holdings is noise, since a fund inherits its sponsor's SIC code. Past alpha barely
    predicts future alpha.</p>`;
  annotate(app);
  drawActivity();
}

async function drawActivity(){
  const d=await (await fetch('/api/timeline?days=730')).json();
  if(!d.rows||!d.rows.length){ const w=$('#w-act');
    if(w) w.innerHTML='<div class="nochart">Nothing to plot yet.</div>'; return; }
  const r=d.rows;
  $('#w-act').style.height='240px';
  paint('c-act',{type:'line',data:{labels:r.map(x=>x.ym),
      datasets:[{label:'Buys',data:r.map(x=>x.buys),borderColor:C.buy,backgroundColor:C.buy,
          borderWidth:2,pointRadius:0,pointHoverRadius:5,tension:.25},
        {label:'Sells',data:r.map(x=>x.sells),borderColor:C.sell,backgroundColor:C.sell,
          borderWidth:2,pointRadius:0,pointHoverRadius:5,tension:.25}]},
    options:{maintainAspectRatio:false,responsive:true,
      interaction:{mode:'index',intersect:false},
      // The tooltip shows both lines; a click opens whichever is nearer the cursor.
      onHover:(e,els)=>{e.native.target.style.cursor=els.length?'pointer':'default';},
      onClick:(e,els,chart)=>{
        const hit=chart.getElementsAtEventForMode(e,'nearest',{intersect:false,axis:'xy'},true)[0];
        if(!hit) return;
        const ym=r[hit.index].ym;
        Object.assign(st,{q:'',member:'',ticker:'',chamber:'',floor:'',tickered:'0',
          type:hit.datasetIndex?'sell':'buy',since:[ym+'-01',d.since||''].sort()[1],until:ym+'-31',
          sort:'date',dir:'desc',offset:0});
        if(location.hash!=='#/explore') location.hash='#/explore'; else explore();},
      plugins:{legend:{display:true,position:'top',align:'end',
          labels:{boxWidth:9,boxHeight:9,usePointStyle:true,pointStyle:'line',padding:14}},
        tooltip:Object.assign({},tip,{callbacks:{
          label:c=>`${c.dataset.label}: ${c.parsed.y.toLocaleString()}`,
          footer:()=>'click a line to list them'}})},
      scales:{x:axis({grid:{display:false}}),
        y:axis({beginAtZero:true,ticks:{color:C.faint,padding:6,
          callback:v=>v.toLocaleString()}})}}});
}

// --- explorer --------------------------------------------------------------
const st={q:'',member:'',ticker:'',chamber:'',type:'',floor:'',since:'',until:'',tickered:'0',
  sort:'date',dir:'desc',offset:0,limit:100};
let timer=null;

async function explore(){
  if(!facets.members.length) facets=await (await fetch('/api/facets')).json();
  app.innerHTML=`<div class="filters">
    <div><label>search</label><input id="f-q" placeholder="member, ticker, asset" value="${esc(st.q)}"></div>
    <div><label>member</label><select id="f-member"><option value="">any</option>${
      facets.members.map(m=>`<option value="${esc(m.member)}"${st.member===m.member?' selected':''}>${
        esc(m.full_name||m.member)} (${m.n})</option>`).join('')}</select></div>
    <div><label>ticker</label><select id="f-ticker"><option value="">any</option>${
      facets.tickers.map(t=>`<option value="${esc(t.ticker)}"${st.ticker===t.ticker?' selected':''}>${
        esc(t.ticker)} (${t.n})</option>`).join('')}</select></div>
    <div><label>chamber</label><select id="f-chamber">${
      ['','House','Senate'].map(c=>`<option value="${c}"${st.chamber===c?' selected':''}>${c||'both'}</option>`).join('')}</select></div>
    <div><label>type</label><select id="f-type">${
      ['','buy','sell'].map(c=>`<option value="${c}"${st.type===c?' selected':''}>${c||'any'}</option>`).join('')}</select></div>
    <div><label>min amount</label><input id="f-floor" inputmode="numeric" placeholder="15001" value="${esc(st.floor)}"></div>
    <div><label>since</label><input id="f-since" placeholder="2025-01-01" value="${esc(st.since)}"></div>
    <div><label>until</label><input id="f-until" placeholder="2025-12-31" value="${esc(st.until)}"></div>
    <div class="chk"><input type="checkbox" id="f-tickered"${st.tickered==='1'?' checked':''}>
      <label style="margin:0;text-transform:none;font-size:13px">tickered only</label></div>
  </div><div id="rows"><div class="spin">loading...</div></div>`;

  const bind=(id,key,ev)=>{const el=$(id); if(!el) return;
    el.addEventListener(ev,()=>{ st[key]=el.type==='checkbox'?(el.checked?'1':'0'):el.value;
      st.offset=0; clearTimeout(timer); timer=setTimeout(rows,ev==='input'?260:0); });};
  bind('#f-q','q','input'); bind('#f-member','member','change');
  bind('#f-ticker','ticker','change'); bind('#f-chamber','chamber','change');
  bind('#f-type','type','change'); bind('#f-floor','floor','input');
  bind('#f-since','since','input'); bind('#f-until','until','input');
  bind('#f-tickered','tickered','change');
  rows();
}

async function rows(){
  const p=new URLSearchParams(Object.fromEntries(
    Object.entries(st).filter(([k,v])=>v!=='' && v!=='0' || k==='offset' || k==='limit')));
  const box=$('#rows'); if(!box) return;
  const d=await (await fetch('/api/trades?'+p)).json();
  if(d.error){ box.innerHTML=`<div class="err">${esc(d.error)}</div>`; return; }
  if(!d.rows.length){ box.innerHTML='<p class="note">Nothing matches those filters.</p>'; return; }
  const cols=[['date','disclosed'],['member','member'],['ticker','ticker'],['','asset'],
    ['','type'],['amount','amount'],['ret','90d ret'],['alpha','alpha']];
  const th=cols.map(([k,l])=>k?`<th class="s" data-k="${k}"${st.sort===k?` data-on="${
    st.dir==='asc'?'^':'v'}"`:''}>${l}</th>`:`<th>${l}</th>`).join('');
  const body=d.rows.map(r=>`<tr>
    <td class="num">${esc(r.disclosed||r.tx_date||'')}</td>
    <td><span class="mlink" data-m="${esc(r.member)}">${esc(r.full_name||r.member)}</span>${
      r.party?` <span class="note">${esc(r.party[0])}</span>`:''}</td>
    <td>${r.ticker?`<b class="mlink" data-t="${esc(r.ticker)}">${esc(r.ticker)}</b>`
      :'<span class="note">-</span>'}</td>
    <td>${esc((r.asset_name||'').slice(0,54))}</td>
    <td class="${/^s/i.test(r.tx_type||'')?'neg':'pos'}">${esc(r.tx_type||'')}</td>
    <td class="num">${esc(r.amount_range||'')}</td>
    <td class="${cls(r.ret_90)}">${pct(r.ret_90)}</td>
    <td class="${cls(r.alpha)}">${pct(r.alpha)}</td></tr>`).join('');
  const from=d.offset+1, to=Math.min(d.offset+d.limit,d.total);
  box.innerHTML=`<div class="pager"><b>${d.total.toLocaleString()}</b> matching -
      showing ${from.toLocaleString()}-${to.toLocaleString()}
      <button id="prev"${d.offset?'':' disabled'}>prev</button>
      <button id="next"${to>=d.total?' disabled':''}>next</button></div>
    <div class="tbl-scroll"><table><thead><tr>${th}</tr></thead><tbody>${body}</tbody></table></div>`;
  box.querySelectorAll('th.s').forEach(h=>h.onclick=()=>{
    const k=h.dataset.k;
    st.dir=(st.sort===k&&st.dir==='desc')?'asc':'desc'; st.sort=k; st.offset=0; rows();});
  box.querySelectorAll('.mlink').forEach(e=>e.onclick=()=>
    e.dataset.t?tickerPanel(e.dataset.t):member(e.dataset.m));
  annotate(box.querySelector('thead'));
  const prev=$('#prev'), next=$('#next');
  if(prev) prev.onclick=()=>{st.offset=Math.max(0,st.offset-st.limit); rows();};
  if(next) next.onclick=()=>{st.offset+=st.limit; rows();};
}

// The price line is the context the disclosure counts lack: bars tell you when
// Congress moved, this tells you what the name was doing when they did. Same line
// treatment as Recent activity on the overview, so the two read as one chart type.
function drawPrice(d){
  const p=d.price; if(!p||!(p.labels||[]).length) return;
  const up=(d.agg.net||0)>=0, line=up?C.buy:C.sell;
  const grain=p.bucket==='day'?'Daily closes'
    :p.bucket==='week'?'Weekly closes' : 'Monthly closes';
  // On a name Congress discloses most weeks, ringing every bucket draws the line
  // twice and says nothing. There the rings thin out to the heaviest periods; the
  // bars below still carry the full timing, and a hover still reports every count.
  const hit=p.labels.filter((_,i)=>p.buys[i]||p.sells[i]).length;
  const dense=hit>p.labels.length*.45;
  const nz=p.buys.concat(p.sells).filter(n=>n>0).sort((a,b)=>a-b);
  const cut=dense?Math.max(2,nz[Math.floor(nz.length*.75)]||2):1;
  const keep=arr=>arr.map(n=>n>=cut?n:0);
  const cap=$('#px-cap');
  if(cap) cap.textContent=`${grain} from the last refresh, over the span this name has `+
    `been disclosed in. The line is ${up?'blue: Congress is a net buyer'
      :'red: Congress is a net seller'} of it. `+(dense
      ? `It is disclosed in most ${p.bucket==='month'?'months':p.bucket==='week'?'weeks':'sessions'}, `+
        `so only periods of ${cut} or more are marked -- blue circles are buys, just `+
        `under the line, red diamonds sells, just above it. Hover any point for the `+
        `full count.`
      : `Marks sit where the disclosures land, sized by how many: blue circles are `+
        `buys, just under the line, red diamonds sells, just above it.`);
  // A marker sits on the close, so it reads as a point on the line rather than a
  // second series floating beside it. Radius grows with the square root of the
  // count: area, not radius, is what the eye compares. The rings are hollow and
  // the price line is drawn last, on top of them -- on a name Congress trades
  // every week a wall of filled dots erases the very line it is annotating.
  const dot=n=>n?Math.min(2.6+Math.sqrt(n),6.5):0;
  // Buys ride just under the close and sells just above it, the way a trading
  // chart marks them: a day that carries both would otherwise stack one marker
  // exactly on the other and show only whichever drew last.
  const lo=Math.min(...p.close), hi=Math.max(...p.close), off=(hi-lo)*.03||.01;
  const at=(arr,d)=>p.labels.map((_,i)=>arr[i]?p.close[i]+d*off:null);
  const marker=(arr,color,style,d)=>({data:at(arr,d),showLine:false,pointStyle:style,
    pointRadius:arr.map(dot),pointHoverRadius:arr.map(n=>n?dot(n)+2:0),
    pointBackgroundColor:C.surface,pointBorderColor:color,pointBorderWidth:1.6,
    borderColor:color,backgroundColor:color});
  // The line is drawn first and faded: it is the backdrop, and at full strength a
  // blue net-buyer's line swallows the blue buy rings it is meant to carry. The
  // markers keep the full hue, because that is where blue and red have to mean
  // buy and sell.
  paint('c-px',{type:'line',data:{labels:p.labels,
      datasets:[{label:'Close',data:p.close,borderColor:fade(line,.5),
          backgroundColor:fade(line,.5),borderWidth:1.6,pointRadius:0,
          pointHoverRadius:5,tension:.25},
        Object.assign({label:'Buys'},marker(keep(p.buys),C.buy,'circle',-1)),
        Object.assign({label:'Sells'},marker(keep(p.sells),C.sell,'rectRot',1))]},
    options:{maintainAspectRatio:false,responsive:true,
      interaction:{mode:'index',intersect:false},
      plugins:{legend:{display:true,position:'top',align:'end',
          labels:{boxWidth:9,boxHeight:9,usePointStyle:true,padding:12,
            filter:i=>i.text!=='Close'}},
        // Counts come from the data rather than from the markers, so a bucket
        // whose marker was thinned away still answers when you hover it.
        tooltip:Object.assign({},tip,{filter:i=>i.datasetIndex===0,callbacks:{
          title:i=>p.asof[i[0].dataIndex],
          label:c=>`close ${px(p.close[c.dataIndex])}`,
          afterBody:i=>{const k=i[0].dataIndex, out=[];
            if(p.buys[k]) out.push(`${p.buys[k]} buy${p.buys[k]===1?'':'s'} disclosed`);
            if(p.sells[k]) out.push(`${p.sells[k]} sell${p.sells[k]===1?'':'s'} disclosed`);
            return out;}}})},
      scales:{x:axis({grid:{display:false},ticks:{color:C.faint,maxTicksLimit:6,
          callback:function(v){const l=p.labels[v]||''; return p.bucket==='month'?l:l.slice(0,7);}}}),
        // One precision for the whole axis: px() drops cents above $100, which on a
        // scale crossing it prints $240 next to $80.00.
        y:axis({ticks:{color:C.faint,padding:6,
          callback:v=>hi<100?'$'+v.toFixed(2):'$'+Math.round(v).toLocaleString()}})}}});
}

async function member(name){
  document.querySelectorAll('.panel').forEach(p=>p.remove());
  const d=await (await fetch('/api/member?name='+encodeURIComponent(name))).json();
  const a=d.agg||{}, el=document.createElement('div');
  el.className='panel';
  el.innerHTML=`<button class="x" id="closep">close</button>
    ${d.wiki_thumb?`<img src="${esc(d.wiki_thumb)}" alt="">`:''}
    <h2>${esc(d.full_name||name)}</h2>
    <p class="note">${esc([d.party,d.chamber,d.state,d.district?'district '+d.district:''].filter(Boolean).join(' - '))}</p>
    ${d.wiki_desc?`<p>${esc(d.wiki_desc)}</p>`:''}
    <div class="kv">
      <b>disclosures</b><span>${(a.n||0).toLocaleString()}</span>
      <b>buys / sells</b><span>${a.buys||0} / ${a.sells||0}</span>
      <b>disclosed volume</b><span>at least $${(a.vol||0).toLocaleString()}</span>
      <b>mean alpha</b><span class="${a.alpha>0?'':''}">${a.alpha==null?'not priced yet':pct(a.alpha)}</span>
      <b>mean filing lag</b><span>${a.lag==null?'-':Math.round(a.lag)+' days'}</span>
      ${d.party_unity!=null?`<b>party unity</b><span>${(d.party_unity).toFixed(0)}%</span>`:''}
      ${d.nominate!=null?`<b>DW-NOMINATE</b><span>${d.nominate.toFixed(2)}</span>`:''}
    </div>
    ${(d.top||[]).length?`<h3>Most-traded</h3>
      <p class="note">Bar length is how many disclosures; blue is net buying, red net selling.</p>
      <div class="canvas-wrap" id="w-mem" style="height:${Math.min(d.top.length,12)*22+30}px">
        <canvas id="c-mem"></canvas></div>`:''}
    ${(d.committees||[]).length?`<h3>Committees</h3><div>${d.committees.map(c=>
      `<span class="pill">${esc(c.name||'')}</span>`).join('')}</div>`:''}
    ${d.wiki_url?`<p style="margin-top:1rem"><a href="${esc(d.wiki_url)}" target="_blank" rel="noopener">Wikipedia</a></p>`:''}
    <p><button id="onlyme">show only their trades</button></p>`;
  document.body.appendChild(el);
  annotate(el);
  if((d.top||[]).length){
    // Length is how often they traded the name; colour is which way it went on
    // balance -- blue net buying, red net selling -- the same rule the trend
    // charts use, so a reader carries one reading across the whole portal.
    paint('c-mem',{type:'bar',data:{labels:d.top.map(t=>t.ticker),
        datasets:[{data:d.top.map(t=>t.n),
          backgroundColor:d.top.map(t=>(t.net||0)>=0?C.buy:C.sell),borderWidth:0,
          borderRadius:3,borderSkipped:'start',barPercentage:.8,categoryPercentage:.9}]},
      options:{indexAxis:'y',maintainAspectRatio:false,responsive:true,
        onClick:(ev,els)=>{ if(els&&els.length) tickerPanel(d.top[els[0].index].ticker); },
        onHover:(ev,els)=>{ ev.native.target.style.cursor=els.length?'pointer':'default'; },
        plugins:{legend:{display:false},tooltip:Object.assign({},tip,{callbacks:{
          label:c=>{const t=d.top[c.dataIndex];
            return [`${c.parsed.x} disclosure${c.parsed.x===1?'':'s'}`,
              `${t.net>0?'net buying':t.net<0?'net selling':'balanced'}: ${money(t.net||0)}`];}}})},
        scales:{x:axis({ticks:{color:C.faint,padding:4,precision:0}}),
          y:axis({grid:{display:false},ticks:{color:C.ink,padding:4}})}}});
  }
  $('#closep').onclick=()=>el.remove();
  $('#onlyme').onclick=()=>{ st.member=name; st.offset=0; el.remove();
    if(location.hash!=='#/explore') location.hash='#/explore'; else explore(); };
}

// --- glossary hover layer --------------------------------------------------
// The jargon here is load-bearing -- "vs index" and "vs sector" mean different
// things and the gap between them is the whole story -- so every term explains
// itself in place rather than in a legend nobody scrolls to.
const tipbox=(()=>{const d=document.createElement('div'); d.id='tipbox';
  document.body.appendChild(d); return d;})();
let tiphide=null;
function showTip(el){
  const k=el.dataset.g, txt=GLOSS[k]; if(!txt) return;
  clearTimeout(tiphide);
  tipbox.innerHTML=`<b>${esc(k)}</b>${esc(txt)}`;
  tipbox.classList.add('on');
  const r=el.getBoundingClientRect(), w=Math.min(330,innerWidth-24);
  tipbox.style.width=w+'px';
  const bh=tipbox.offsetHeight;
  let top=r.bottom+8; if(top+bh>innerHeight-8) top=Math.max(8,r.top-bh-8);
  tipbox.style.top=top+'px';
  tipbox.style.left=Math.max(12,Math.min(r.left,innerWidth-w-12))+'px';
}
function hideTip(){ tiphide=setTimeout(()=>tipbox.classList.remove('on'),80); }

const rxSafe=s=>s.replace(/[.*+?^${}()|[\\]\\\\]/g,'\\\\$&');
const GKEYS=Object.keys(GLOSS).sort((a,b)=>b.length-a.length);
const GRX=new RegExp('(?<![\\\\w-])('+GKEYS.map(rxSafe).join('|')+')(?![\\\\w-])','gi');

// Wraps known terms wherever they appear as text. Capped per term so a long
// report does not turn into a field of dotted underlines.
function annotate(root){
  if(!root) return;
  const seen={};
  const walk=document.createTreeWalker(root,NodeFilter.SHOW_TEXT,{acceptNode(n){
    if(!n.nodeValue||!n.nodeValue.trim()) return NodeFilter.FILTER_REJECT;
    const p=n.parentElement;
    if(!p||p.closest('.gloss,a,code,script,style,option,#tipbox'))
      return NodeFilter.FILTER_REJECT;
    return NodeFilter.FILTER_ACCEPT;}});
  const jobs=[]; let n;
  while((n=walk.nextNode())) if(GRX.test(n.nodeValue)){ GRX.lastIndex=0; jobs.push(n); }
  jobs.forEach(node=>{
    const txt=node.nodeValue; let out=null, last=0; GRX.lastIndex=0; let m;
    while((m=GRX.exec(txt))){
      const key=GKEYS.find(k=>k.toLowerCase()===m[1].toLowerCase());
      if(!key) continue;
      seen[key]=(seen[key]||0)+1;
      if(seen[key]>2) continue;                 // first two mentions only
      out=out||document.createDocumentFragment();
      if(m.index>last) out.appendChild(document.createTextNode(txt.slice(last,m.index)));
      const sp=document.createElement('span');
      sp.className='gloss'; sp.dataset.g=key; sp.tabIndex=0;
      sp.setAttribute('aria-label',m[1]+': '+GLOSS[key]);
      sp.textContent=m[1];
      out.appendChild(sp); last=m.index+m[1].length;
    }
    if(out){ if(last<txt.length) out.appendChild(document.createTextNode(txt.slice(last)));
      node.parentNode.replaceChild(out,node); }
  });
}
document.addEventListener('mouseover',e=>{
  const g=e.target.closest&&e.target.closest('.gloss'); if(g) showTip(g);});
document.addEventListener('mouseout',e=>{
  if(e.target.closest&&e.target.closest('.gloss')) hideTip();});
document.addEventListener('focusin',e=>{
  const g=e.target.closest&&e.target.closest('.gloss'); if(g) showTip(g);});
document.addEventListener('focusout',e=>{
  if(e.target.closest&&e.target.closest('.gloss')) hideTip();});
addEventListener('scroll',()=>tipbox.classList.remove('on'),{passive:true});

// --- charts ----------------------------------------------------------------
// Two hues only, and they carry polarity rather than identity: buying vs selling
// is a diverging scale about zero, so blue/red with a neutral zero rule is the
// honest encoding. Both steps are the ones publish.py already uses, and the pair
// validates against this surface for contrast and colour-vision separation.
const C={buy:'#3987e5',sell:'#e66767',ink:'#9aa4b2',faint:'#6b7480',
  grid:'rgba(255,255,255,.07)',surface:'#12161c'};
// Same hue, less weight -- for marks that have to sit behind something else.
const fade=(hex,a)=>`rgba(${parseInt(hex.slice(1,3),16)},${parseInt(hex.slice(3,5),16)},${
  parseInt(hex.slice(5,7),16)},${a})`;
const charts={};
function paint(id,cfg){
  const el=document.getElementById(id); if(!el) return;
  if(typeof Chart==='undefined'){
    el.closest('.canvas-wrap').innerHTML='<div class="nochart">Charts need Chart.js from '+
      'the CDN, which did not load. The numbers are all in the table below.</div>'; return; }
  if(charts[id]){ charts[id].destroy(); }
  Chart.defaults.color=C.ink; Chart.defaults.font.family=
    "-apple-system,Segoe UI,Roboto,sans-serif"; Chart.defaults.font.size=11.5;
  charts[id]=new Chart(el,cfg);
}
const money=v=>{const a=Math.abs(v);
  return (v<0?'-$':'$')+(a>=1e6?(a/1e6).toFixed(1)+'M':a>=1e3?(a/1e3).toFixed(0)+'k':a);};
// Share prices need the cents that money() throws away, and never the k/M step:
// a $1,200 close is $1,200, not $1k.
const px=v=>'$'+Number(v).toLocaleString(undefined,
  {minimumFractionDigits:v<100?2:0,maximumFractionDigits:2});
// Hairline grid, no border, ticks in muted ink -- chrome stays recessive so the
// bars carry the reading.
const axis=(extra={})=>Object.assign({grid:{color:C.grid,drawBorder:false,drawTicks:false},
  border:{display:false},ticks:{color:C.faint,padding:6}},extra);
const tip={backgroundColor:'#0b0e13',borderColor:'#2a3038',borderWidth:1,
  titleColor:'#fff',bodyColor:'#d6dae0',padding:9,displayColors:true,boxPadding:4,
  cornerRadius:6};

function tableView(head,rows){
  return '<details class="tv"><summary>Table view</summary><div class="tbl-scroll"><table><thead><tr>'+
    head.map(h=>`<th>${esc(h)}</th>`).join('')+'</tr></thead><tbody>'+
    rows.map(r=>'<tr>'+r.map((c,i)=>`<td${i?' class="num"':''}>${esc(c)}</td>`).join('')+'</tr>').join('')+
    '</tbody></table></div></details>';
}

// --- trends ----------------------------------------------------------------
const tstate={metric:'net',days:'365',floor:'',chamber:'',unit:'dollars'};
const METRICS=[['net','Net flow'],['volume','Dollar volume'],['count','Trade count'],
  ['members','Distinct members']];

async function trends(){
  app.innerHTML=`
  <div class="filters">
    <div><label>window</label><select id="t-days">${
      [['90','last 90 days'],['180','last 6 months'],['365','last year'],
       ['730','last 2 years'],['0','everything']].map(([v,l])=>
      `<option value="${v}"${tstate.days===v?' selected':''}>${l}</option>`).join('')}</select></div>
    <div><label>chamber</label><select id="t-chamber">${
      ['','House','Senate'].map(c=>`<option value="${c}"${tstate.chamber===c?' selected':''}>${c||'both'}</option>`).join('')}</select></div>
    <div><label>min amount</label><input id="t-floor" inputmode="numeric" placeholder="15001" value="${esc(tstate.floor)}"></div>
  </div>
  <div class="chartbox">
    <h2>Top names</h2>
    <p class="cap" id="t-cap"></p>
    <div class="toggle" id="t-metric">${METRICS.map(([k,l])=>
      `<button data-m="${k}" aria-pressed="${tstate.metric===k}">${l}</button>`).join('')}</div>
    <div class="canvas-wrap" id="w-top"><canvas id="c-top"></canvas></div>
    <div id="t-table"></div>
  </div>
  <div class="chartbox">
    <h2>Buying and selling over time</h2>
    <p class="cap">Disclosures per month, by direction. Amounts are the lower bound of
      each disclosed bracket, so dollar figures are floors rather than exact sums.</p>
    <div class="toggle" id="t-unit">
      <button data-u="dollars" aria-pressed="${tstate.unit==='dollars'}">Dollars</button>
      <button data-u="count" aria-pressed="${tstate.unit==='count'}">Trade count</button></div>
    <div class="canvas-wrap" id="w-time"><canvas id="c-time"></canvas></div>
    <div id="t-time-table"></div>
  </div>`;

  annotate(app.querySelector('.filters'));
  const rerun=()=>{ drawTop(); drawTime(); };
  $('#t-days').onchange=e=>{tstate.days=e.target.value; rerun();};
  $('#t-chamber').onchange=e=>{tstate.chamber=e.target.value; rerun();};
  $('#t-floor').oninput=e=>{tstate.floor=e.target.value; clearTimeout(timer);
    timer=setTimeout(rerun,400);};
  document.querySelectorAll('#t-metric button').forEach(b=>b.onclick=()=>{
    tstate.metric=b.dataset.m;
    document.querySelectorAll('#t-metric button').forEach(x=>
      x.setAttribute('aria-pressed',String(x.dataset.m===tstate.metric)));
    drawTop();});
  document.querySelectorAll('#t-unit button').forEach(b=>b.onclick=()=>{
    tstate.unit=b.dataset.u;
    document.querySelectorAll('#t-unit button').forEach(x=>
      x.setAttribute('aria-pressed',String(x.dataset.u===tstate.unit)));
    drawTime();});
  rerun();
}

function qwin(extra){ return new URLSearchParams(Object.assign(
  {days:tstate.days,chamber:tstate.chamber,floor:tstate.floor},extra||{})); }

async function drawTop(){
  const d=await (await fetch('/api/top?'+qwin({metric:tstate.metric,limit:'18'}))).json();
  const cap=$('#t-cap'), box=$('#t-table');
  if(d.error||!d.rows||!d.rows.length){
    if(cap) cap.textContent='Nothing in this window.';
    $('#w-top').innerHTML='<div class="nochart">No matching trades.</div>';
    if(box) box.innerHTML=''; return; }
  const m=tstate.metric;
  // Colour means direction on every metric here: blue where the name is net
  // bought over the window, red where it is net sold. Length still carries the
  // metric the reader picked, so the two channels answer different questions --
  // how much, and which way. Under net flow they agree by construction, because
  // there the bar itself is the signed number.
  const rows=m==='net'?d.rows.slice().sort((a,b)=>b.net-a.net):d.rows;
  const vals=rows.map(r=>m==='members'?r.members:m==='count'?r.count:m==='volume'?r.volume:r.net);
  const dollars=(m==='net'||m==='volume');
  const hue=' Blue is a name Congress is net buying over this window, red net selling.';
  cap.textContent=(m==='net'
    ? 'Disclosed buying minus selling per name, ordered by size in either direction, because a name Congress dumped says as much as one it bought.'
    : m==='volume' ? 'Disclosed dollars traded per name, buys and sells together.'
    : m==='count' ? 'Number of disclosures per name.'
    : 'How many different members traded the name -- the convergence signal, hardest to skew with one heavy trader.')+hue;
  const colors=rows.map(r=>r.net>=0?C.buy:C.sell);
  $('#w-top').style.height=Math.max(220,rows.length*26+46)+'px';
  paint('c-top',{type:'bar',data:{labels:rows.map(r=>r.ticker),
      datasets:[{label:METRICS.find(x=>x[0]===m)[1],data:vals,backgroundColor:colors,
        borderWidth:0,borderRadius:4,borderSkipped:'start',barPercentage:.82,
        categoryPercentage:.9}]},
    options:{indexAxis:'y',maintainAspectRatio:false,responsive:true,
      layout:{padding:{right:8}},
      onClick:(ev,els)=>{ if(els&&els.length) tickerPanel(rows[els[0].index].ticker); },
      onHover:(ev,els)=>{ ev.native.target.style.cursor=els.length?'pointer':'default'; },
      plugins:{legend:{display:false},tooltip:Object.assign({},tip,{callbacks:{
        label:c=>{const r=rows[c.dataIndex];
          // The bar no longer states its own direction once the metric is a
          // magnitude, so the net figure that drives the colour is spelled out.
          const dir=r.net>0?'net buying':r.net<0?'net selling':'balanced';
          return dollars?[`${METRICS.find(x=>x[0]===m)[1]}: ${money(vals[c.dataIndex])}`,
            `bought ${money(r.buy_vol)} in ${r.buys}, sold ${money(r.sell_vol)} in ${r.sells}`,
            `${r.members} member${r.members===1?'':'s'}`,
            ...(m==='net'?[]:[`${dir}: ${money(r.net)}`])]
            :[`${vals[c.dataIndex].toLocaleString()}`,
              `${r.buys} buys, ${r.sells} sells, ${r.members} members`,
              `${dir}: ${money(r.net)}`];}}})},
      scales:{x:axis({ticks:{color:C.faint,padding:6,
          callback:v=>dollars?money(v):v.toLocaleString()},
        grid:{color:C.grid,drawBorder:false,drawTicks:false}}),
        y:axis({grid:{display:false},ticks:{color:C.ink,padding:6,font:{size:11.5}}})}}});
  box.innerHTML=tableView(['ticker','buys','sells','buy $','sell $','net $','members'],
    rows.map(r=>[r.ticker,r.buys,r.sells,money(r.buy_vol),money(r.sell_vol),
      money(r.net),r.members]));
  annotate(document.querySelector('#t-cap')); annotate(box);
}

async function drawTime(){
  const d=await (await fetch('/api/timeline?'+qwin())).json();
  const box=$('#t-time-table');
  if(d.error||!d.rows||!d.rows.length){
    $('#w-time').innerHTML='<div class="nochart">No matching trades.</div>';
    if(box) box.innerHTML=''; return; }
  const r=d.rows, dollars=tstate.unit==='dollars';
  const buy=r.map(x=>dollars?x.buy_vol:x.buys), sell=r.map(x=>dollars?x.sell_vol:x.sells);
  $('#w-time').style.height='300px';
  // Both series are the same measure in the same unit, so they share one axis --
  // a second y-scale here would invent a relationship that is not in the data.
  paint('c-time',{type:'bar',data:{labels:r.map(x=>x.ym),
      datasets:[{label:'Buys',data:buy,backgroundColor:C.buy,borderWidth:0,borderRadius:3,
          borderSkipped:'start',categoryPercentage:.78,barPercentage:.92},
        {label:'Sells',data:sell,backgroundColor:C.sell,borderWidth:0,borderRadius:3,
          borderSkipped:'start',categoryPercentage:.78,barPercentage:.92}]},
    options:{maintainAspectRatio:false,responsive:true,
      interaction:{mode:'index',intersect:false},
      plugins:{legend:{display:true,position:'top',align:'end',
          labels:{boxWidth:9,boxHeight:9,usePointStyle:true,pointStyle:'rect',padding:14}},
        tooltip:Object.assign({},tip,{callbacks:{
          label:c=>`${c.dataset.label}: ${dollars?money(c.parsed.y):c.parsed.y.toLocaleString()}`,
          afterBody:i=>{const x=r[i[0].dataIndex];
            return [`${x.members} members active`];}}})},
      scales:{x:axis({grid:{display:false}}),
        y:axis({ticks:{color:C.faint,padding:6,
          callback:v=>dollars?money(v):v.toLocaleString()}})}}});
  box.innerHTML=tableView(['month','buys','sells','buy $','sell $','members'],
    r.map(x=>[x.ym,x.buys,x.sells,money(x.buy_vol),money(x.sell_vol),x.members]));
}

async function tickerPanel(sym){
  document.querySelectorAll('.panel').forEach(p=>p.remove());
  const d=await (await fetch('/api/ticker?symbol='+encodeURIComponent(sym))).json();
  const el=document.createElement('div'); el.className='panel';
  if(d.error||!d.agg||!d.agg.n){
    el.innerHTML=`<button class="x" id="closep">close</button><h2>${esc(sym)}</h2>
      <p class="note">${esc(d.error||'No disclosures name this ticker.')}</p>`;
    document.body.appendChild(el); $('#closep').onclick=()=>el.remove(); return; }
  const a=d.agg;
  el.innerHTML=`<button class="x" id="closep">close</button>
    <h2>${esc(d.ticker)}</h2>
    <p class="note">${esc(d.asset_name||'')}${d.sector?' - '+esc(d.sector):''}</p>
    <div class="kv">
      <b>disclosures</b><span>${a.n.toLocaleString()}</span>
      <b>buys / sells</b><span>${a.buys} / ${a.sells}</span>
      <b>net flow</b><span class="${a.net>0?'pos':a.net<0?'neg':''}">${money(a.net)}</span>
      <b>dollar volume</b><span>${money((a.buy_vol||0)+(a.sell_vol||0))}</span>
      <b>distinct members</b><span>${a.members}</span>
      <b>vs index</b><span class="${d.alpha_med>0?'pos':d.alpha_med<0?'neg':''}">${
        d.alpha_med==null?'not priced yet':pct(d.alpha_med)+' median over '+d.alpha_n+' priced'}</span>
      <b>first seen</b><span>${esc(a.first_seen||'')}</span>
      <b>latest</b><span>${esc(a.last_seen||'')}</span>
    </div>
    ${(d.price&&(d.price.labels||[]).length>2)?`<h3>Price trend</h3>
      <p class="cap" id="px-cap"></p>
      <div class="canvas-wrap" id="w-px" style="height:190px"><canvas id="c-px"></canvas></div>`
      :`<p class="note">No cached closes for this name, so there is no price line.
        <code>prices</code> fetches them on a refresh.</p>`}
    ${d.months.length>1?`<h3>Disclosures per month</h3>
      <div class="canvas-wrap" id="w-tk" style="height:150px"><canvas id="c-tk"></canvas></div>`:''}
    <h3>Who traded it</h3>
    <div class="tbl-scroll"><table><thead><tr><th>member</th><th>buys</th><th>sells</th>
      <th>net</th></tr></thead><tbody>${d.members.map(m=>`<tr>
      <td><span class="mlink" data-m="${esc(m.member)}">${esc(m.full_name||m.member)}</span></td>
      <td class="num">${m.buys}</td><td class="num">${m.sells}</td>
      <td class="num ${m.net>0?'pos':m.net<0?'neg':''}">${money(m.net)}</td></tr>`).join('')}
      </tbody></table></div>
    <h3>Most recent</h3>
    <div class="tbl-scroll"><table><thead><tr><th>disclosed</th><th>member</th><th>type</th>
      <th>amount</th><th>alpha</th></tr></thead><tbody>${d.recent.map(r=>`<tr>
      <td class="num">${esc(r.disclosed||r.tx_date||'')}</td>
      <td>${esc((r.full_name||r.member||'').slice(0,22))}</td>
      <td class="${/^s/i.test(r.tx_type||'')?'neg':'pos'}">${esc(r.tx_type||'')}</td>
      <td class="num">${esc(r.amount_range||'')}</td>
      <td class="${cls(r.alpha)}">${pct(r.alpha)}</td></tr>`).join('')}</tbody></table></div>
    <p style="margin-top:1rem"><button id="onlytk">show only this stock</button></p>`;
  document.body.appendChild(el);
  drawPrice(d);
  if(d.months.length>1){
    paint('c-tk',{type:'bar',data:{labels:d.months.map(m=>m.ym),
        datasets:[{label:'Buys',data:d.months.map(m=>m.buys),backgroundColor:C.buy,
            borderWidth:0,borderRadius:2,borderSkipped:'start',categoryPercentage:.8},
          {label:'Sells',data:d.months.map(m=>m.sells),backgroundColor:C.sell,
            borderWidth:0,borderRadius:2,borderSkipped:'start',categoryPercentage:.8}]},
      options:{maintainAspectRatio:false,responsive:true,
        interaction:{mode:'index',intersect:false},
        plugins:{legend:{display:true,position:'top',align:'end',
            labels:{boxWidth:8,boxHeight:8,usePointStyle:true,pointStyle:'rect',padding:10}},
          tooltip:tip},
        scales:{x:axis({grid:{display:false},ticks:{color:C.faint,maxTicksLimit:6}}),
          y:axis({beginAtZero:true,ticks:{color:C.faint,precision:0}})}}});
  }
  el.querySelectorAll('.mlink').forEach(e=>e.onclick=()=>member(e.dataset.m));
  $('#closep').onclick=()=>el.remove();
  $('#onlytk').onclick=()=>{ st.ticker=d.ticker; st.member=''; st.q=''; st.offset=0;
    el.remove(); if(location.hash!=='#/explore') location.hash='#/explore'; else explore(); };
  annotate(el);
}

// --- reports ---------------------------------------------------------------
const ropts={};
async function report(name){
  const spec=REPORTS[name]; if(!spec){ app.innerHTML='<div class="err">No such report.</div>'; return; }
  const o=ropts[name]=ropts[name]||{};
  const ctl=Object.entries(spec.args).map(([k,t])=>{
    if(t==='bool') return `<div class="chk"><input type="checkbox" id="o-${k}"${o[k]?' checked':''}>
      <label style="margin:0;text-transform:none;font-size:13px">${esc(k.replace('-',' '))}</label></div>`;
    const dflt=(spec.defaults||{})[k];
    if(k==='horizon'){ const cur=o[k]||dflt||'90';
      return `<div><label>${k}</label><select id="o-${k}">${
      ['30','90'].map(v=>`<option${cur===v?' selected':''}>${v}</option>`).join('')}</select></div>`; }
    return `<div><label>${esc(k.replace('-',' '))}</label><input id="o-${k}" inputmode="numeric"
      placeholder="${esc(dflt==null?'':dflt)}" value="${esc(o[k]==null?'':o[k])}"></div>`;
  }).join('');
  app.innerHTML=(ctl?`<div class="filters">${ctl}</div>`:'')+'<div id="out"><div class="spin">running...</div></div>';
  Object.keys(spec.args).forEach(k=>{ const el=$('#o-'+k); if(!el) return;
    el.addEventListener(el.type==='checkbox'||el.tagName==='SELECT'?'change':'input',()=>{
      o[k]=el.type==='checkbox'?(el.checked?'1':''):el.value;
      clearTimeout(timer); timer=setTimeout(()=>run(name),el.tagName==='INPUT'&&el.type!=='checkbox'?400:0);});});
  run(name);
}
async function run(name){
  const out=$('#out'); if(!out) return;
  const p=new URLSearchParams(Object.entries(ropts[name]||{}).filter(([,v])=>v!==''&&v!=null));
  out.innerHTML='<div class="spin">running...</div>';
  const d=await (await fetch(`/api/report/${name}?`+p)).json();
  out.innerHTML=(d.refreshing?'<p class="note">A refresh is running; these are the '+
    'pre-refresh numbers until it commits.</p>':'')+
    (d.rc?`<div class="err">Exited ${d.rc}.</div>`:'')+d.html;
  annotate(out);
}

// --- maintenance: the jobs that write, and the model they need --------------
async function jobs(){
  app.innerHTML='<div class="spin">loading...</div>';
  const d=await (await fetch('/api/jobs')).json();
  const p=d.prereq;
  const row=(k,label)=>`<div><b>${esc(label)}</b> <span class="${p[k].ok?'num pos':'num neg'}">`+
    `${p[k].ok?'ok':'not ready'}</span> &mdash; ${esc(p[k].detail)}</div>`;
  const cards=d.jobs.map(j=>{
    const dis=(!j.ready||d.running)?' disabled':'';
    const since=j.since?`<input id="since" value="2025-01-01" inputmode="numeric"
      style="max-width:9rem;margin-right:.5rem" aria-label="fetch from this date">`:'';
    const why=!j.ready?`<p class="note neg">${esc(j.why)}</p>`
      :(d.running?'<p class="note">Another job is running; only one writes at a time.</p>':'');
    return `<div class="chartbox"><h2>${esc(j.title)}</h2>
      <p class="cap">${esc(j.blurb)}</p>${why}
      <div>${since}<button data-job="${esc(j.name)}"${dis}>${esc(j.title)}</button></div></div>`;
  }).join('');
  app.innerHTML=`<div class="chartbox"><h2>Model</h2>
      <p class="cap">What <code>advise</code>, <code>resolve</code> and <code>topics</code>
      will use. Set it in <code>.env</code>; a shell export beats the file.</p>
      ${row('model','Model')}${row('key','Congress.gov key')}${row('titles','Meeting titles')}
      <p class="note">The check below is a live round trip. Its last step asks for JSON
      constrained by a schema &mdash; the part <code>resolve</code> and <code>topics</code>
      rest on, and the part an endpoint can silently ignore while still answering.</p>
      <div><button id="llmck">Check model</button></div>
      <pre id="llmout" style="display:none"></pre></div>
    <h2>Jobs</h2>
    <p class="note">Each of these writes to the database, so only one runs at a time and
    they share the slot with a refresh. Progress shows in the bar at the top of the page.</p>
    ${cards}`;
  $('#llmck').onclick=async e=>{
    const b=e.target, out=$('#llmout');
    b.disabled=true; b.textContent='checking...';
    out.style.display=''; out.textContent='running a live round trip...';
    try{ const r=await (await fetch('/api/llm')).json();
      out.textContent=r.text||'(no output)';
      out.className=r.rc?'err':'';
    }catch(err){ out.textContent='could not reach the server'; }
    b.disabled=false; b.textContent='Check model';
  };
  app.querySelectorAll('button[data-job]').forEach(b=>{ b.onclick=async()=>{
    const since=$('#since')?('?since='+encodeURIComponent($('#since').value.trim())):'';
    b.disabled=true;
    const r=await (await fetch('/job/'+b.dataset.job+since,{method:'POST'})).json();
    if(!r.started){ alert(r.why||'could not start'); b.disabled=false; return; }
    tick(); jobs();
  };});
}

function full(){
  app.innerHTML='<p class="note">The rendered static page, exactly as `publish` writes it. '+
    '<a href="/page" target="_blank" rel="noopener">Open in its own tab</a>.</p>'+
    '<iframe src="/page" title="rendered page"></iframe>';
}

// --- refresh + live stats ---------------------------------------------------
async function tick(){
  try{
    const s=await (await fetch('/status')).json();
    const b=$('#rf'), l=$('#live');
    if(s.running){ b.disabled=true; b.textContent=(s.verb||'Running')+'...';
      l.textContent=(s.line||'')+(s.elapsed?`  (${Math.floor(s.elapsed/60)}m${s.elapsed%60}s)`:'');
      setTimeout(tick,1500);
    } else {
      b.disabled=false; b.textContent='Refresh data';
      if(s.rc===0){ l.textContent=(s.title||'done')+' finished'; statline(); route();
        setTimeout(()=>l.textContent='',4000); }
      else if(s.rc!=null){ l.textContent=(s.title||'job')+' failed: '+(s.line||'see the terminal'); }
    }
  }catch(e){ $('#live').textContent='lost contact with the server'; }
}
async function statline(){
  try{ const s=await (await fetch('/api/stats')).json();
    if(s&&s.trades) $('#stat').textContent=
      `${s.trades.toLocaleString()} trades - ${s.members} members - ${s.priced.toLocaleString()} priced`;
    // Warn before a refresh dies at `sectors` rather than after.
    const old=$('#nocontact'); if(old) old.remove();
    if(s&&!s.contact){ const w=document.createElement('div'); w.className='err'; w.id='nocontact';
      w.innerHTML='<b>CONGRESS_CONTACT is not set.</b> Collection will stop at '+
        '<code>sectors</code>: the SEC answers 403 to anonymous clients. Start the portal '+
        'with <code>./run.sh portal</code>, or export it before launching.';
      app.parentNode.insertBefore(w,app); }
  }catch(e){}
}
$('#rf').onclick=async()=>{ $('#rf').disabled=true; $('#live').textContent='starting...';
  await fetch('/refresh',{method:'POST'}); tick(); };

$('#st').onclick=async()=>{
  if(!confirm('Stop the portal? The page stays open but stops working until you '+
              'restart it with ./run.sh portal')) return;
  let r=await fetch('/shutdown',{method:'POST'});
  if(r.status===409){
    const d=await r.json();
    if(!confirm((d.note||'A refresh is running.')+'\\n\\nStop anyway?')) return;
    r=await fetch('/shutdown?force=1',{method:'POST'});
  }
  $('#st').disabled=true; $('#rf').disabled=true;
  $('#live').textContent='portal stopped - restart with ./run.sh portal';
};

statline(); route(); tick();
</script></body></html>""").encode("utf-8")

if __name__ == "__main__":
    raise SystemExit(main())
