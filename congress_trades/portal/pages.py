"""HTML: the app shell, and the server-rendered pages that work without JavaScript."""
from __future__ import annotations

import html
import json
import re
from pathlib import Path

from ..config import CONFIG
from .api import stats
from .jobs import JOBS, MODE, REPORTS, _prereq, _refresh, _report_args, _run

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
            return "num up"
        if t.startswith("-"):
            return "num down"
        return "num"
    return ""


# --- page shell --------------------------------------------------------------

# The portal's stylesheet, shared by the no-JavaScript pages below and the app.
WEB = Path(__file__).resolve().parents[1] / "web"
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


# What each fixed tab is for, shown on hover. Report tabs use their blurb in
# jobs.REPORTS, so every tab's description lives in Python, in one place.
TABS = {
    "": ("Overview", "Headline numbers, buying and selling per month (click a point "
                     "to list those trades), and a door into every report."),
    "trends": ("Trends", "The most-traded names by net flow, dollar volume, trade "
                         "count or how many members traded them, and buying against "
                         "selling month by month. Click a bar to see who traded it."),
    "explore": ("Explore", "Every disclosure, searchable and sortable: filter by "
                           "member, ticker, chamber, direction, size or date, and open "
                           "anyone's full record."),
    "watch": ("Watchlist", "Members and tickers you follow (your own holdings, say). "
                           "Every trade they touch raises an alert and appears in the "
                           "digest. Personal: never on the shareable page."),
    "page": ("Full page", "The self-contained page `publish` writes: one HTML file "
                          "you can save, share or open without the portal."),
    "jobs": ("Maintenance", "Refresh the data, choose the AI model, and run the "
                            "jobs that write to the database, one at a time."),
}


def _tab(href: str, label: str, tip: str, current: bool) -> str:
    return (f'<a href="{href}" title="{html.escape(tip)}"'
            f'{" aria-current=page" if current else ""}>{html.escape(label)}</a>')


def shell(title: str, body: str, current: str = "") -> bytes:
    nav = [_tab("/", *TABS[""], current == ""),
           _tab("/page", *TABS["page"], current == "page")]
    nav += [_tab(f"/r/{key}", spec["title"], spec["blurb"], current == key)
            for key, spec in REPORTS.items()]
    if not MODE["read_only"]:
        nav.append(_tab("/jobs", *TABS["jobs"], current == "jobs"))
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


# --- the app -----------------------------------------------------------------
# One page, hash-routed. Every control is a query against sqlite rather than a
# filter over a baked payload, so nothing here goes stale between refreshes and
# the whole database stays reachable without reloading.

_app: dict[bool, bytes] = {}


def app_html() -> bytes:
    """The app shell, with the report and tab tables and the mode baked in."""
    ro = MODE["read_only"]
    if ro not in _app:
        _app[ro] = (WEB / "portal.html").read_text(encoding="utf-8").replace(
            "__REPORTS__", json.dumps({k: {"title": v["title"], "blurb": v["blurb"],
                                           "group": v.get("group", "briefings"),
                                           "args": {a: t.__name__ for a, t in v["args"].items()},
                                           "defaults": {a: str(d) for a, d in
                                                        v.get("defaults", {}).items()}}
                                       for k, v in REPORTS.items()})).replace(
            "__TABS__", json.dumps({k: {"title": t, "blurb": b} for k, (t, b) in TABS.items()})
        ).replace("__READ_ONLY__", json.dumps(ro)).encode("utf-8")
    return _app[ro]
# Served at /static/<name>. A fixed list, so a URL can never name another file.
STATIC = {"portal.css": "text/css; charset=utf-8",
          "common.css": "text/css; charset=utf-8",
          "portal.js": "text/javascript; charset=utf-8",
          "common.js": "text/javascript; charset=utf-8"}

