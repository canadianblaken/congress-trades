"""What each committee meeting was actually about, so timing can ask a sharper question.

`timing` currently locates a trade relative to any meeting of any committee the
member sits on, and its own closing caveat says why that is weak:

    Proximity is not jurisdiction. These rows say the member sits on a committee
    that met near the trade, not that the committee had any business with the
    company traded. Busy committees meet weekly.

A committee that meets weekly puts almost every trade within 30 days of one of its
meetings, so the treatment group is nearly everybody and the test is diluted to
near nothing. The fix needs subject matter, which means the meeting titles -- and
the shipped snapshot omits them on purpose ("Titles omitted; the analysis does not
use them"), so every one of the 9,639 stored meetings has an empty title.

Two stages, because they fail for different reasons and one of them is slow:

  fetch  Titles from the Congress.gov API, which needs a free key. Roughly
         0.75s per meeting, so the full back catalogue is about two hours --
         resumable, and each meeting is cached on disk forever because a meeting
         that has happened never changes. --since limits it to the years you
         actually score.
  tag    A model reads each title and returns the sectors that hearing bears on,
         from the same closed vocabulary ticker_sectors uses. It must be able to
         answer "none": most hearings are nominations, budgets, oversight of an
         agency or procedural business, and touch no traded industry at all. A
         tagger that found a sector in everything would rebuild the very problem
         this module exists to fix.

The result is stored beside the meetings, never merged into them, and `timing`
only consults it when asked for the sector-matched arm.
"""
from __future__ import annotations

import datetime as dt
import time

from . import committees, db, llm, sectors
from .config import CONFIG

BATCH = 25          # titles are short; a bigger batch than the asset pass fits

SYSTEM = """\
You read titles of US congressional committee meetings and say which industries, \
if any, the meeting bears on.

Return for each title:
- sectors: every industry from the allowed list whose companies would be directly \
affected by the subject of this meeting. Usually zero or one; rarely more than \
two. Return an EMPTY list for meetings about nominations, budgets and \
appropriations, agency oversight, internal procedure, foreign policy, \
investigations of individuals, or anything else with no specific industry \
subject. Most meetings are like this, and an empty list is the correct and \
expected answer -- do not reach for a loose connection. "Oversight of the \
Department of Defense" is agency oversight, not Transportation Equipment; \
"Reauthorization of the Defense Production Act for munitions" does bear on it.
Pick the sector whose companies are IN the business the meeting is about, and \
prefer the most specific one available. These are the pairs that get confused, \
and a tag one sector off never matches the trade it should:
- Chip design and fabrication is Semiconductors, not Electronics & Electrical \
Equipment.
- Drilling, leasing and extraction of oil, gas and minerals is Mining & Energy \
Extraction. Petroleum Refining is refineries and fuel processing only.
- Railroads, airlines, trucking, shipping and pipelines are Transportation & \
Logistics. The companies that BUILD aircraft, rail cars, ships and vehicles are \
Transportation Equipment.
- Hospitals, insurers and care providers are Healthcare Services. Drug and \
chemical makers are Pharma & Chemicals. Device and diagnostics makers are \
Instruments & Medical Devices.
- Banks and lenders are Banking & Finance; Insurance is its own sector.
- Software, cloud and online platforms are Software & IT Services. Telecom and \
broadcast carriers are Communications, and the equipment they buy is \
Communications Equipment.
- Electric, gas and water utilities are Utilities & Power.
Never answer "Unclassified" — that marks a company whose industry is unknown, \
not a subject. If no sector fits, return an empty list.

- subject: the subject in plain words, at most 12 words, no bill numbers.
- confidence: high only when the title names its subject outright.

Judge only what the title says. Do not infer a committee's usual jurisdiction \
from its name, and never guess at a bill's contents from its number alone. Return \
one object per input, echoing its index i, for every index you were given.\
"""


def schema() -> dict:
    return {
        "type": "object",
        "properties": {"items": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "i": {"type": "integer"},
                "sectors": {"type": "array",
                            "items": {"type": "string",
                                      "enum": list(sectors.VOCAB)}},
                "subject": {"type": "string"},
                "confidence": {"type": "string",
                               "enum": ["high", "medium", "low"]},
            },
            "required": ["i", "sectors", "subject", "confidence"],
            "additionalProperties": False}}},
        "required": ["items"], "additionalProperties": False}


# ------------------------------------------------------------------- stage 1
def fetch(cfg=CONFIG, since: str = "", limit: int = 0, quiet: bool = False) -> int:
    """Fill in the titles the snapshot left out. Needs CONGRESS_API_KEY.

    Only meetings that are missing a title are fetched, so an interrupted run
    resumes where it stopped and a second run costs nothing.
    """
    say = (lambda *a: None) if quiet else print
    # Before anything else, and before printing a time estimate for work that is
    # about to not happen: the key is the whole prerequisite here.
    committees._key()
    committees.ensure_meetings(cfg)
    with db.connect(cfg.db_path) as conn:
        rows = [dict(r) for r in conn.execute(
            """SELECT event_id, congress, chamber, meeting_date
                 FROM committee_meetings
                WHERE (title IS NULL OR title = '') AND meeting_date >= ?
                ORDER BY meeting_date DESC""", (since or "",))]
    if limit:
        rows = rows[:limit]
    if not rows:
        say("every stored meeting already has a title")
        return 0
    # PAUSE is the wait between requests; the request itself adds ~0.2s, so
    # counting the pause alone under-promises by a fifth. Measured at 0.95s per
    # meeting on an idle machine -- and noticeably worse while a local model is
    # loaded, which is worth knowing before you run this next to `tag`.
    say(f"fetching {len(rows):,} meeting titles "
        f"(~{len(rows) * (committees.PAUSE + 0.2) / 60:.0f} min on an idle "
        "machine; cached, so this resumes)")

    got, empty, fetched = 0, 0, 0
    pending: list[tuple[str, str]] = []

    def flush():
        if not pending:
            return
        with db.connect(cfg.db_path) as conn:
            conn.executemany("UPDATE committee_meetings SET title = ? "
                             "WHERE event_id = ?", pending)
        pending.clear()

    for i, r in enumerate(rows, 1):
        chamber = (r["chamber"] or "").lower()
        # Sleep only for requests that actually go out, exactly as collect() does.
        # Pausing on cache hits would make a resumed run cost the same as the
        # first one, which defeats the point of caching at all.
        cached = committees._cache_path(cfg, r["congress"], chamber,
                                        r["event_id"]).exists()
        rec = committees.detail(r["congress"], chamber, r["event_id"], cfg)
        title = (rec or {}).get("title", "")
        if title:
            pending.append((title, r["event_id"]))
            got += 1
        else:
            empty += 1
        if not cached:
            fetched += 1
            time.sleep(committees.PAUSE)
        if i % 100 == 0:
            flush()          # checkpoint, so an interrupted run keeps its work
            say(f"  [{i:>5}/{len(rows)}] {got:,} titles, {empty:,} without one")
    flush()
    say(f"{got:,} titles stored; {empty:,} meetings the API has no title for"
        + (f" ({fetched:,} fetched, {len(rows) - fetched:,} already cached)"
           if fetched != len(rows) else ""))
    return 0


# ------------------------------------------------------------------- stage 2
def ask_batch(titles: list[str], s: llm.Settings) -> dict[int, dict]:
    listing = "\n".join(f"{i}. {t}" for i, t in enumerate(titles))
    prompt = (f"Tag these {len(titles)} committee meeting titles. Return exactly "
              f"{len(titles)} objects, one per index.\n\n{listing}")
    got = llm.ask_json(prompt, SYSTEM, schema(), s=s)
    items = got.get("items") if isinstance(got, dict) else got
    if not isinstance(items, list):
        raise llm.LLMError(f"expected a list of items, got {type(items).__name__}")
    out: dict[int, dict] = {}
    for it in items:
        if isinstance(it, dict) and isinstance(it.get("i"), int) \
                and 0 <= it["i"] < len(titles) and it["i"] not in out:
            out[it["i"]] = it
    return out


def tag(cfg=CONFIG, limit: int = 0, refresh: bool = False, dry_run: bool = False,
        quiet: bool = False) -> int:
    say = (lambda *a: None) if quiet else print
    with db.connect(cfg.db_path) as conn:
        have_titles = conn.execute(
            "SELECT count(*) FROM committee_meetings "
            "WHERE title IS NOT NULL AND title != ''").fetchone()[0]
    # The cheaper prerequisite first: no point loading a model to tag nothing.
    if not have_titles:
        raise SystemExit(
            "no meeting has a title yet, so there is nothing to tag. Run\n"
            "  congress-trades topics --stage fetch --since 2025-01-01\n"
            "first; it needs a free CONGRESS_API_KEY from "
            "https://api.congress.gov/sign-up/")
    s = llm.settings()
    say(f"tagging with {llm.preflight(s)}")

    with db.connect(cfg.db_path) as conn:
        done = {} if refresh else {
            r["event_id"]: r["model"] for r in
            conn.execute("SELECT event_id, model FROM meeting_topics")}
        rows = [dict(r) for r in conn.execute(
            """SELECT event_id, title, meeting_date FROM committee_meetings
                WHERE title IS NOT NULL AND title != ''
                ORDER BY meeting_date DESC""")]
    todo = [r for r in rows if done.get(r["event_id"]) != s.model]
    cached = len(rows) - len(todo)
    if limit:
        todo = todo[:limit]
    if not todo:
        say(f"nothing to do: all {len(rows):,} titles already tagged by {s.model}")
        return 0
    say(f"{len(todo):,} of {len(rows):,} titled meetings to tag"
        + (f", {cached:,} already stored for this model" if cached else ""))

    results, failed = [], 0
    for start in range(0, len(todo), BATCH):
        chunk = todo[start:start + BATCH]
        try:
            got = ask_batch([r["title"] for r in chunk], s)
        except llm.LLMError as e:
            failed += len(chunk)
            say(f"  [{start + len(chunk):>5}/{len(todo)}] batch failed: {e}")
            continue
        for i, r in enumerate(chunk):
            it = got.get(i)
            if not it:
                failed += 1
                continue
            tags = [x for x in (it.get("sectors") or []) if x in set(sectors.VOCAB)]
            # "Unclassified" is a ticker's missing SIC code, not a subject a
            # hearing can be about. Letting it through would match every trade
            # whose sector lookup failed, which is the opposite of the point.
            tags = [x for x in tags if x != "Unclassified"]
            results.append({
                "event_id": r["event_id"], "title": r["title"],
                "sectors": ",".join(sorted(set(tags))),
                "subject": (it.get("subject") or "")[:140],
                "confidence": it.get("confidence") or ""})
        if (start // BATCH) % 10 == 0 or start + BATCH >= len(todo):
            n_tagged = sum(1 for r in results if r["sectors"])
            say(f"  [{start + len(chunk):>5}/{len(todo)}] "
                f"{n_tagged:,} with a sector, {len(results) - n_tagged:,} without")

    report(results, failed, say)
    if dry_run:
        say("\ndry run: nothing written")
        return 0
    now = db.utcnow()
    with db.connect(cfg.db_path) as conn:
        conn.executemany(
            """INSERT INTO meeting_topics
                 (event_id, sectors, subject, confidence, model, updated_at)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(event_id) DO UPDATE SET
                 sectors=excluded.sectors, subject=excluded.subject,
                 confidence=excluded.confidence, model=excluded.model,
                 updated_at=excluded.updated_at""",
            [(r["event_id"], r["sectors"], r["subject"], r["confidence"], s.model,
              now) for r in results])
    say(f"\nstored {len(results):,} meeting topics")
    return 0


def report(results: list[dict], failed: int, say=print) -> None:
    counts: dict[str, int] = {}
    for r in results:
        for x in (r["sectors"].split(",") if r["sectors"] else []):
            counts[x] = counts.get(x, 0) + 1
    with_sector = sum(1 for r in results if r["sectors"])
    say(f"\n{len(results):,} titles tagged"
        + (f", {failed:,} unanswered" if failed else ""))
    say(f"  {with_sector:,} bear on some industry, "
        f"{len(results) - with_sector:,} on none")
    if results and with_sector / len(results) > 0.6:
        # The tagger is supposed to find most hearings industry-free. If it does
        # not, the sector-matched arm is no narrower than the arm it replaces.
        say("  WARNING: most meetings were given a sector. Check a sample before "
            "trusting the sector-matched arm -- a tagger that matches everything "
            "reproduces exactly the dilution it was built to remove.")
    for label, n in sorted(counts.items(), key=lambda kv: -kv[1])[:15]:
        say(f"  {n:>5}  {label}")
    for r in results[:8]:
        say(f"    {r['sectors'] or '—':<40} {r['title'][:70]}")


def coverage(cfg=CONFIG) -> dict:
    """What is available, for the timing report to state plainly."""
    with db.connect(cfg.db_path) as conn:
        total = conn.execute("SELECT count(*) FROM committee_meetings").fetchone()[0]
        titled = conn.execute(
            "SELECT count(*) FROM committee_meetings "
            "WHERE title IS NOT NULL AND title != ''").fetchone()[0]
        tagged = conn.execute("SELECT count(*) FROM meeting_topics").fetchone()[0]
        with_sector = conn.execute(
            "SELECT count(*) FROM meeting_topics WHERE sectors != ''").fetchone()[0]
        models = [r[0] for r in conn.execute(
            "SELECT DISTINCT model FROM meeting_topics")]
    return {"meetings": total, "titled": titled, "tagged": tagged,
            "with_sector": with_sector, "models": models}


def sectors_by_event(conn) -> dict[str, set[str]]:
    """event_id -> the sectors that meeting bears on. Empty when never tagged."""
    return {r["event_id"]: {x for x in r["sectors"].split(",") if x}
            for r in conn.execute(
                "SELECT event_id, sectors FROM meeting_topics WHERE sectors != ''")}


def run(cfg=CONFIG, stage: str = "both", since: str = "", limit: int = 0,
        refresh: bool = False, dry_run: bool = False, quiet: bool = False) -> int:
    if stage in ("fetch", "both"):
        rc = fetch(cfg, since, limit, quiet)
        if rc:
            return rc
    if stage in ("tag", "both"):
        return tag(cfg, limit, refresh, dry_run, quiet)
    return 0


def selftest(cfg=CONFIG):
    sc = schema()
    enum = sc["properties"]["items"]["items"]["properties"]["sectors"]["items"]["enum"]
    assert enum == list(sectors.VOCAB), "the schema enum drifted from sectors.VOCAB"
    # The tagger's vocabulary has to be the one trades are labelled with, or a
    # tagged hearing could never match a trade.
    with db.connect(cfg.db_path) as conn:
        used = {r[0] for r in conn.execute(
            "SELECT DISTINCT sector FROM ticker_sectors WHERE sector != ''")}
        assert used <= set(sectors.VOCAB), f"trades carry sectors the tagger cannot say: {used - set(sectors.VOCAB)}"
        by_event = sectors_by_event(conn)
    assert all("Unclassified" not in v for v in by_event.values()), \
        "Unclassified is a missing SIC code, not a hearing subject"
    c = coverage(cfg)
    assert c["tagged"] <= c["titled"] <= c["meetings"], c
    assert dt.date.today().isoformat()
    print(f"selftest ok: {len(sectors.VOCAB)} sectors, {c['meetings']:,} meetings, "
          f"{c['titled']:,} titled, {c['tagged']:,} tagged")


if __name__ == "__main__":
    selftest()
