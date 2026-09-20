"""Which industries a committee's jurisdiction plausibly touches, generated once.

`legislators.COMMITTEE_SECTORS` was seventeen hand-written rows matched as
substrings against whatever name a seat happened to carry, and its own docstring
said what it was: EDITORIAL, NOT OFFICIAL. Two things were wrong with it beyond
that admission.

It was matched against the wrong string. A seat's `name` is the full committee
name for a full committee ("House Committee on Agriculture") but a bare label for
a subcommittee ("Health", "Energy", "Defense") -- and those bare labels repeat:
three different committees have a subcommittee called Health, three more have one
called Energy. A substring test cannot tell them apart, and it matched some of
them to the wrong parent outright. Appropriations' "Homeland Security"
subcommittee picked up the homeland-security row, though the entire reason
Appropriations was left out is that its reach is too broad to flag.

And seventeen needles never covered the 221 committees and subcommittees members
actually sit on. Everything unlisted -- most House subcommittees, nearly every
Senate one -- silently contributed nothing, so a seat either matched a needle by
luck or carried no jurisdiction at all.

So the table is generated instead. A model reads every committee and
subcommittee name in member_committees, with its parent for context, and answers
from the same closed sector vocabulary `ticker_sectors` uses. The result is
reviewed once and committed to seed/committee_sectors.json as data.

Nothing here calls a model at analysis time. `sectors_for_seat` is a dict lookup
against the committed file; the generator runs only when asked:

    congress-trades jurisdiction              # show the committed table
    congress-trades jurisdiction --generate   # ask a model, diff, commit nothing
    congress-trades jurisdiction --write      # commit what --generate proposed

Reviewing the diff is the point, so --generate keeps its raw answer in the cache
and --write promotes it without calling a model again. Asking twice would cost
another pass and could return something other than what you just reviewed, which
would make the review meaningless.

The model does not get the last word. Three rules are applied after it, because
each is a judgement about this project rather than about committee jurisdiction:

  - The broad committees stay empty. Appropriations, Budget, Rules, Ethics,
    House Administration, Oversight and Foreign Affairs reach nearly every
    industry, so tagging them flags nearly every trade and drowns the signal.
    That was the original list's deliberate omission, and it survives here as a
    rule applied to the answer rather than as a request inside a prompt.
  - No committee keeps more than MAX_SECTORS. A seat tagged with nine industries
    is not jurisdiction, it is a wildcard.
  - "Unclassified" is never a jurisdiction. It marks a ticker whose SIC lookup
    failed, and letting it through would match every trade the lookup missed --
    the opposite of narrowing anything.

This is still editorial, and the committed file says so in its own header. It
exists to surface "traded in an industry their committee oversees" as a prompt to
go look, never as a finding.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import db, llm, sectors
from .config import CONFIG

DATA = Path(__file__).resolve().parent.parent / "seed" / "committee_sectors.json"


def _candidate(cfg) -> Path:
    """Where --generate parks its answer for --write to promote.

    In the cache rather than beside the committed file: it is an intermediate,
    it is not reviewed yet, and nothing that has not been read by a human should
    sit in seed/ looking like data.
    """
    return cfg.cache_dir / "committee_sectors.candidate.json"
BATCH = 15           # names are short, but each needs its parent for context
MAX_SECTORS = 5      # above this it is a wildcard, not a jurisdiction

# Committees whose jurisdiction is government-wide. Keyed on the 4-character
# thomas_id root, so every subcommittee of one is covered by its parent's entry:
# Appropriations' Defense subcommittee is still Appropriations.
#
# This is the original hand-written list's deliberate omission, kept as a rule.
# A model asked about "Defense" under Appropriations will answer Transportation
# Equipment, and it is not wrong about the subject -- it is wrong about what this
# project should do with it, which is not a question the model was asked.
BROAD: dict[str, str] = {
    "HSAP": "Appropriations", "SSAP": "Appropriations",
    "HSBU": "Budget", "SSBU": "Budget",
    "HSRU": "Rules", "SSRA": "Rules and Administration",
    "HSSO": "Ethics", "SLET": "Ethics",
    "HSHA": "House Administration",
    "HSGO": "Oversight and Government Reform",
    "HSFA": "Foreign Affairs", "SSFR": "Foreign Relations",
}

# Corrections made at review, applied as a rule so they survive regeneration.
#
# The review step is not decoration: both runs of this table produced a body
# tagged Agriculture whose own `why` was about something else. The confidence
# gate below catches most of that, but not all -- "Space and Aeronautics" came
# back Agriculture at HIGH confidence, justified as "NASA oversight and space
# policy", which does not mention agriculture at all. Agriculture is VOCAB's
# first value and so the value constrained decoding falls back to.
#
# Hand-editing the committed JSON would fix it until the next --generate wiped
# it out. Recording the correction here instead means a regeneration applies it
# again, and the reason stays next to the change. Each entry needs a reason, and
# an empty tuple is a legitimate correction.
OVERRIDES: dict[str, tuple[tuple[str, ...], str]] = {
    # Aerospace manufacturers are SIC 372/376, which roll up to Transportation
    # Equipment -- the same sector a trade in one of them would carry.
    "HSSY16": (("Transportation Equipment",),
               "Space and Aeronautics is aerospace, not agriculture"),
}

SYSTEM = """\
You read the names of US congressional committees and subcommittees and say \
which industries, if any, that body's jurisdiction covers.

Return for each input every industry from the allowed list whose companies are \
directly regulated, funded, or overseen by this body. Usually one to three. \
Never more than five: a body tagged with more industries than that matches \
everything and distinguishes nothing.

An EMPTY list is a correct and common answer. Return one for any body whose \
subject is government itself rather than an industry -- appropriations, budget, \
rules, ethics, internal administration, oversight of agencies, foreign policy, \
nominations, investigations -- and for any body whose subject is a population or \
a policy rather than a business: "Human Resources", "Social Security", \
"Elections", "Civil Rights". Do not reach for a loose connection. Almost every \
committee touches the economy somehow; that is not jurisdiction.

A subcommittee is given with its parent committee. Judge the subcommittee's own \
subject, not the parent's -- "Defense" under Appropriations is spending, not \
aerospace -- but use the parent to disambiguate a bare label: "Health" under \
Ways and Means is health-care financing, "Health" under Energy and Commerce is \
drugs and devices regulation.

Pick the sector whose companies are IN the business the body oversees, and \
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
Never answer "Unclassified" -- that marks a company whose industry is unknown, \
not a jurisdiction. If no sector fits, return an empty list.

Also return:
- why: the jurisdiction in plain words, at most 12 words.
- confidence: high only when the name states its subject outright.

Return one object per input, echoing its index i, for every index you were \
given.\
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
                "why": {"type": "string"},
                "confidence": {"type": "string",
                               "enum": ["high", "medium", "low"]},
            },
            "required": ["i", "sectors", "why", "confidence"],
            "additionalProperties": False}}},
        "required": ["items"], "additionalProperties": False}


# ------------------------------------------------------------------- the rules
def apply_rules(key: str, proposed: list[str],
                confidence: str = "") -> tuple[tuple[str, ...], str]:
    """(kept, note). Everything the model is not allowed to decide.

    Returned rather than applied silently so the review diff can show which
    answers were overruled and why -- a rule that fires on half the table is
    either doing the job or hiding a bad prompt, and you cannot tell which
    without seeing it.
    """
    root = (key or "")[:4]
    if key in OVERRIDES:
        fixed, why = OVERRIDES[key]
        if tuple(proposed) == fixed:
            return fixed, ""
        return fixed, f"corrected at review: {why}"
    if root in BROAD:
        if proposed:
            return (), f"overruled: {BROAD[root]} reaches every industry"
        return (), ""
    # A low-confidence tag is not a weak opinion, it is a specific failure, and
    # the first run showed exactly what it looks like. Constrained decoding has
    # to emit something from the enum, so a model that cannot map its own
    # reasoning onto the vocabulary emits the enum's first value -- which is
    # "Agriculture", because VOCAB is sorted. All four low-confidence tags in
    # that run came back Agriculture, on Armed Services and on Science, Space
    # and Technology, each with a `why` naming its real subject: "Defense
    # procurement and military equipment oversight". Nothing correct was marked
    # low. So low confidence is dropped rather than discounted.
    if (confidence or "").strip().lower() == "low" and proposed:
        return (), f"dropped {'/'.join(proposed)}: low confidence"
    kept = [s for s in dict.fromkeys(proposed)
            if s in set(sectors.VOCAB) and s != "Unclassified"]
    dropped = [s for s in dict.fromkeys(proposed) if s not in kept]
    note = f"dropped {'/'.join(dropped)}" if dropped else ""
    if len(kept) > MAX_SECTORS:
        note = (f"trimmed {len(kept)} sectors to {MAX_SECTORS}"
                + (f"; {note}" if note else ""))
        kept = kept[:MAX_SECTORS]
    return tuple(kept), note


# -------------------------------------------------------------------- loading
_cache: dict | None = None


def load(force: bool = False) -> dict:
    """The committed table, keyed by committee id. {} when it has not been generated.

    Sectors are filtered against the current vocabulary on the way in, so a
    rename in sectors.py cannot leave a tag behind that no trade can ever carry.
    """
    global _cache
    if _cache is not None and not force:
        return _cache
    try:
        raw = json.loads(DATA.read_text())
    except (OSError, ValueError):
        _cache = {}
        return _cache
    vocab = set(sectors.VOCAB) - {"Unclassified"}
    out = {}
    for key, rec in (raw.get("committees") or {}).items():
        secs = tuple(s for s in (rec.get("sectors") or []) if s in vocab)
        out[key] = {**rec, "sectors": secs[:MAX_SECTORS]}
    _cache = out
    return _cache


def sectors_for_seat(seat: dict) -> tuple[str, ...]:
    """The sectors one committee seat covers. A dict lookup; no model, no network.

    A key the table has never seen -- a subcommittee created after the table was
    generated -- falls back to its parent committee's entry rather than to
    nothing, because a subcommittee's jurisdiction is a subset of its parent's.
    That keeps a stale table conservative instead of silently blind, and
    `coverage()` reports how often it happens.
    """
    table = load()
    key = (seat or {}).get("key") or ""
    rec = table.get(key)
    if rec is not None:
        return tuple(rec["sectors"])
    root = key[:4]
    if root in BROAD:
        return ()
    parent = table.get(root)
    return tuple(parent["sectors"]) if parent else ()


def coverage(cfg=CONFIG) -> dict:
    """How much of the roster the committed table actually answers for."""
    table = load()
    try:
        with db.connect(cfg.db_path) as conn:
            seats = [dict(r) for r in conn.execute(
                "SELECT DISTINCT key, name, parent FROM member_committees")]
    except Exception:
        seats = []
    known = [s for s in seats if s["key"] in table]
    tagged = [s for s in known if table[s["key"]]["sectors"]]
    return {"table": len(table), "seats": len(seats), "known": len(known),
            "tagged": len(tagged), "missing": len(seats) - len(known),
            "generated": _meta().get("generated", ""),
            "model": _meta().get("model", "")}


def _meta() -> dict:
    try:
        raw = json.loads(DATA.read_text())
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in raw.items() if k != "committees"}


# ----------------------------------------------------------------- generating
def targets(cfg=CONFIG) -> list[dict]:
    """Every distinct committee and subcommittee a member actually sits on."""
    with db.connect(cfg.db_path) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT DISTINCT key, name, parent FROM member_committees "
            "ORDER BY parent, name")]
    return [r for r in rows if r.get("key") and r.get("name")]


def ask_batch(rows: list[dict], s: llm.Settings) -> dict[int, dict]:
    listing = "\n".join(
        f"{i}. {r['name']}" + (f"  (subcommittee of {r['parent']})"
                               if r.get("parent") else "  (full committee)")
        for i, r in enumerate(rows))
    prompt = (f"Give the industry jurisdiction of these {len(rows)} congressional "
              f"bodies. Return exactly {len(rows)} objects, one per index.\n\n"
              f"{listing}")
    got = llm.ask_json(prompt, SYSTEM, schema(), s=s)
    items = got.get("items") if isinstance(got, dict) else got
    if not isinstance(items, list):
        raise llm.LLMError(f"expected a list of items, got {type(items).__name__}")
    out: dict[int, dict] = {}
    for it in items:
        if isinstance(it, dict) and isinstance(it.get("i"), int) \
                and 0 <= it["i"] < len(rows) and it["i"] not in out:
            out[it["i"]] = it
    return out


def _payload(results: list[dict], model: str, raw: bool = False) -> dict:
    return {
        "_comment": ("EDITORIAL, NOT OFFICIAL. Real jurisdiction is defined by "
                     "chamber rules and is messier than this. Generated by a "
                     "model from committee names, reviewed once, and committed "
                     "as data -- see congress_trades/jurisdiction.py. It exists "
                     "to surface 'traded in an industry their committee "
                     "oversees' as a prompt to go look, never as a finding."),
        "generated": db.utcnow()[:10],
        "model": model,
        "vocabulary": list(sectors.VOCAB),
        "committees": {r["key"]: {"name": r["name"], "parent": r["parent"],
                                  "sectors": r["sectors"], "why": r["why"],
                                  "confidence": r["confidence"],
                                  **({"proposed": r.get("proposed", [])}
                                     if raw else {})}
                       for r in sorted(results, key=lambda r: r["key"])},
    }


def _commit(results: list[dict], model: str, say=print) -> None:
    DATA.parent.mkdir(parents=True, exist_ok=True)
    DATA.write_text(json.dumps(_payload(results, model), indent=1) + "\n")
    load(force=True)
    say(f"\nwrote {len(results):,} committees to {DATA}")


def generate(cfg=CONFIG, write: bool = False, limit: int = 0,
             quiet: bool = False) -> int:
    say = (lambda *a: None) if quiet else print
    rows = targets(cfg)
    if not rows:
        raise SystemExit(
            "member_committees is empty, so there is nothing to generate from. "
            "Run\n  congress-trades enrich\nfirst.")
    s = llm.settings()
    say(f"generating with {llm.preflight(s)}")
    if limit:
        rows = rows[:limit]
    say(f"{len(rows):,} committees and subcommittees to classify")

    results: list[dict] = []
    failed = 0
    for start in range(0, len(rows), BATCH):
        chunk = rows[start:start + BATCH]
        try:
            got = ask_batch(chunk, s)
        except llm.LLMError as e:
            failed += len(chunk)
            say(f"  [{start + len(chunk):>4}/{len(rows)}] batch failed: {e}")
            continue
        for i, r in enumerate(chunk):
            it = got.get(i)
            if not it:
                failed += 1
                continue
            kept, note = apply_rules(r["key"], list(it.get("sectors") or []),
                                     it.get("confidence") or "")
            results.append({
                "key": r["key"], "name": r["name"], "parent": r.get("parent") or "",
                "sectors": list(kept),
                "proposed": list(it.get("sectors") or []),
                "why": (it.get("why") or "")[:100],
                "confidence": it.get("confidence") or "", "note": note})
        say(f"  [{start + len(chunk):>4}/{len(rows)}] "
            f"{sum(1 for r in results if r['sectors']):,} with a jurisdiction")

    if not results:
        say("\nno committee was classified; nothing to review")
        return 1
    # A partial answer must never become the table: a batch that failed would
    # silently delete every committee in it, and a jurisdiction that vanishes
    # looks exactly like one a model decided against.
    if failed:
        say(f"\n{failed:,} of {len(rows):,} went unanswered. The table is "
            "all-or-nothing -- committing a partial run would drop those "
            "committees rather than leave them alone.")

    report(results, failed, say)
    diff(results, say)

    cand = _candidate(cfg)
    cand.parent.mkdir(parents=True, exist_ok=True)
    cand.write_text(json.dumps({**_payload(results, s.model, raw=True),
                                "unanswered": failed,
                                "asked": len(rows)}, indent=1) + "\n")
    if write:
        if failed:
            say("\nrefusing to commit a partial run. Re-run --generate, or "
                "promote it deliberately with --write once it is complete.")
            return 1
        _commit(results, s.model, say)
        return 0
    say(f"\nnothing committed. The proposal is in {cand}.")
    say("Review the diff above, then run `congress-trades jurisdiction --write` "
        "to commit it without asking again.")
    return 0


def promote(cfg=CONFIG, quiet: bool = False) -> int:
    """Commit what --generate already proposed. Calls no model."""
    say = (lambda *a: None) if quiet else print
    cand = _candidate(cfg)
    try:
        raw = json.loads(cand.read_text())
    except (OSError, ValueError):
        raise SystemExit(
            f"no proposal to commit ({cand} is missing or unreadable). Run\n"
            "  congress-trades jurisdiction --generate\nfirst.")
    # Re-apply the rules rather than trusting what the proposal was scored
    # against, for the same reason resolve keeps its raw proposals: the rules
    # are the part that keeps changing, and a rule added after a generate run
    # should take effect without paying for another one.
    results = []
    for k, v in (raw.get("committees") or {}).items():
        proposed = list(v.get("proposed", v.get("sectors") or []))
        kept, note = apply_rules(k, proposed, v.get("confidence", ""))
        results.append({"key": k, "name": v.get("name", ""),
                        "parent": v.get("parent", ""), "sectors": list(kept),
                        "proposed": proposed, "why": v.get("why", ""),
                        "confidence": v.get("confidence", ""), "note": note})
    overruled = [r for r in results if r["note"]]
    if overruled:
        say(f"{len(overruled)} proposal(s) overruled by the current rules:")
        for r in overruled[:15]:
            say(f"  {r['name'][:46]:<46} {r['note']}")
    if not results:
        raise SystemExit(f"{cand} holds no committees")
    if raw.get("unanswered"):
        say(f"WARNING: that run left {raw['unanswered']} committee(s) unanswered, "
            "so committing it drops them from the table.")
    diff(results, say)
    _commit(results, raw.get("model", ""), say)
    return 0


def report(results: list[dict], failed: int, say=print) -> None:
    counts: dict[str, int] = {}
    for r in results:
        for x in r["sectors"]:
            counts[x] = counts.get(x, 0) + 1
    tagged = sum(1 for r in results if r["sectors"])
    say(f"\n{len(results):,} bodies classified"
        + (f", {failed:,} unanswered" if failed else ""))
    say(f"  {tagged:,} carry a jurisdiction, {len(results) - tagged:,} carry none")
    overruled = [r for r in results if r["note"]]
    if overruled:
        say(f"  {len(overruled):,} answers overruled by the rules:")
        for r in overruled[:12]:
            say(f"    {r['name'][:44]:<44} {r['note']}")
    if results and tagged / len(results) > 0.75:
        # The same warning topics.py carries, for the same reason: a tagger that
        # finds jurisdiction everywhere reproduces the dilution it was meant to
        # remove, and committee overlap goes back to flagging every trade.
        say("  WARNING: most bodies were given a jurisdiction. Check a sample "
            "before committing this -- a table that matches everything makes "
            "committee overlap meaningless.")
    for label, n in sorted(counts.items(), key=lambda kv: -kv[1])[:15]:
        say(f"  {n:>4}  {label}")


def diff(results: list[dict], say=print) -> None:
    """What this run would change about the committed table. The review step."""
    old = load()
    if not old:
        say("\nno committed table yet: every row below is new.")
    changed, added = [], []
    for r in results:
        was = old.get(r["key"])
        if was is None:
            if r["sectors"]:
                added.append(r)
        elif tuple(was["sectors"]) != tuple(r["sectors"]):
            changed.append((r, was))
    gone = [k for k in old if k not in {r["key"] for r in results}]
    say(f"\ndiff against the committed table: {len(added)} new, "
        f"{len(changed)} changed, {len(gone)} no longer in the roster")
    for r in added[:25]:
        label = f"{r['parent']} / {r['name']}" if r["parent"] else r["name"]
        say(f"  + {label[:58]:<58} {'/'.join(r['sectors'])}")
    for r, was in changed[:25]:
        label = f"{r['parent']} / {r['name']}" if r["parent"] else r["name"]
        say(f"  ~ {label[:58]:<58} {'/'.join(was['sectors']) or '—'} "
            f"-> {'/'.join(r['sectors']) or '—'}")
    for k in gone[:10]:
        say(f"  - {old[k].get('name', k)}")


def to_markdown(cfg=CONFIG) -> str:
    """The committed table, for reading."""
    table, c = load(), coverage(cfg)
    if not table:
        return ("# Committee jurisdiction\n\nNo table has been generated yet. "
                "Run `congress-trades jurisdiction --generate`.\n")
    L = [f"# Committee jurisdiction — {c['tagged']} of {c['table']} bodies "
         "carry one", "",
         f"Generated {c['generated'] or 'at an unknown date'} by "
         f"{c['model'] or 'an unrecorded model'}, reviewed once, committed as "
         "data. EDITORIAL, NOT OFFICIAL: it prompts a look, it is not a finding.",
         ""]
    if c["missing"]:
        L += [f"{c['missing']} committee(s) on the current roster are not in the "
              "table and fall back to their parent's jurisdiction. Re-generate "
              "to include them.", ""]
    L += ["| committee | subcommittee | sectors |", "|---|---|---|"]
    for key in sorted(table, key=lambda k: (table[k].get("parent") or
                                            table[k].get("name", ""), k)):
        rec = table[key]
        if not rec["sectors"]:
            continue
        parent, name = rec.get("parent") or "", rec.get("name") or key
        L.append(f"| {parent or name} | {name if parent else '—'} "
                 f"| {', '.join(rec['sectors'])} |")
    empty = sum(1 for r in table.values() if not r["sectors"])
    L += ["", f"{empty} bodies carry no industry jurisdiction, which is the "
          "expected answer for appropriations, budget, rules, ethics, "
          "administration, oversight and foreign affairs."]
    return "\n".join(L) + "\n"


def run(cfg=CONFIG, generate_table: bool = False, write: bool = False,
        limit: int = 0, quiet: bool = False) -> int:
    if generate_table:
        return generate(cfg, write, limit, quiet)
    if write:
        return promote(cfg, quiet)
    import sys
    sys.stdout.write(to_markdown(cfg))
    return 0


def selftest(cfg=CONFIG):
    """The rules and the lookup, with no model and no network."""
    # The broad committees stay empty however confident the model was.
    kept, note = apply_rules("HSAP01", ["Agriculture", "Food & Beverage"])
    assert kept == () and "every industry" in note, (kept, note)
    assert apply_rules("HSAP", ["Banking & Finance"])[0] == ()
    assert apply_rules("SSFR12", ["Transportation Equipment"])[0] == ()
    # ...and a body outside that set keeps what it was given.
    kept, _ = apply_rules("HSAG14", ["Agriculture"])
    assert kept == ("Agriculture",), kept
    # Unclassified is a missing SIC code, not a jurisdiction.
    kept, note = apply_rules("HSIF03", ["Healthcare Services", "Unclassified"])
    assert kept == ("Healthcare Services",) and "Unclassified" in note, (kept, note)
    # A sector outside the vocabulary cannot survive, whatever the schema did.
    assert apply_rules("HSIF03", ["Interpretive Dance"])[0] == ()
    # Low confidence is the enum-collapse failure, not a weak opinion.
    kept, note = apply_rules("HSAS", ["Agriculture"], "low")
    assert kept == () and "low confidence" in note, (kept, note)
    assert apply_rules("HSAS", ["Transportation Equipment"], "medium")[0] == \
        ("Transportation Equipment",), "medium is kept"
    assert apply_rules("HSAS", ["Transportation Equipment"], "high")[0] == \
        ("Transportation Equipment",)
    # An empty answer with low confidence is just an empty answer.
    assert apply_rules("HSAS", [], "low") == ((), "")
    # A wildcard is trimmed rather than trusted.
    many = list(sectors.VOCAB)[:MAX_SECTORS + 3]
    kept, note = apply_rules("HSSY01", many)
    assert len(kept) == MAX_SECTORS and "trimmed" in note, (kept, note)
    # Duplicates collapse instead of eating the cap.
    assert apply_rules("HSAG14", ["Agriculture", "Agriculture"])[0] == ("Agriculture",)

    # A reviewed correction beats the model, and beats its confidence.
    for k, (fixed, why) in OVERRIDES.items():
        assert set(fixed) <= set(sectors.VOCAB), f"{k} overrides to a non-sector"
        assert why, f"{k} has no reason recorded"
        assert apply_rules(k, ["Agriculture"], "high")[0] == fixed, k
        assert k[:4] not in BROAD, f"{k} is overridden and also ruled broad"

    sc = schema()
    enum = sc["properties"]["items"]["items"]["properties"]["sectors"]["items"]["enum"]
    assert enum == list(sectors.VOCAB), "the schema enum drifted from sectors.VOCAB"
    assert json.dumps(sc)

    table = load()
    assert all(set(r["sectors"]) <= set(sectors.VOCAB) for r in table.values()), \
        "the committed table carries a sector no trade can ever have"
    assert all("Unclassified" not in r["sectors"] for r in table.values())
    assert all(len(r["sectors"]) <= MAX_SECTORS for r in table.values())
    assert all(not r["sectors"] for k, r in table.items() if k[:4] in BROAD), \
        "a broad committee was committed with a jurisdiction"
    # A seat the table has never seen falls back to its parent, not to nothing.
    if table:
        any_key = next((k for k, r in table.items()
                        if len(k) == 4 and r["sectors"]), "")
        if any_key:
            assert sectors_for_seat({"key": any_key + "99"}) == \
                tuple(table[any_key]["sectors"]), "parent fallback is broken"
    assert sectors_for_seat({"key": "ZZZZ99"}) == ()
    assert sectors_for_seat({}) == ()

    c = coverage(cfg)
    assert c["known"] <= c["seats"] or not c["seats"]
    print(f"selftest ok: {c['table']} committees committed, {c['tagged']} with a "
          f"jurisdiction, {c['missing']} seat(s) falling back to a parent")


if __name__ == "__main__":
    selftest()
