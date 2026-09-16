"""Committee meeting dates, and whether members trade around their own hearings.

This is the actual insider thesis. The static committee-overlap flag in the
digest only says a member traded a sector their committee oversees, which is
unsurprising -- people invest in what they know. The testable claim is about
TIMING: did the trade land just before a hearing or markup that committee held?

Meetings come from the Congress.gov API, which needs a free key:

    CONGRESS_API_KEY=...        # https://api.congress.gov/sign-up/

Every other source was tried and rejected. docs.house.gov's calendar redirects to
an error page, senate.gov publishes only the current week, and govinfo's CHRG
sitemaps are published transcripts whose coverage collapses for recent years --
183 packages for 2026 against ~1,300 for a completed year. Building on that would
have meant testing timing on a subsample biased toward older trades.

Committees are matched on the four-character root of their system code, so a
subcommittee meeting counts for the parent committee's members. That is the right
jurisdiction rule: a member sitting on the full committee has visibility into its
subcommittees' business.
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import os
import time
from pathlib import Path

import requests

from . import db
from .config import CONFIG, user_agent

# A snapshot ships with the checkout so `timing` works with no signup at all.
# Meetings that have already happened never change, so committed history stays
# correct; only the tail goes stale, and a key tops that up incrementally.
SEED = Path(__file__).resolve().parent.parent / "seed" / "committee-meetings.json.gz"

API = "https://api.congress.gov/v3/committee-meeting"
CONGRESSES = (117, 118, 119)          # 2021-2022, 2023-2024, 2025-2026
PAUSE = 0.75                          # the key allows 5,000 requests an hour


def _key() -> str:
    k = os.getenv("CONGRESS_API_KEY", "").strip()
    if not k:
        raise SystemExit(
            "CONGRESS_API_KEY is not set. Committee meeting dates come from the\n"
            "Congress.gov API; a key is free at https://api.congress.gov/sign-up/\n"
            "and every keyless source lacks historical coverage (see this module's\n"
            "docstring).")
    return k


def _get(url: str, cfg, **params):
    params.update({"format": "json", "api_key": _key()})
    r = requests.get(url, params=params, timeout=30,
                     headers={"User-Agent": user_agent(cfg)})
    if r.status_code == 429:                  # over the hourly allowance
        time.sleep(60)
        r = requests.get(url, params=params, timeout=30,
                         headers={"User-Agent": user_agent(cfg)})
    r.raise_for_status()
    return r.json()


def list_events(congress: int, cfg=CONFIG, progress=None) -> list[dict]:
    """Every meeting id for one congress. The list carries no date or committee,
    so this is only an index into the detail fetches."""
    out, offset = [], 0
    while True:
        d = _get(f"{API}/{congress}", cfg, limit=250, offset=offset)
        got = d.get("committeeMeetings") or []
        out += [{"eventId": m["eventId"], "chamber": m["chamber"].lower()} for m in got]
        total = (d.get("pagination") or {}).get("count", 0)
        offset += len(got)
        if progress:
            progress(f"  congress {congress}: {len(out)}/{total} event ids")
        if not got or offset >= total:
            return out
        time.sleep(PAUSE)


def _cache_path(cfg, congress: int, chamber: str, event_id: str):
    d = cfg.cache_dir / "meetings"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{congress}-{chamber}-{event_id}.json"


def detail(congress: int, chamber: str, event_id: str, cfg=CONFIG) -> dict | None:
    """One meeting, cached on disk. Past meetings never change, so a cached file
    is final and makes the whole fetch resumable after an interruption."""
    path = _cache_path(cfg, congress, chamber, event_id)
    if path.exists():
        try:
            return json.loads(path.read_text()) or None
        except ValueError:
            pass
    try:
        got = _get(f"{API}/{congress}/{chamber}/{event_id}", cfg)
    except requests.RequestException:
        return None                            # transient: no cache write, retry later
    m = got.get("committeeMeeting") or {}
    if not m:
        path.write_text("{}")
        return None
    rec = {"event_id": event_id, "congress": congress, "chamber": m.get("chamber", ""),
           "date": (m.get("date") or "")[:10], "type": m.get("type") or "",
           "title": (m.get("title") or "")[:300],
           "roots": sorted({(c.get("systemCode") or "")[:4].lower()
                            for c in (m.get("committees") or [])
                            if c.get("systemCode")})}
    path.write_text(json.dumps(rec, separators=(",", ":")))
    return rec


def collect(cfg=CONFIG, congresses=CONGRESSES, quiet: bool = False) -> int:
    say = (lambda *_: None) if quiet else print
    with db.connect(cfg.db_path) as conn:
        known = {r["event_id"] for r in
                 conn.execute("SELECT event_id FROM committee_meetings")}
    if known:
        say(f"  {len(known)} meetings already stored; fetching only what is new")
    rows = []
    for cg in congresses:
        ids = list_events(cg, cfg, progress=say)
        # Listing is cheap; detail fetches are not. Skipping ids already stored
        # turns a re-run into a top-up of the tail instead of the whole history.
        ids = [e for e in ids if e["eventId"] not in known]
        say(f"  congress {cg}: {len(ids)} new meetings to fetch")
        for i, ev in enumerate(ids):
            # Sleep only for requests that actually go out. Pausing on cache hits
            # too would make a resumed run cost the same two hours as the first
            # one, which defeats the point of caching at all.
            cached = _cache_path(cfg, cg, ev["chamber"], ev["eventId"]).exists()
            rec = detail(cg, ev["chamber"], ev["eventId"], cfg)
            if rec and rec["date"] and rec["roots"]:
                rows.append(rec)
            if not quiet and i % 250 == 0:
                say(f"  congress {cg}: {i}/{len(ids)} meetings "
                    f"({'cached' if cached else 'fetched'})")
            if not cached:
                time.sleep(PAUSE)

        _store(rows, cfg)
        say(f"  congress {cg}: stored, {len(rows)} meetings so far")
    print(f"stored {len(rows)} meetings with a date and a committee")
    return 0


def _store(rows, cfg) -> None:
    with db.connect(cfg.db_path) as conn:
        for r in rows:
            conn.execute(
                """INSERT OR REPLACE INTO committee_meetings
                   (event_id, congress, chamber, meeting_date, type, title, roots)
                   VALUES(?,?,?,?,?,?,?)""",
                (r["event_id"], r["congress"], r["chamber"], r["date"], r["type"],
                 r["title"], ",".join(r["roots"])))


# ---------------------------------------------------------------- seed / export
def seed(cfg=CONFIG, force: bool = False, quiet: bool = False) -> int:
    """Load the committed snapshot into the database.

    Runs automatically when the table is empty, so a fresh clone can answer the
    timing question without an API key or a two-hour fetch. Existing rows win
    unless `force`, since a live fetch is always fresher than the snapshot.
    """
    if not SEED.exists():
        if not quiet:
            print(f"no snapshot at {SEED}")
        return 1
    payload = json.loads(gzip.decompress(SEED.read_bytes()))
    rows = payload["meetings"]
    with db.connect(cfg.db_path) as conn:
        have = conn.execute("SELECT count(*) FROM committee_meetings").fetchone()[0]
        if have and not force:
            if not quiet:
                print(f"{have} meetings already stored; snapshot not applied "
                      "(use --seed to force)")
            return 0
        verb = "INSERT OR REPLACE" if force else "INSERT OR IGNORE"
        for r in rows:
            conn.execute(
                f"""{verb} INTO committee_meetings
                    (event_id, congress, chamber, meeting_date, type, title, roots)
                    VALUES(?,?,?,?,?,?,?)""",
                (r["event_id"], r["congress"], r["chamber"], r["date"], r["type"],
                 r.get("title", ""), ",".join(r["roots"])))
    if not quiet:
        print(f"seeded {len(rows)} meetings from the snapshot "
              f"(as of {payload.get('as_of', 'unknown')}); "
              "run `committees` with CONGRESS_API_KEY to top up the tail")
    return 0


def export(cfg=CONFIG, quiet: bool = False) -> int:
    """Rewrite the committed snapshot from the database."""
    with db.connect(cfg.db_path) as conn:
        # Titles are omitted deliberately: the analysis reads only date, type and
        # committee roots, and titles are five sixths of the file. A live fetch
        # fills them in for anyone who wants them.
        rows = [{"event_id": r["event_id"], "congress": r["congress"],
                 "chamber": r["chamber"], "date": r["meeting_date"],
                 "type": r["type"], "roots": (r["roots"] or "").split(",")}
                for r in conn.execute(
                    "SELECT * FROM committee_meetings ORDER BY meeting_date")]
    if not rows:
        print("nothing to export")
        return 1
    payload = {"as_of": dt.date.today().isoformat(),
               "source": "https://api.congress.gov/v3/committee-meeting",
               "note": "US government public-domain data. Held meetings are final; "
                       "re-run `congress-trades committees` with a key to extend. "
                       "Titles omitted; the analysis does not use them.",
               "congresses": sorted({r["congress"] for r in rows}),
               "span": [rows[0]["date"], rows[-1]["date"]],
               "meetings": rows}
    SEED.parent.mkdir(parents=True, exist_ok=True)
    SEED.write_bytes(gzip.compress(
        json.dumps(payload, separators=(",", ":")).encode(), 9))
    if not quiet:
        print(f"wrote {SEED.name}: {len(rows)} meetings, "
              f"{SEED.stat().st_size/1024:.0f} KB, span {payload['span'][0]} to "
              f"{payload['span'][1]}")
    return 0


# ---------------------------------------------------------------- the actual test
def _member_roots(conn) -> dict[str, set[str]]:
    """member name -> the committee roots they sit on."""
    bio = {r["bioguide"]: r["member"] for r in
           conn.execute("SELECT member, bioguide FROM congress_members "
                        "WHERE bioguide IS NOT NULL AND bioguide != ''")}
    out: dict[str, set[str]] = {}
    for r in conn.execute("SELECT bioguide, key FROM member_committees"):
        name = bio.get(r["bioguide"])
        if name:
            out.setdefault(name, set()).add((r["key"] or "")[:4].lower())
    return out


def ensure_meetings(cfg=CONFIG, quiet: bool = True) -> int:
    """Number of meetings available, seeding from the snapshot if the table is
    empty. Called by `timing` so the analysis works on a fresh clone."""
    with db.connect(cfg.db_path) as conn:
        have = conn.execute("SELECT count(*) FROM committee_meetings").fetchone()[0]
    if have:
        return have
    seed(cfg, quiet=quiet)
    with db.connect(cfg.db_path) as conn:
        return conn.execute("SELECT count(*) FROM committee_meetings").fetchone()[0]


def load(floor: int = 1, window: int = 30, cfg=CONFIG):
    """Each priced trade, with the signed distance in days to the nearest meeting
    of a committee that member actually sits on.

    Negative means the trade came BEFORE the meeting, which is the direction the
    insider thesis predicts. Only meetings within `window` days are considered;
    beyond that the nearest meeting says nothing, since busy committees meet
    constantly.
    """
    import datetime as dt

    from . import scorecard
    with db.connect(cfg.db_path) as conn:
        roots = _member_roots(conn)
        meetings: dict[str, list] = {}
        for r in conn.execute("SELECT meeting_date, roots, type FROM committee_meetings "
                              "WHERE meeting_date != ''"):
            for root in (r["roots"] or "").split(","):
                if root:
                    meetings.setdefault(root, []).append(
                        (dt.date.fromisoformat(r["meeting_date"]), r["type"]))
        for v in meetings.values():
            v.sort()
        rows = [dict(r) for r in conn.execute(
            """SELECT t.member, t.ticker, t.tx_type, t.tx_date, t.disclosed,
                      t.amount_min, r.ret_90, r.bench_90, r.sec_90,
                      COALESCE(NULLIF(t.disclosed,''), t.tx_date) AS d0
                 FROM congress_trades t
                 JOIN trade_returns r ON r.trade_id = t.id
                WHERE t.ticker != '' AND t.amount_min >= ? AND t.tx_date != ''
                  AND r.ret_90 IS NOT NULL AND r.bench_90 IS NOT NULL""", (floor,))]

    out, no_seat, no_meeting = [], 0, 0
    for r in rows:
        mine = roots.get(r["member"])
        if not mine:
            no_seat += 1
            continue
        try:
            td = dt.date.fromisoformat(r["tx_date"])
        except ValueError:
            continue
        best = None
        for root in mine:
            for mdate, mtype in meetings.get(root, ()):
                delta = (td - mdate).days           # <0: trade before the meeting
                if abs(delta) <= window and (best is None or abs(delta) < abs(best[0])):
                    best = (delta, mtype, root)
        if best is None:
            no_meeting += 1
            continue
        r["days"], r["mtype"], r["root"] = best
        r["alpha"] = scorecard.alpha(r["tx_type"], r["ret_90"], r["bench_90"])
        if r["alpha"] is not None:
            out.append(r)
    return out, {"no_seat": no_seat, "no_meeting_in_window": no_meeting,
                 "considered": len(rows)}


DAY_BUCKETS = ((-30, -15), (-14, -8), (-7, -3), (-2, -1), (0, 0), (1, 2), (3, 7),
               (8, 14), (15, 30))


def by_days(rows) -> list[dict]:
    """One row per bucket, with two intervals.

    Nine buckets tested at 90% will throw up about one apparent hit by chance, so
    a bare "significant" here is close to meaningless. Each flagged bucket is
    re-tested at a level corrected for how many buckets were examined
    (Bonferroni: 1 - 0.10/k), and only `sig_corrected` is worth reading.
    """
    import statistics

    from . import backtest
    usable = [(lo, hi, [r for r in rows if lo <= r["days"] <= hi])
              for lo, hi in DAY_BUCKETS]
    usable = [(lo, hi, sel) for lo, hi, sel in usable if len(sel) >= 8]
    k = max(1, len(usable))
    corrected = 1 - 0.10 / k
    out = []
    for lo, hi, sel in usable:
        a = [r["alpha"] for r in sel]
        months = [(r["d0"][:7], r["alpha"]) for r in sel]
        clo, chi = backtest._ci_clustered(months)
        sig = bool(clo is not None and (clo > 0 or chi < 0))
        blo, bhi = (backtest._ci_clustered(months, level=corrected)
                    if sig else (None, None))
        # Who is in the bucket matters as much as how big it is: a "Congress trades
        # ahead of hearings" result carried by four members is a claim about four
        # people.
        counts: dict[str, int] = {}
        for r in sel:
            counts[r["member"]] = counts.get(r["member"], 0) + 1
        top3 = sum(sorted(counts.values(), reverse=True)[:3]) / len(sel)
        out.append({"lo": lo, "hi": hi, "n": len(sel),
                    "med": statistics.median(a), "mean": statistics.fmean(a),
                    "clo": clo, "chi": chi, "sig": sig,
                    "blo": blo, "bhi": bhi,
                    "sig_corrected": bool(blo is not None and (blo > 0 or bhi < 0)),
                    "members": len(counts), "top3_share": top3,
                    "buckets_tested": k, "corrected_level": round(corrected, 4)})
    return out


def before_vs_after(rows, days: int = 7) -> dict:
    import statistics

    from . import backtest

    def side(sel):
        if len(sel) < 8:
            return {"n": len(sel), "med": None, "clo": None, "chi": None}
        a = [r["alpha"] for r in sel]
        clo, chi = backtest._ci_clustered([(r["d0"][:7], r["alpha"]) for r in sel])
        return {"n": len(a), "med": statistics.median(a), "mean": statistics.fmean(a),
                "clo": clo, "chi": chi,
                "sig": bool(clo is not None and (clo > 0 or chi < 0))}
    return {"window": days,
            "before": side([r for r in rows if -days <= r["days"] < 0]),
            "after": side([r for r in rows if 0 < r["days"] <= days]),
            "rest": side([r for r in rows if abs(r["days"]) > days])}


def build(floor: int = 1, window: int = 30, cfg=CONFIG) -> dict:
    ensure_meetings(cfg)
    rows, counts = load(floor, window, cfg)
    return {"n": len(rows), "window": window, "coverage": counts,
            "buckets": by_days(rows), "split": before_vs_after(rows),
            "types": sorted({r["mtype"] for r in rows})}


def to_markdown(d: dict) -> str:
    from . import scorecard
    p = scorecard.pct
    c = d["coverage"]
    L = [f"# Committee timing — do members trade around their own hearings?", "",
         f"{d['n']:,} priced trades fall within {d['window']} days of a meeting held "
         "by a committee that member sits on. Of "
         f"{c['considered']:,} considered, {c['no_seat']:,} had no committee seat on "
         f"record and {c['no_meeting_in_window']:,} had no relevant meeting in the "
         "window.", "",
         "Negative days mean the trade came **before** the meeting, which is the "
         "direction the insider thesis predicts. Committees are matched on the root "
         "of their system code, so a subcommittee meeting counts for the parent "
         "committee's members. Intervals resample whole months.", "",
         "| days from meeting | trades | members | top 3 members | median α "
         "| 90% by month |",
         "|---|--:|--:|--:|--:|:--:|"]
    for b in d["buckets"]:
        rng = ("day of" if b["lo"] == b["hi"] == 0
               else f"{b['lo']} to {b['hi']}")
        ci = "—" if b["clo"] is None else f"{p(b['clo'])} to {p(b['chi'])}"
        mark = " **" if b.get("sig_corrected") else (" *(uncorrected)*" if b["sig"]
                                                     else "")
        L.append(f"| {rng}{mark} | {b['n']:,} | {b['members']} "
                 f"| {b['top3_share']*100:.0f}% | {p(b['med'])} | {ci} |")

    s = d["split"]
    L += ["", f"## Within {s['window']} days before vs after", "",
          "| group | trades | median α | 90% by month |", "|---|--:|--:|:--:|"]
    for k, label in (("before", f"{s['window']} days before a meeting"),
                     ("after", f"{s['window']} days after"),
                     ("rest", "further out")):
        v = s[k]
        ci = "—" if v["clo"] is None else f"{p(v['clo'])} to {p(v['chi'])}"
        L.append(f"| {label}{' **' if v.get('sig') else ''} | {v['n']:,} "
                 f"| {p(v['med'])} | {ci} |")

    raw = [b for b in d["buckets"] if b["sig"]]
    real = [b for b in d["buckets"] if b.get("sig_corrected")]
    k = d["buckets"][0]["buckets_tested"] if d["buckets"] else 0
    L += [""]
    if not raw:
        L.append("**No timing bucket has an interval that misses zero.** Trading "
                 "close to a hearing of one's own committee is not measurably "
                 "different from trading at any other time.")
    elif not real:
        names = ", ".join(f"{b['lo']} to {b['hi']} days" for b in raw)
        L += [f"**Nothing survives the correction for testing {k} buckets.** "
              f"{len(raw)} bucket(s) miss zero at a plain 90% level ({names}), but "
              f"{k} buckets tested at 90% produce about {0.1*k:.1f} apparent hits by "
              "chance alone, and none of them still misses zero once the level is "
              "corrected for that. Treat the ** rows as noise, not as timing "
              "signal."]
    else:
        L.append("Rows marked ** survive a level corrected for testing "
                 f"{k} buckets, which is the only version worth reading.")
    L += ["",
          "### Why even a surviving bucket is not the insider thesis", "",
          "- **Returns are measured from the disclosure date, not the trade date.** "
          "A trade placed 10 days before a hearing has its 90-day window start "
          "whenever it was filed, up to 45 days later -- by which point the hearing "
          "is long past. This test locates trades in time relative to meetings; it "
          "cannot measure a return earned *through* the meeting.",
          "- **A real information effect should be strongest nearest the event and "
          "fall away.** Check the gradient: if a middle bucket is up while the days "
          "immediately before a meeting are flat, that shape argues for noise, not "
          "for foreknowledge.",
          "- **Proximity is not jurisdiction.** These rows say the member sits on a "
          "committee that met near the trade, not that the committee had any "
          "business with the company traded. Busy committees meet weekly.",
          "- **Check the member columns.** A bucket whose top three members supply "
          "half its trades is a claim about three people, whatever its interval."]
    return "\n".join(L) + "\n"


def selftest(cfg=CONFIG):
    with db.connect(cfg.db_path) as conn:
        stored = conn.execute("SELECT count(*) FROM committee_meetings").fetchone()[0]
    stored = stored or ensure_meetings(cfg)
    if not stored:
        print("selftest skipped: no meetings and no snapshot "
              "(run `congress-trades committees`)")
        return
    rows, counts = load(cfg=cfg)
    assert counts["considered"] >= len(rows)
    assert all(abs(r["days"]) <= 30 for r in rows), "trade outside its own window"
    assert all(r["alpha"] is not None for r in rows)
    bs = by_days(rows)
    assert all(b["n"] >= 8 for b in bs), "bucket reported below the minimum"
    for b in bs:
        # The corrected interval is strictly wider, so it can never turn a
        # non-result into a result.
        if b["sig"] and b["blo"] is not None:
            assert b["blo"] <= b["clo"] and b["bhi"] >= b["chi"], \
                "corrected interval is not wider than the plain one"
        assert not (b["sig_corrected"] and not b["sig"]), \
            "corrected significance without plain significance"
        assert 0 < b["top3_share"] <= 1 and b["members"] >= 1
    s = before_vs_after(rows)
    assert s["before"]["n"] + s["after"]["n"] + s["rest"]["n"] <= len(rows)
    txt = to_markdown(build(cfg=cfg))
    assert "Committee timing" in txt and "before" in txt
    print(f"selftest ok: {stored:,} meetings, {len(rows):,} trades matched, "
          f"{len(bs)} buckets")
