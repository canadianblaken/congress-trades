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

from . import annual, db, prices, scorecard
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


# --- model picker --------------------------------------------------------------
# Every provider the Maintenance page offers. A cloud provider's endpoint is fixed
# here and never taken from the request: the endpoint is where the API key gets
# sent, so a page that could rewrite it could hand your key to anyone. Only the
# local and custom entries take a base URL from the form.
# Suggestions are a starting point; "List models" asks the provider itself.
PRESETS = [
    {"id": "anthropic", "label": "Anthropic (Claude)", "provider": "anthropic",
     "base": "https://api.anthropic.com", "key": True,
     "models": ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5",
                "claude-fable-5-1"]},
    {"id": "openai", "label": "OpenAI", "provider": "openai",
     "base": "https://api.openai.com/v1", "key": True},
    {"id": "google", "label": "Google (Gemini)", "provider": "openai",
     "base": "https://generativelanguage.googleapis.com/v1beta/openai", "key": True},
    {"id": "xai", "label": "xAI (Grok)", "provider": "openai",
     "base": "https://api.x.ai/v1", "key": True},
    {"id": "mistral", "label": "Mistral", "provider": "openai",
     "base": "https://api.mistral.ai/v1", "key": True},
    {"id": "deepseek", "label": "DeepSeek", "provider": "openai",
     "base": "https://api.deepseek.com/v1", "key": True},
    {"id": "groq", "label": "Groq", "provider": "openai",
     "base": "https://api.groq.com/openai/v1", "key": True},
    {"id": "openrouter", "label": "OpenRouter (many providers)", "provider": "openai",
     "base": "https://openrouter.ai/api/v1", "key": True},
    {"id": "together", "label": "Together AI", "provider": "openai",
     "base": "https://api.together.xyz/v1", "key": True},
    {"id": "ollama", "label": "Local: Ollama", "provider": "ollama",
     "base": "http://127.0.0.1:11434", "key": False, "editable": True},
    {"id": "local", "label": "Local: LiteLLM / LM Studio / vLLM", "provider": "openai",
     "base": "http://127.0.0.1:4000/v1", "key": False, "editable": True},
    {"id": "custom", "label": "Other OpenAI-compatible endpoint", "provider": "openai",
     "base": "", "key": False, "editable": True},
]
_PRESET = {p["id"]: p for p in PRESETS}
MODEL_KEYS = ("CONGRESS_LLM_PRESET", "CONGRESS_LLM_PROVIDER", "CONGRESS_LLM_BASE",
              "CONGRESS_LLM_MODEL", "CONGRESS_LLM_KEY", "CONGRESS_OLLAMA_BASE",
              "CONGRESS_OLLAMA_MODEL")


def model_current() -> dict:
    """What is configured, for the form. The key itself never leaves the server."""
    import os
    g = lambda k: os.getenv(k, "").strip()
    provider = g("CONGRESS_LLM_PROVIDER").lower() or "openai"
    preset = g("CONGRESS_LLM_PRESET")
    if preset not in _PRESET:
        preset = {"ollama": "ollama", "anthropic": "anthropic"}.get(provider, "local")
    ollama = provider == "ollama"
    key = g("CONGRESS_LLM_KEY") or (g("ANTHROPIC_API_KEY") if provider == "anthropic" else "")
    from . import llm
    return {"preset": preset, "login": llm.anthropic_login(),
            "base": g("CONGRESS_OLLAMA_BASE" if ollama else "CONGRESS_LLM_BASE")
                    or _PRESET[preset]["base"],
            "model": g("CONGRESS_OLLAMA_MODEL" if ollama else "CONGRESS_LLM_MODEL"),
            "key_set": bool(key), "key_tail": key[-4:] if len(key) >= 12 else ""}


def _model_settings(body: dict, for_save: bool):
    """(preset, llm.Settings) from a form submission, or raise ValueError."""
    from . import llm
    import os
    preset = _PRESET.get(str(body.get("preset", "")))
    if not preset:
        raise ValueError("unknown provider")
    base = preset["base"]
    if preset.get("editable"):
        base = str(body.get("base", "")).strip().rstrip("/")
        if not re.fullmatch(r"https?://[^\s/]+(/[^\s]*)?", base):
            raise ValueError("the endpoint must be an http(s) URL")
    model = str(body.get("model", "")).strip()
    if for_save and not re.fullmatch(r"[\w.:/@+-]{1,200}", model):
        raise ValueError("pick a model")
    key = str(body.get("key", "")).strip()
    if any(c in key for c in "\r\n") or len(key) > 500:
        raise ValueError("that does not look like an API key")
    login = preset["provider"] == "anthropic" and model_current()["login"]
    if not key and not login and model_current()["preset"] == preset["id"]:
        # Blank means "keep the saved one" -- but only for the same provider, so
        # switching from one company to another never sends the old key along.
        key = os.getenv("CONGRESS_LLM_KEY", "").strip() or (
            os.getenv("ANTHROPIC_API_KEY", "").strip()
            if preset["provider"] == "anthropic" else "")
    if preset["key"] and not key and not login:
        raise ValueError(f"{preset['label']} needs an API key"
                         + (", or sign in with `ant auth login`"
                            if preset["provider"] == "anthropic" else ""))
    return preset, llm.Settings(provider=preset["provider"], model=model or "-",
                                base=base, key=key)


def _write_env(updates: dict[str, str]) -> None:
    """Set KEY=value lines in .env in place, keeping every other line and comment.
    Mode 600, because it now holds API keys."""
    import os
    from .config import ENV_FILE
    lines = ENV_FILE.read_text().splitlines() if ENV_FILE.exists() else []
    left = dict(updates)
    out = []
    for line in lines:
        k = line.split("=", 1)[0].strip().removeprefix("export ").strip()
        if k in left:
            v = left.pop(k)
            if v:
                out.append(f"{k}={v}")
        else:
            out.append(line)
    out += [f"{k}={v}" for k, v in left.items() if v]
    tmp = ENV_FILE.with_suffix(".tmp")
    tmp.write_text("\n".join(out) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(ENV_FILE)


def model_save(body: dict) -> dict:
    """Write the choice to .env and into this process's environment, which every
    job and report subprocess inherits -- so it applies to the next run without a
    restart. A shell that exports these before starting the portal is overridden
    here on purpose: the newest choice is the one on screen."""
    import os
    preset, st = _model_settings(body, for_save=True)
    ollama = st.provider == "ollama"
    updates = {"CONGRESS_LLM_PRESET": preset["id"], "CONGRESS_LLM_PROVIDER": st.provider}
    if ollama:
        updates |= {"CONGRESS_OLLAMA_BASE": st.base, "CONGRESS_OLLAMA_MODEL": st.model}
    else:
        updates |= {"CONGRESS_LLM_BASE": st.base if st.provider == "openai" else "",
                    "CONGRESS_LLM_MODEL": st.model, "CONGRESS_LLM_KEY": st.key}
    _write_env(updates)
    for k, v in updates.items():
        if v:
            os.environ[k] = v
        else:
            os.environ.pop(k, None)
    return model_current()


def model_list(body: dict) -> list[str]:
    from . import llm
    return llm.list_models(_model_settings(body, for_save=False)[1])


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

# The portal's stylesheet, shared by the no-JavaScript pages below and the app.
WEB = Path(__file__).resolve().parent / "web"
CSS = (WEB / "portal.css").read_text(encoding="utf-8")

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
         "ret": "r.ret_90", "alpha": "alpha(t.tx_type, r.ret_90, r.bench_90)"}


def _ro():
    conn = sqlite3.connect(f"file:{CONFIG.db_path}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    # The one definition of "did this trade work", callable from SQL. Every query
    # here uses it instead of spelling the formula out: the copies this replaced
    # had drifted -- sorting ignored a sell's direction, and exchanges scored as buys.
    conn.create_function("alpha", 3, scorecard.alpha, deterministic=True)
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
        r["alpha"] = scorecard.alpha(r["tx_type"], r["ret_90"], r["bench_90"])
    return {"rows": out, "total": total, "limit": limit, "offset": offset}


def api_names() -> dict:
    """ticker -> a readable name, for hover text."""
    if not CONFIG.db_path.exists():
        return {}
    try:
        with _ro() as conn:
            return db.ticker_names(conn)
    except sqlite3.Error:
        return {}


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
            """SELECT alpha(t.tx_type, r.ret_90, r.bench_90)
                 FROM congress_trades t JOIN trade_returns r ON r.trade_id = t.id
                WHERE t.ticker = ?""",
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
            """SELECT t.member, COALESCE(m.full_name,'') full_name, COALESCE(m.party,'') party,
                      t.tx_type, t.tx_date,
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
        r["alpha"] = scorecard.alpha(r["tx_type"], r["ret_90"], r["bench_90"])
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
                      AVG(JULIANDAY(t.disclosed) - JULIANDAY(t.tx_date)) lag
                 FROM congress_trades t WHERE t.member = ?""", (name,)).fetchone()
        prof["agg"] = dict(agg) if agg else {}
        prof["top"] = [dict(r) for r in conn.execute(
            """SELECT ticker, COUNT(*) n,
                      SUM(CASE WHEN LOWER(tx_type) LIKE 'b%' THEN amount_min
                               WHEN LOWER(tx_type) LIKE 's%' THEN -amount_min
                               ELSE 0 END) net
                 FROM congress_trades
                WHERE member = ? AND ticker != '' GROUP BY ticker
                ORDER BY n DESC LIMIT 12""", (name,)).fetchall()]
        prof["annual"] = annual.member_summary(conn, name)
        conn.close()
        prof["compliance"] = _member_compliance(name)
        prof["finance"] = _member_finance(prof.get("bioguide"))
        prof["score"] = _member_score(name)
        return prof
    except sqlite3.Error as e:
        return {"error": str(e)}


# The other disclosure streams, joined onto one person. Each comes from the module
# that owns its rules -- compliance decides what counts as late, finance what counts
# as in-jurisdiction PAC money -- so the panel can never disagree with the report.
_whole: dict = {}


def _cached(name: str, fn):
    """A whole-dataset result, rebuilt only when the database file has changed."""
    stamp = CONFIG.db_path.stat().st_mtime if CONFIG.db_path.exists() else 0
    hit = _whole.get(name)
    if not hit or hit[0] != stamp:
        try:
            hit = _whole[name] = (stamp, fn())
        except Exception:                      # a report that cannot build is absent, not fatal
            hit = _whole[name] = (stamp, None)
    return hit[1]


def _member_compliance(name: str) -> dict | None:
    from . import compliance
    d = _cached("compliance", lambda: compliance.build(CONFIG))
    m = ((d or {}).get("members") or {}).get(name)
    return {k: m.get(k) for k in ("total_trades", "late_count", "late_share",
                                  "worst_lag", "median_late_lag")} if m else None


def _member_score(name: str) -> dict:
    """The scoreboard's own figures for this member, or why there are none. The
    panel used to average alpha itself -- a mean over every trade, where the
    scoreboard takes a median over closed windows above the default floor -- and
    the two could disagree in sign for the same person."""
    d = _cached("scorecard", lambda: {m["member"]: m for m in scorecard.members(
        CONFIG.default_floor, "90", cfg=CONFIG)}) or {}
    m = d.get(name)
    if not m:
        return {"min": scorecard.MIN_TRADES}
    o = m["overall"]
    return {"med": o["med"], "smed": o["smed"], "beat": o["beat"], "n": o["n"]}


def _member_finance(bioguide: str | None) -> dict | None:
    from . import finance
    if not bioguide:
        return None
    d = _cached("finance", lambda: finance.run(None, CONFIG))
    return next((o for o in (d or {}).get("overlaps") or []
                 if o.get("bioguide") == bioguide), None)


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

    def _same_origin(self) -> bool:
        """Only this portal's own page may change the model. Without this, any
        website open in the same browser could POST here -- or reach it through a
        rebound DNS name -- and point your API key at a server of its choosing."""
        port = self.server.server_address[1]
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        origin = self.headers.get("Origin")
        return (self.headers.get("Host") in hosts
                and (origin is None or origin.removeprefix("http://") in hosts)
                and (self.headers.get("Content-Type") or "").startswith("application/json"))

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not 0 < n <= 8192:
            return {}
        try:
            d = json.loads(self.rfile.read(n))
        except ValueError:
            return {}
        return d if isinstance(d, dict) else {}

    def do_POST(self):
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)
        if path in ("/api/model", "/api/models"):
            if not self._same_origin():
                self._json({"error": "refused: not from this portal's page"}, 403)
                return
            from . import llm
            try:
                if path == "/api/model":
                    self._json({"current": model_save(self._body())})
                else:
                    self._json({"models": model_list(self._body())})
            except ValueError as e:
                self._json({"error": str(e)}, 400)
            except llm.LLMError as e:
                self._json({"error": str(e)}, 502)
            return
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

        if path.startswith("/static/") and path[8:] in STATIC:
            self._send((WEB / path[8:]).read_bytes(), STATIC[path[8:]])
            return
        if path == "/status":
            job = _refresh["job"]
            self._json({"running": _refresh["running"], "line": _refresh["line"],
                        "rc": _refresh["rc"], "job": job,
                        "title": JOBS.get(job, {}).get("title", job),
                        "verb": JOBS.get(job, {}).get("verb", "Running"),
                        "elapsed": int(time.time() - _refresh["started"])
                        if _refresh["started"] else 0})
            return
        if path == "/api/model":
            self._json({"presets": PRESETS, "current": model_current()})
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
        if path == "/api/names":
            self._json(api_names()); return
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

APP = (WEB / "portal.html").read_text(encoding="utf-8").replace(
    "__REPORTS__", json.dumps({k: {"title": v["title"], "blurb": v["blurb"],
                                   "args": {a: t.__name__ for a, t in v["args"].items()},
                                   "defaults": {a: str(d) for a, d in
                                                v.get("defaults", {}).items()}}
                               for k, v in REPORTS.items()})).encode("utf-8")
# Served at /static/<name>. A fixed list, so a URL can never name another file.
STATIC = {"portal.css": "text/css; charset=utf-8",
          "portal.js": "text/javascript; charset=utf-8",
          "common.js": "text/javascript; charset=utf-8"}

if __name__ == "__main__":
    raise SystemExit(main())
