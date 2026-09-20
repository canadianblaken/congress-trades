"""Does the House parser actually read the filings it has already collected?

Every number in this project rests on `parse_house_ptr` reading a PDF that
pdftotext mangled first, and the mangling is severe: headers collapse to bare
letters, "Filing Status: New" comes out as "F      S      : New", and a page
break drops a repeated column header into the middle of a transaction. Commit
19d6694 is what that costs when it goes unnoticed -- 4,506 equity trades parsed
as untickered, excluded from every return, scorecard and backtest, and the
project confidently describing them as "municipal bonds, notes, funds -- wealth
preservation" until someone looked.

Nothing would have caught that but reading the filings. So this reads them, in
two passes that fail differently and are worth keeping apart:

  scan   No model. For each cached text, count the transaction headers the
         parser's own pattern finds and compare that to the rows it returned.
         The pattern is how the parser locates a transaction, so a text where
         it matches more often than the parser produced rows is a text the
         parser is dropping from. This is exact, it costs nothing, and it runs
         over all of them.

  audit  A model reads the raw text beside the rows the parser produced and
         answers one question: which transactions in this document are not in
         that list? This is the half that can see what the pattern cannot --
         a transaction written in a layout the pattern never matches at all is
         invisible to `scan` by construction, because `scan` counts with the
         same pattern the parser uses. It is sampled rather than exhaustive:
         it is slow, and a regression net does not need every filing to catch a
         template change.

A model asked "what is missing" will always find something missing, so nothing
it claims is believed on its own. Each claimed row is checked back against the
document -- the date it cites must be in the text, the asset it names must be in
the text, and it must not already be among the parsed rows -- and only claims
that survive are reported. A refuted claim is kept in the count, because a pass
that suddenly claims forty misses and verifies none has told you something about
the model rather than about the parser.

This is deliberately NOT a pipeline stage. It reads the cache and writes only
its own findings table; it never edits a trade, and no collection run depends on
it. It is a thing you run periodically and read:

    congress-trades parser-qa --scan            # the exact pass, all filings
    congress-trades parser-qa --sample 25       # ...and ask a model about 25
    congress-trades parser-qa --doc 20030803    # one filing, in detail
"""
from __future__ import annotations

import json
import random
import re

from . import collect, db, llm
from .config import CONFIG

# Enough of the document to be worth asking about. Most House PTRs are small --
# the median cached text is about 2KB -- but a few hundred-transaction filings
# run past 100KB, and a prompt that overflows the context returns a confident
# answer about whichever half survived. Those are skipped by the model pass and
# reported as skipped; `scan` still covers them exactly, which is the half that
# matters most on a filing with 473 transactions in it.
CHARS_PER_TOKEN = 3.5
RESERVE_TOKENS = 1200          # the parsed rows, the instructions, and the reply

SYSTEM = """\
You read one US House periodic transaction report (a STOCK Act filing) that has \
been converted from PDF to text, and a list of the transactions a parser \
extracted from it. You answer one question: which transactions in the document \
are NOT in the parser's list?

The text is mangled, and that is expected. A PDF-to-text converter has collapsed \
headings to single letters, split rows across lines, and repeated the column \
header in the middle of the page where the PDF broke. "F      S      : New" is \
"Filing Status: New". Ignore all of it. You are looking only for transaction \
rows.

A transaction row has an asset, a type letter (P for purchase, S for sale), a \
transaction date, a notification date, and a dollar amount bracket. It usually \
looks like:

    SP    Apple Inc. Common Stock (AAPL) [ST]    P    04/10/2025 05/15/2025   $1,001 - $15,000

Report a transaction ONLY when it is in the document and NOT in the parser's \
list. Match on the transaction date together with the asset: the parser's asset \
text may be abbreviated or cut short, and a row whose date and asset both \
correspond to one in the list is the SAME row, not a missing one, however \
differently it is written.

For each genuinely missing transaction give:
- tx_date: exactly as the document writes it, MM/DD/YYYY.
- asset: the asset text as the document writes it, copied not paraphrased.
- tx_type: P or S, as the document writes it.
- amount: the dollar bracket as written, or "" if you cannot see one.

Every field must be copied from the document. A claim whose date or asset is not \
in the document will be discarded, so copy, never reconstruct.

An empty list is the expected answer for most filings, and is a good answer. Do \
not report a row because it is formatted oddly, because it is a bond or a fund, \
or because the parser wrote its name differently. Report it only when the \
transaction is genuinely absent from the list.\
"""


def schema() -> dict:
    return {
        "type": "object",
        "properties": {"missing": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "tx_date": {"type": "string"},
                "asset": {"type": "string"},
                "tx_type": {"type": "string", "enum": ["P", "S"]},
                "amount": {"type": "string"},
            },
            "required": ["tx_date", "asset", "tx_type", "amount"],
            "additionalProperties": False}}},
        "required": ["missing"], "additionalProperties": False}


# ------------------------------------------------------------------- the cache
def cached_docs(cfg=CONFIG) -> list[str]:
    """Doc ids of every House filing text already on disk. Fetches nothing."""
    return sorted(p.stem for p in cfg.cache_dir.glob("*.txt") if p.stem.isdigit())


def text_for(doc_id: str, cfg=CONFIG) -> str:
    p = cfg.cache_dir / f"{doc_id}.txt"
    try:
        return p.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


# ------------------------------------------------------------------ pass one
def scan_one(text: str) -> dict:
    """Headers the pattern finds, against rows the parser returned.

    `parse_house_ptr` splits the text on blank lines and takes ONE transaction
    per block, so a block holding two -- which is what a page break in the
    middle of a transaction produces -- yields one row and silently drops the
    other. Counting the pattern over the whole text instead of per block is what
    makes that visible.
    """
    rows = collect.parse_house_ptr(text)
    found = collect._H_TYPE.findall(text)
    return {"n_parsed": len(rows), "n_pattern": len(found),
            "unaccounted": max(0, len(found) - len(rows)), "rows": rows}


def scan(cfg=CONFIG, quiet: bool = False) -> dict:
    """The exact pass over every cached filing. No model, no network."""
    say = (lambda *a: None) if quiet else print
    docs = cached_docs(cfg)
    if not docs:
        raise SystemExit(
            f"no cached filing texts in {cfg.cache_dir}. Run\n"
            "  congress-trades backfill --chamber house\nfirst.")
    gaps, total = [], 0
    for doc in docs:
        r = scan_one(text_for(doc, cfg))
        if r["unaccounted"]:
            gaps.append({"doc_id": doc, **{k: r[k] for k in
                                           ("n_parsed", "n_pattern", "unaccounted")}})
            total += r["unaccounted"]
    gaps.sort(key=lambda g: -g["unaccounted"])
    say(f"scanned {len(docs):,} cached House filings with no model")
    if not gaps:
        say("  every transaction the pattern finds is a row the parser returned")
    else:
        say(f"  {len(gaps):,} filing(s) hold {total:,} transaction header(s) the "
            "parser did not turn into rows")
        for g in gaps[:15]:
            say(f"    {g['doc_id']}  {g['n_pattern']:>4} found, "
                f"{g['n_parsed']:>4} parsed, {g['unaccounted']:>3} dropped")
        if len(gaps) > 15:
            say(f"    ... and {len(gaps) - 15} more")
    return {"docs": len(docs), "gaps": gaps, "unaccounted": total}


# ------------------------------------------------------------------ pass two
def _budget(s: llm.Settings) -> int:
    """How much document this model can actually be shown, in characters."""
    ctx = s.num_ctx if s.provider == "ollama" else 8192
    return int(max(2000, (ctx - RESERVE_TOKENS)) * CHARS_PER_TOKEN)


def _words(text: str) -> set[str]:
    return {t for t in re.split(r"[^A-Za-z0-9]+", (text or "").upper()) if t}


def verify(claim: dict, text: str, rows: list[dict]) -> tuple[bool, str]:
    """(is a real miss, why). The model proposes; the document decides.

    Three gates, and the third is the one that matters most in practice: a model
    handed a long list will re-report rows that are already in it, and counting
    those as parser failures would turn a working parser into an alarming one.
    """
    date = (claim.get("tx_date") or "").strip()
    asset = (claim.get("asset") or "").strip()
    if not re.fullmatch(r"\d{2}/\d{2}/\d{4}", date):
        return False, f"refused: {date!r} is not a date this filing could carry"
    if date not in text:
        return False, f"refused: {date} does not appear in the document"
    # An identifying word from the asset has to be in the document too, or the
    # claim is a date that happens to be present with a name that is not.
    ident = {w for w in _words(asset) if len(w) >= 4 and not w.isdigit()}
    if not ident:
        return False, f"refused: {asset!r} carries no identifying word"
    have = _words(text)
    shared = ident & have
    if not shared:
        return False, (f"refused: none of {'/'.join(sorted(ident))} appears in "
                       "the document")
    # Already parsed? Same date plus any shared identifying word is the same row.
    for r in rows:
        if r.get("tx_date") != date:
            continue
        if _words(r.get("asset_name", "")) & ident:
            return False, (f"refused: already parsed as "
                           f"{r.get('asset_name', '')[:40]!r}")
    return True, f"corroborated: {date} and {'/'.join(sorted(shared))} in the text"


def ask_one(text: str, rows: list[dict], s: llm.Settings) -> list[dict]:
    listing = "\n".join(
        f"{i + 1}. {r['tx_date']}  {r['tx_type'][:1].upper()}  "
        f"{r['asset_name'][:70]}  {r['amount_range']}"
        for i, r in enumerate(rows)) or "(the parser returned no rows at all)"
    prompt = (f"DOCUMENT\n========\n{text}\n\n"
              f"THE PARSER EXTRACTED {len(rows)} TRANSACTION(S)\n"
              f"==============================================\n{listing}\n\n"
              "Which transactions in the document are not in that list?")
    got = llm.ask_json(prompt, SYSTEM, schema(), s=s)
    items = got.get("missing") if isinstance(got, dict) else got
    return [x for x in items if isinstance(x, dict)] if isinstance(items, list) else []


def audit(cfg=CONFIG, sample: int = 25, doc: str = "", seed: int = 0,
          refresh: bool = False, dry_run: bool = False,
          quiet: bool = False) -> int:
    """Ask a model about a sample of cached filings, and verify what it says."""
    say = (lambda *a: None) if quiet else print
    docs = [doc] if doc else cached_docs(cfg)
    if not docs:
        raise SystemExit(f"no cached filing texts in {cfg.cache_dir}")
    if doc and not text_for(doc, cfg):
        raise SystemExit(f"no cached text for filing {doc} in {cfg.cache_dir}")
    s = llm.settings()
    say(f"auditing with {llm.preflight(s)}")
    budget = _budget(s)

    if not doc:
        with db.connect(cfg.db_path) as conn:
            done = set() if refresh else {
                r[0] for r in conn.execute(
                    "SELECT doc_id FROM parser_audits WHERE model = ?", (s.model,))}
        pool = [d for d in docs if d not in done]
        # Deterministic, so a reported finding can be reproduced exactly. A
        # sample nobody can re-draw is a story, not a test.
        random.Random(seed).shuffle(pool)
        skipped_done = len(docs) - len(pool)
        docs = pool[:sample] if sample else pool
    else:
        skipped_done = 0

    results, oversize, failed = [], 0, 0
    for i, d in enumerate(docs, 1):
        text = text_for(d, cfg)
        if not text:
            continue
        sc = scan_one(text)
        if len(text) > budget:
            oversize += 1
            continue
        try:
            claims = ask_one(text, sc["rows"], s)
        except llm.LLMError as e:
            failed += 1
            say(f"  [{i:>4}/{len(docs)}] {d}: {e}")
            continue
        verified = []
        for c in claims:
            ok, why = verify(c, text, sc["rows"])
            if ok:
                verified.append({**c, "why": why})
        results.append({
            "doc_id": d, "n_parsed": sc["n_parsed"], "n_pattern": sc["n_pattern"],
            "n_claimed": len(claims), "n_verified": len(verified),
            "missing": verified})
        if verified:
            say(f"  [{i:>4}/{len(docs)}] {d}: {len(verified)} verified miss(es) "
                f"of {len(claims)} claimed")
        elif i % 10 == 0 or i == len(docs):
            say(f"  [{i:>4}/{len(docs)}] {sum(1 for r in results if r['n_verified']):,} "
                "filing(s) with a verified miss so far")

    report(results, oversize, failed, skipped_done, say)
    if dry_run:
        say("\ndry run: nothing written")
        return 0
    now = db.utcnow()
    with db.connect(cfg.db_path) as conn:
        conn.executemany(
            """INSERT INTO parser_audits
                 (doc_id, n_parsed, n_pattern, n_claimed, n_verified, detail,
                  model, updated_at)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(doc_id) DO UPDATE SET
                 n_parsed=excluded.n_parsed, n_pattern=excluded.n_pattern,
                 n_claimed=excluded.n_claimed, n_verified=excluded.n_verified,
                 detail=excluded.detail, model=excluded.model,
                 updated_at=excluded.updated_at""",
            [(r["doc_id"], r["n_parsed"], r["n_pattern"], r["n_claimed"],
              r["n_verified"], json.dumps(r["missing"]), s.model, now)
             for r in results])
    say(f"\nstored {len(results):,} audit(s)")
    return 0


def report(results: list[dict], oversize: int, failed: int, skipped_done: int,
           say=print) -> None:
    claimed = sum(r["n_claimed"] for r in results)
    verified = sum(r["n_verified"] for r in results)
    bad = [r for r in results if r["n_verified"]]
    say(f"\n{len(results):,} filing(s) audited"
        + (f", {skipped_done:,} already audited by this model" if skipped_done else "")
        + (f", {oversize:,} too large for this model's context" if oversize else "")
        + (f", {failed:,} failed" if failed else ""))
    say(f"  {claimed:,} row(s) claimed missing, {verified:,} corroborated by the "
        f"document, {claimed - verified:,} refuted")
    if claimed and not verified:
        # Worth saying plainly. It means the parser looks clean on this sample
        # AND that the model is generating claims the document does not support,
        # which is a fact about the audit rather than about the parser.
        say("  Every claim was refuted: on this sample the parser dropped "
            "nothing the model could point to in the text.")
    for r in bad[:15]:
        say(f"\n  {r['doc_id']}  parser returned {r['n_parsed']}, "
            f"{r['n_verified']} verified miss(es):")
        for m in r["missing"][:6]:
            say(f"    {m['tx_date']}  {m['tx_type']}  {m['asset'][:58]}  "
                f"{m.get('amount', '')}")
    if not bad and results:
        say("  No filing in this sample lost a transaction the model could find.")


def summary(cfg=CONFIG) -> dict:
    """What has been audited so far, for the portal and for `--status`."""
    try:
        with db.connect(cfg.db_path) as conn:
            row = conn.execute(
                "SELECT count(*), coalesce(sum(n_verified), 0), "
                "coalesce(sum(n_claimed), 0) FROM parser_audits").fetchone()
            models = [r[0] for r in conn.execute(
                "SELECT DISTINCT model FROM parser_audits WHERE model != ''")]
    except Exception:
        return {"audited": 0, "verified": 0, "claimed": 0, "cached": 0,
                "models": []}
    return {"audited": row[0], "verified": row[1], "claimed": row[2],
            "cached": len(cached_docs(cfg)), "models": models}


def run(cfg=CONFIG, scan_only: bool = False, sample: int = 25, doc: str = "",
        seed: int = 0, refresh: bool = False, dry_run: bool = False,
        quiet: bool = False) -> int:
    # The exact pass always runs: it is free, it covers everything, and it is
    # the half that found the real dropped rows.
    if not doc:
        scan(cfg, quiet)
    if scan_only:
        if not quiet:
            print(stored_to_text(cfg), end="")
        return 0
    return audit(cfg, sample, doc, seed, refresh, dry_run, quiet)


def stored_to_text(cfg=CONFIG) -> str:
    """What previous model audits found, so the free pass still shows the history.

    `scan` and the model audit answer different questions, and a reader looking
    at one wants to know whether the other has ever run. Without this the exact
    pass looks like the whole story, which is exactly the mistake that let a
    parser drop 4,506 rows.
    """
    s = summary(cfg)
    if not s["audited"]:
        return ("\nNo filing has been audited by a model yet. The scan above "
                "counts with the parser's own pattern, so it cannot see a "
                "transaction written in a layout that pattern never matches.\n"
                "  congress-trades parser-qa --sample 25\n")
    L = [f"\nModel audits so far: {s['audited']:,} of {s['cached']:,} cached "
         f"filings, by {', '.join(s['models']) or 'an unrecorded model'}.",
         f"  {s['claimed']:,} row(s) claimed missing, {s['verified']:,} "
         "corroborated by the document."]
    with db.connect(cfg.db_path) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT doc_id, n_parsed, detail FROM parser_audits "
            "WHERE n_verified > 0 ORDER BY n_verified DESC LIMIT 10")]
    for r in rows:
        try:
            miss = json.loads(r["detail"]) or []
        except ValueError:
            miss = []
        L.append(f"  {r['doc_id']}  parser returned {r['n_parsed']}, missed "
                 f"{len(miss)}:")
        for m in miss[:4]:
            L.append(f"    {m.get('tx_date', '')}  {m.get('tx_type', '')}  "
                     f"{(m.get('asset') or '')[:58]}")
    if not rows:
        L.append("  No audited filing has lost a transaction a model could find.")
    return "\n".join(L) + "\n"


def selftest(cfg=CONFIG):
    """The gates and the scan, with no model and no network."""
    # A block holding two transactions because a page break dropped a column
    # header into it. This is the real shape, from filing 20030803.
    two = """
           BITMINE IMMERSIN TECH INC            S          07/28/2025 07/28/2025   $50,001 -
           (BMNR) [ST]                                                             $100,000
           F       S    : New
ID   Owner Asset                                Transaction Date      Notification Amount
           BITMINE IMMERSIN TECH INC            P          07/16/2025 07/16/2025   $1,001 - $15,000
           (BMNR) [ST]
"""
    sc = scan_one(two)
    assert sc["n_pattern"] == 2, sc
    # Both come back now. Before the parser was fixed this returned one row and
    # the scan reported the other as unaccounted, which is how it was found.
    assert sc["n_parsed"] == 2, sc
    assert sc["unaccounted"] == 0, sc
    assert [r["tx_type"] for r in sc["rows"]] == ["sell", "buy"], sc["rows"]
    assert [r["amount_min"] for r in sc["rows"]] == [50001, 1001], sc["rows"]

    # The gates are checked against a parser that DID miss a row -- what a
    # regression looks like: the scan found two, the parser returned one.
    rows = sc["rows"][:1]
    # The genuine miss is corroborated: its date and its name are both in the text.
    ok, why = verify({"tx_date": "07/16/2025", "asset": "BITMINE IMMERSIN TECH INC",
                      "tx_type": "P", "amount": "$1,001 - $15,000"}, two, rows)
    assert ok and "corroborated" in why, why
    # The row the parser DID return is not a miss, however it is spelled.
    ok, why = verify({"tx_date": "07/28/2025", "asset": "Bitmine Immersion Tech",
                      "tx_type": "S", "amount": ""}, two, rows)
    assert not ok and "already parsed" in why, why
    # A date the document does not carry.
    ok, why = verify({"tx_date": "01/02/2020", "asset": "BITMINE IMMERSIN",
                      "tx_type": "P", "amount": ""}, two, rows)
    assert not ok and "does not appear" in why, why
    # A real date with a company the document never names -- the invention this
    # gate exists for.
    ok, why = verify({"tx_date": "07/16/2025", "asset": "Nonesuch Holdings",
                      "tx_type": "P", "amount": ""}, two, rows)
    assert not ok and "appears in the document" in why, why
    # Not a date at all.
    assert not verify({"tx_date": "sometime", "asset": "BITMINE", "tx_type": "P",
                       "amount": ""}, two, rows)[0]
    # An asset with nothing identifying in it.
    assert not verify({"tx_date": "07/16/2025", "asset": "a - b", "tx_type": "P",
                       "amount": ""}, two, rows)[0]

    # A clean filing must not look like a broken one.
    one = """
           SP          UnitedHealth Group Incorporated            P                 04/10/2025 05/15/2025             $1,001 - $15,000
                       Common Stock (UNH) [ST]
"""
    assert scan_one(one)["unaccounted"] == 0, scan_one(one)

    # Type E is an exchange, and the model pass is how it was found: the scan
    # counts with the parser's own pattern, so it was blind to it by
    # construction. Filing 20035106 held two transactions and returned one.
    ex = scan_one("""
           JT          King Cnty Wash 4.00% 12/01/32 [GS] E                07/26/2026 08/01/2026             $15,001 -
                                                                                                             $50,000
""")
    assert ex["n_parsed"] == 1 and ex["unaccounted"] == 0, ex
    assert ex["rows"][0]["tx_type"] == "exchange", ex["rows"][0]

    sc_schema = schema()
    assert sc_schema["properties"]["missing"]["items"]["properties"]["tx_type"][
        "enum"] == ["P", "S"]
    assert json.dumps(sc_schema)
    assert _budget(llm.Settings("ollama", "m", "b", num_ctx=8192)) > 20000

    s = summary(cfg)
    assert s["verified"] <= s["claimed"]
    print(f"selftest ok: both rows in a split block, exchange read, 6 "
          f"verification gates, {s['cached']:,} cached filings, "
          f"{s['audited']:,} audited")


if __name__ == "__main__":
    selftest()
