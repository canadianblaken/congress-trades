"""What the parser could not resolve, put to a model -- and then verified.

`repair-tickers` recovers a symbol the filing spelled out in parentheses. What is
left after it are the names no pattern can reach, and they are two different piles
wearing the same label:

  - Instruments no regex will ever catch. "ATHERTON MICH CMNTY SCH" is municipal
    debt, "BANK AMERICA CORP SER N MTN" is corporate debt, "Bank of Montreal
    13-Month Digital" is a structured note. Reading those needs world knowledge,
    not a better pattern, and getting them wrong only costs a row in a mix table.
  - Listed equities whose filing never wrote the symbol at all. "Analog Devices",
    "AMD", "BRK-B - Berkshire Hathaway Inc Class B". These are ordinary stock
    trades currently excluded from every return, scorecard and backtest in the
    project, because an untickered row cannot be priced.

The second pile is the valuable one and the dangerous one. A wrong ticker on a
real disclosure is far worse than no ticker: it gets priced, scored, and attributed
to a member as a trade they never made. A model asked for a symbol will always
produce something -- asked about a Birmingham bond during development, one
cheerfully answered "BIRMINGHAM ALA GO WTS SER. 2018".

So nothing a model says about a ticker is trusted, only used as a candidate:

  1. the label must be an instrument that HAS a symbol;
  2. the string must look like a symbol;
  3. SEC's own company_tickers file must register it, and SEC's name for it must
     recognisably match the filing's text -- or the symbol must appear in that
     text verbatim, which is how a bare "AMD" passes;
  4. the price source must actually return a series for it.

Only a candidate through all four is written into congress_trades. Everything
else is kept in asset_labels with the reason it was refused, because a rejected
proposal is the most useful thing here for judging whether this pass is working.

Labels are held to a lower bar -- they are stored, never merged into the trades
table, and `mix` only consults them for rows the deterministic classifier gave up
on. Dropping the asset_labels table returns the project to parsed fact.

    congress-trades resolve --dry-run      # see the proposals, write nothing
    congress-trades resolve                # store labels; still no ticker writes
    congress-trades resolve --apply        # also write verified tickers to trades
"""
from __future__ import annotations

import json
import re

from . import assets, db, llm, prices, sectors
from .config import CONFIG

BATCH = 20          # names per call; small enough that one bad batch is cheap
# Labels that can carry a symbol at all. A structured note or a muni has an
# issuer that is often listed -- Bank of Montreal is a real ticker -- but the
# INSTRUMENT is not that equity, and pricing it as though it were would be a
# fabricated trade.
TICKERABLE = {"Listed equity", "Funds & trusts"}

SYSTEM = """\
You label financial instruments from US congressional STOCK Act filings. Each \
input is the asset text exactly as it appeared in the filing, which may be \
abbreviated, truncated, missing punctuation, or upper-cased by a PDF extractor.

Take the FIRST of these that fits:

1. Listed equity — the text is a bare exchange symbol ("AMD", "ASML", "ARCC"), \
or names a company with its share class ("Broadcom Inc. - Common Stock", "BRK-B \
- Berkshire Hathaway Inc Class B"), or is simply a company's name ("Analog \
Devices"). A company name with no instrument word attached means that company's \
shares. Do not label something as debt merely because it names a company: shares \
are the default for a company, and debt has to say so.
2. Government & municipal debt — a US state, county, city, school district, \
authority, board or agency, usually abbreviated: "CHASKA MN ELEC REVENUE", \
"AUSTIN TEX PUB IMPT REF BDS", "GO WTS", "Cmnty Sch", "Transprtn Brd", "Sales \
Tax Rev", "Pub Impt". These are very common in this data.
3. Corporate debt — a company's borrowing rather than its shares, which the text \
states: "MTN" (medium-term note), "Senior Notes", "Debenture", "Sub Notes", or a \
coupon and maturity beside a corporate issuer.
4. Structured notes — a bank product described as Digital, Autocallable, \
Buffered, Market-Linked, or linked to an index.
5. Otherwise the best fit from the allowed list: funds, ETFs, BDCs and interval \
funds are Funds & trusts; a private company described by its line of business is \
Private & pre-IPO equity; a named partnership or LP is Partnerships & private.

"Rate/Coupon:" or "Matures:" anywhere in the text means it is a bond — decide \
between 2 and 3 by who is borrowing, and never call it equity.

Also return:
- ticker: the exchange symbol, ONLY for case 1, and only when you are confident \
of the symbol. A bond ISSUED BY a listed company is still a bond, so return "" \
for it even though its issuer is listed. If the input is already a bare symbol, \
return that symbol unchanged. When in any doubt return "".
- issuer: the entity behind the instrument, in plain words ("City of Austin, \
Texas", "Bank of Montreal", "Advanced Micro Devices"). "" if unclear.
- confidence: high only when the text names the instrument unambiguously.

Never guess a symbol from an issuer's name unless that issuer's own shares are \
what is being traded. Returning "" costs nothing; a wrong symbol is recorded as \
a trade the member never made. Return one object per input, echoing its index i, \
for every index you were given.\
"""


def schema() -> dict:
    return {
        "type": "object",
        "properties": {"items": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "i": {"type": "integer"},
                "label": {"type": "string", "enum": list(assets.VOCAB)},
                "ticker": {"type": "string"},
                "issuer": {"type": "string"},
                "confidence": {"type": "string",
                               "enum": ["high", "medium", "low"]},
            },
            "required": ["i", "label", "ticker", "issuer", "confidence"],
            "additionalProperties": False}}},
        "required": ["items"], "additionalProperties": False}


# ------------------------------------------------------------------ verifying
# Corporate-form and boilerplate words carry no identifying power: "TRUST" is
# shared by half the filings, so matching on it would verify nothing. "CAPITAL"
# is deliberately NOT here -- it is half of "Capital One", and dropping it would
# refuse a real recovery.
_NOISE = {"INC", "INCORPORATED", "CORP", "CORPORATION", "COMPANY", "PLC", "LTD",
          "LIMITED", "LLC", "THE", "AND", "CLASS", "COMMON", "STOCK", "SHARES",
          "SHARE", "ORDINARY", "GROUP", "HOLDING", "HOLDINGS", "TRUST", "FUND",
          "FUNDS", "ETF", "NEW", "DEPOSITARY", "RECEIPT", "SER", "SERIES", "COM"}

# Shared on its own, these say almost nothing: thousands of companies are a
# "Financial Corporation" or a "Technologies Inc". One of them matching is not
# evidence of identity, so a match resting only on these needs a second word.
_GENERIC = {"FINANCIAL", "FINANCE", "SERVICES", "SERVICE", "SYSTEMS", "SOLUTIONS",
            "TECHNOLOGIES", "TECHNOLOGY", "GLOBAL", "PARTNERS", "INDUSTRIES",
            "ENTERPRISES", "RESOURCES", "PROPERTIES", "INTERNATIONAL", "NATIONAL",
            "AMERICAN", "AMERICA", "UNITED", "FIRST", "GENERAL"}

# A captive finance arm borrows under a name one word away from its listed
# parent's. "General Motors Financial Company" is not General Motors Co, and a
# disclosure naming it is that subsidiary's paper; matching it to GM on
# GENERAL/MOTORS would price a bond as the parent's stock -- a trade the member
# never made. This caught exactly that during development.
_SUBSIDIARY = {"FINANCIAL", "FINANCE", "FUNDING", "CREDIT", "CAPITAL", "LEASING",
               "ACCEPTANCE", "MORTGAGE", "RECEIVABLES"}


def _words(text: str) -> set[str]:
    """Every word, unfiltered. For tests about what a name CONTAINS."""
    return {t for t in re.split(r"[^A-Za-z0-9]+", (text or "").upper()) if t}


def _tokens(text: str) -> set[str]:
    """Identifying words only. For tests about whether two names MATCH."""
    return {t for t in _words(text) if len(t) >= 3 and t not in _NOISE
            and not t.isdigit()}


def _name_agrees(name: str, title: str) -> tuple[bool, str]:
    """Does SEC's name for the symbol match THE FILING TEXT?

    Only the filing text. The model's `issuer` field is deliberately not part of
    this, though it reads like corroboration: it comes from the same model as the
    symbol, so it corroborates nothing and merely lets one invention vouch for
    another. Allowing it briefly here accepted a Georgia state bond as GS because
    the model had also written "Goldman Sachs" in the issuer field, and accepted
    GOLDMAN SACHS GROUP INC as GOOGL because it had written "Alphabet Inc.".
    Evidence has to come from somewhere the model is not.

    Corroboration also has to be specific. A single generic word in common --
    "Financial", "Technologies" -- is satisfied by thousands of companies, so a
    match resting on one of those alone is asked for a second word.
    """
    theirs = _tokens(title)
    ours = _tokens(name)
    shared = {w for w in theirs & ours if len(w) >= 4}
    if not shared:
        return False, ""
    if shared - _GENERIC or len(shared) >= 2:
        return True, "/".join(sorted(shared))
    return False, ""


def verify(name: str, cand: str, label: str, titles: dict[str, str],
           has_series, issuer: str = "") -> tuple[str, str]:
    """(ticker, verdict). The ticker is '' unless every gate passed.

    Two routes in, and they rest on different evidence:

      The filing wrote the symbol itself ("AMD", "FIG (NYSE)"). That is the
      filing's own assertion, which outranks anything the model thinks -- it is
      the same evidence `repair_tickers` accepts. The model only noticed it. If
      the model's issuer then disagrees with SEC about what that symbol is, the
      symbol still stands and the disagreement is recorded, because a stale idea
      of who trades under FIG does not make the filing's "FIG" wrong.

      The model proposed a symbol that is nowhere in the filing. Here the model
      is the only source, so the bar is higher: SEC's registered name has to
      corroborate it specifically, and the subsidiary guard has to pass.

    `has_series` is injected rather than called directly so the caller can memoise
    it across a run and a test can supply one that touches no network.
    """
    t = (cand or "").strip().upper().replace("$", "")
    if not t:
        return "", ""
    if label not in TICKERABLE:
        return "", f"refused: proposed {t}, but a {label.lower()} has no equity symbol"
    if not re.fullmatch(r"[A-Z]{1,5}([.\-][A-Z]{1,2})?", t):
        return "", f"refused: {t!r} is not shaped like a symbol"
    title = titles.get(t) or titles.get(t.replace(".", "-"))
    if not title:
        return "", f"refused: {t} is not in SEC's registered symbol list"

    if t in _words(name):
        note = "symbol appears in the filing text"
        agrees, _ = _name_agrees(name, title)
        if issuer and not (_tokens(issuer) & _tokens(title)):
            # Worth seeing, not worth refusing: the filing named the symbol, and
            # that outranks the model's idea of whose symbol it is. The model was
            # wrong about ADCT, ET, FIG and KPTI while the symbols were right.
            note += (f", though the model called it {issuer!r} and SEC calls it "
                     f"{title!r}")
    else:
        agrees, shared = _name_agrees(name, title)
        if not agrees:
            return "", (f"refused: SEC lists {t} as {title!r}, which does not "
                        "specifically match the filing text")
        # The parent's symbol for the subsidiary's paper is the one wrong answer
        # that survives a name match, because the names genuinely are alike.
        stray = (_SUBSIDIARY & _words(name)) - _words(title)
        if stray:
            return "", (f"refused: the filing says {'/'.join(sorted(stray))} but "
                        f"SEC lists {t} as {title!r} — that reads as a finance "
                        "subsidiary of the listed company, not the company")
        note = f"matched SEC name {title!r} on {shared}"

    if not has_series(t):
        return "", f"refused: {t} has no price history, so it could not be scored"
    return t, f"accepted: {note}, price history present"


# ------------------------------------------------------------------ the pass
def targets(conn, scope: str = "unlabelled") -> list[str]:
    """Distinct asset names worth asking about, longest-tail first.

    Default scope is only what the deterministic classifier could not place. The
    wider scope exists to check the model against the patterns -- if it disagrees
    with a confident regex label, that is worth seeing before trusting it here.
    """
    rows = [r["asset_name"] for r in conn.execute(
        "SELECT DISTINCT asset_name FROM congress_trades "
        "WHERE ticker = '' AND asset_name != '' ORDER BY asset_name")]
    if scope == "all":
        return rows
    return [n for n in rows if assets.classify(n) == assets.UNLABELLED]


def ask_batch(names: list[str], s: llm.Settings) -> dict[int, dict]:
    """One call for up to BATCH names. Indices are echoed back and checked.

    A model that silently drops an input would otherwise leave that name looking
    like it was considered and found unlabellable, which is a different and much
    worse thing than not having been asked.
    """
    listing = "\n".join(f"{i}. {n}" for i, n in enumerate(names))
    prompt = (f"Label these {len(names)} assets. Return exactly {len(names)} "
              f"objects, one per index.\n\n{listing}")
    got = llm.ask_json(prompt, SYSTEM, schema(), s=s)
    items = got.get("items") if isinstance(got, dict) else got
    if not isinstance(items, list):
        raise llm.LLMError(f"expected a list of items, got {type(items).__name__}")
    out: dict[int, dict] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        i = it.get("i")
        if isinstance(i, int) and 0 <= i < len(names) and i not in out:
            out[i] = it
    return out


def run(cfg=CONFIG, scope: str = "unlabelled", limit: int = 0,
        dry_run: bool = False, apply_tickers: bool = False,
        refresh: bool = False, quiet: bool = False) -> int:
    say = (lambda *a: None) if quiet else print
    s = llm.settings()
    say(f"resolving with {llm.preflight(s)}")

    with db.connect(cfg.db_path) as conn:
        names = targets(conn, scope)
        done = {} if refresh else {
            r["asset_name"]: r["model"] for r in
            conn.execute("SELECT asset_name, model FROM asset_labels")}
        todo = [n for n in names if done.get(n) != s.model]
    if not todo:
        say(f"all {len(names):,} names are already resolved by {s.model}")
        if apply_tickers:
            # --apply after a completed run must still apply. Returning "nothing
            # to do" here would be technically true of the asking and quietly
            # wrong about the thing actually requested.
            say("applying what is already stored")
            return reverify(cfg, apply_tickers=True, dry_run=dry_run, quiet=quiet)
        return 0
    cached = len(names) - len(todo)
    if limit:
        todo = todo[:limit]

    titles = sectors.ticker_titles(cfg)
    _seen: dict[str, bool] = {}

    def has_series(t: str) -> bool:
        if t not in _seen:
            _seen[t] = bool(prices.series(t, cfg))
        return _seen[t]

    say(f"{len(todo):,} of {len(names):,} untickered names to resolve"
        + (f", {cached:,} already stored for this model" if cached else "")
        + (f" (stopping at --limit {limit})" if limit else ""))

    results: list[dict] = []
    failed = 0
    for start in range(0, len(todo), BATCH):
        chunk = todo[start:start + BATCH]
        try:
            got = ask_batch(chunk, s)
        except llm.LLMError as e:
            # One bad batch must not discard the batches already answered.
            failed += len(chunk)
            say(f"  [{start + len(chunk):>4}/{len(todo)}] batch failed: {e}")
            continue
        for i, name in enumerate(chunk):
            it = got.get(i)
            if not it:
                failed += 1
                continue
            label = it.get("label") if it.get("label") in assets.VOCAB \
                else assets.UNLABELLED
            issuer = (it.get("issuer") or "")[:120]
            ticker, verdict = verify(name, it.get("ticker", ""), label, titles,
                                     has_series, issuer)
            results.append({
                "asset_name": name, "label": label, "ticker": ticker,
                "proposed": (it.get("ticker") or "").strip().upper(),
                "issuer": issuer,
                "confidence": it.get("confidence") or "", "verdict": verdict})
        say(f"  [{start + len(chunk):>4}/{len(todo)}] "
            f"{sum(1 for r in results if r['ticker']):,} tickers verified so far")

    report(results, failed, say)
    if dry_run:
        say("\ndry run: nothing written")
        return 0

    now = db.utcnow()
    with db.connect(cfg.db_path) as conn:
        _store(conn, results, s.model, now)
        say(f"\nstored {len(results):,} labels")

        verified = [r for r in results if r["ticker"]]
        if not verified:
            return 0
        if not apply_tickers:
            say(f"{len(verified):,} verified tickers held back; "
                "re-run with --apply to write them into the trades table")
            return 0
        n = 0
        for r in verified:
            cur = conn.execute(
                "UPDATE congress_trades SET ticker = ? "
                "WHERE ticker = '' AND asset_name = ?",
                (r["ticker"], r["asset_name"]))
            n += cur.rowcount
        say(f"applied {len(verified):,} symbols to {n:,} disclosures — "
            "run `prices` to score them")
    return 0


def _store(conn, results: list[dict], model: str, now: str) -> None:
    conn.executemany(
        """INSERT INTO asset_labels
             (asset_name, label, proposed, ticker, issuer, confidence, verdict,
              model, updated_at)
           VALUES(?,?,?,?,?,?,?,?,?)
           ON CONFLICT(asset_name) DO UPDATE SET
             label=excluded.label, proposed=excluded.proposed,
             ticker=excluded.ticker, issuer=excluded.issuer,
             confidence=excluded.confidence, verdict=excluded.verdict,
             model=excluded.model, updated_at=excluded.updated_at""",
        [(r["asset_name"], r["label"], r.get("proposed", ""), r["ticker"],
          r["issuer"], r["confidence"], r["verdict"], model, now)
         for r in results])


def reverify(cfg=CONFIG, apply_tickers: bool = False, dry_run: bool = False,
             quiet: bool = False) -> int:
    """Re-run the gates over what the model already said. No model is called.

    The gates are the part of this that keeps changing -- each real run has
    turned up another way for a plausible symbol to be wrong -- and re-asking a
    model to re-test a rule it has no part in would be slow, non-deterministic,
    and would conflate a gate change with a different answer. So the raw proposal
    is kept and the verification is replayable against it.
    """
    say = (lambda *a: None) if quiet else print
    with db.connect(cfg.db_path) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT asset_name, label, proposed, ticker, issuer, confidence, model "
            "FROM asset_labels")]
    if not rows:
        say("no stored labels to re-verify; run `resolve` first")
        return 0
    if not any(r["proposed"] for r in rows):
        say(f"none of the {len(rows):,} stored rows kept the model's raw proposal "
            "(they predate that column), so there is nothing to re-check. "
            "Re-run `resolve --refresh` once to populate it.")
        return 0

    titles = sectors.ticker_titles(cfg)
    _seen: dict[str, bool] = {}

    def has_series(t: str) -> bool:
        if t not in _seen:
            _seen[t] = bool(prices.series(t, cfg))
        return _seen[t]

    results, changed = [], []
    for r in rows:
        ticker, verdict = verify(r["asset_name"], r["proposed"], r["label"],
                                 titles, has_series, r["issuer"])
        if ticker != r["ticker"]:
            changed.append((r["asset_name"], r["ticker"], ticker, verdict))
        results.append({**r, "ticker": ticker, "verdict": verdict})

    say(f"re-verified {len(results):,} stored proposals against the current gates")
    if changed:
        say(f"\n{len(changed)} verdict(s) changed:")
        for name, was, now_t in ((c[0], c[1], c[2]) for c in changed):
            say(f"  {was or '—':<8} -> {now_t or '—':<8} {name[:58]}")
    else:
        say("no verdict changed")
    report(results, 0, say)
    if dry_run:
        say("\ndry run: nothing written")
        return 0
    now = db.utcnow()
    with db.connect(cfg.db_path) as conn:
        for r in results:
            conn.execute("UPDATE asset_labels SET ticker = ?, verdict = ?, "
                         "updated_at = ? WHERE asset_name = ?",
                         (r["ticker"], r["verdict"], now, r["asset_name"]))
        verified = [r for r in results if r["ticker"]]
        if not apply_tickers:
            say(f"\n{len(verified):,} verified tickers held back; "
                "re-run with --apply to write them into the trades table")
            return 0
        n = 0
        for r in verified:
            n += conn.execute(
                "UPDATE congress_trades SET ticker = ? "
                "WHERE ticker = '' AND asset_name = ?",
                (r["ticker"], r["asset_name"])).rowcount
        say(f"\napplied {len(verified):,} symbols to {n:,} disclosures — "
            "run `prices` to score them")
    return 0


def report(results: list[dict], failed: int, say=print) -> None:
    by_label: dict[str, int] = {}
    for r in results:
        by_label[r["label"]] = by_label.get(r["label"], 0) + 1
    say(f"\n{len(results):,} names labelled"
        + (f", {failed:,} unanswered" if failed else ""))
    for label, n in sorted(by_label.items(), key=lambda kv: -kv[1]):
        say(f"  {n:>4}  {label}")

    ok = [r for r in results if r["ticker"]]
    refused = [r for r in results if r["verdict"].startswith("refused")]
    say(f"\ntickers: {len(ok):,} verified, {len(refused):,} proposed and refused")
    for r in ok[:20]:
        say(f"  + {r['ticker']:<7} {r['asset_name'][:58]:<58} {r['verdict']}")
    for r in refused[:12]:
        say(f"  - {r['asset_name'][:58]:<58} {r['verdict']}")


def selftest(cfg=CONFIG):
    """Every gate, with no model and no network."""
    titles = {"AMD": "ADVANCED MICRO DEVICES INC", "ADI": "ANALOG DEVICES INC",
              "ACN": "Accenture plc", "BRK-B": "BERKSHIRE HATHAWAY INC",
              "BMO": "BANK OF MONTREAL", "ARCC": "ARES CAPITAL CORP"}
    yes = lambda t: True
    no = lambda t: False

    t, v = verify("AMD", "AMD", "Listed equity", titles, yes)
    assert t == "AMD" and "appears in the filing" in v, v
    t, v = verify("Analog Devices", "ADI", "Listed equity", titles, yes)
    assert t == "ADI" and "ANALOG DEVICES" in v, v
    t, v = verify("ACN - Accenture plc Class A Ordinary Shares (Ireland)", "ACN",
                  "Listed equity", titles, yes)
    assert t == "ACN", v
    t, v = verify("BRK-B - Berkshire Hathaway Inc Class B", "BRK-B",
                  "Listed equity", titles, yes)
    assert t == "BRK-B", v

    # The failure this module exists to prevent: a bond, an issuer's symbol.
    t, v = verify("Bank of Montreal 13-Month Digital", "BMO", "Structured notes",
                  titles, yes)
    assert t == "" and "no equity symbol" in v, v
    # ...and the same symbol refused even when the label would allow one, because
    # nothing in the filing text matches SEC's name for it.
    t, v = verify("ALPHAKEYS BLACKSTONE LIFE", "BMO", "Listed equity", titles, yes)
    assert t == "" and "does not specifically match" in v, v
    # A captive finance arm carries its listed parent's name, which is the one
    # wrong answer a name match cannot catch on its own.
    t, v = verify("General Motors Financial Company", "GM", "Listed equity",
                  {"GM": "General Motors Co"}, yes, "General Motors Financial Company")
    assert t == "" and "finance subsidiary" in v, v
    # ...while the parent itself, named the same way on both sides, still passes.
    t, v = verify("Capital One Financial Corporation", "COF", "Listed equity",
                  {"COF": "CAPITAL ONE FINANCIAL CORP"}, yes,
                  "Capital One Financial Corporation")
    assert t == "COF", v
    # One generic word in common is not identification; two is.
    t, v = verify("Sunrise Financial Corporation", "COF", "Listed equity",
                  {"COF": "CAPITAL ONE FINANCIAL CORP"}, yes, "Sunrise Financial")
    assert t == "" and "does not specifically match" in v, v
    # The filing's own symbol outranks the model: a stale idea of who trades
    # under FIG does not make the filing's "FIG" wrong, but it is recorded.
    t, v = verify("FIG (NYSE) [OT]", "FIG", "Listed equity", {"FIG": "Figma, Inc."},
                  yes, "Fortress Investment Group LLC")
    assert t == "FIG" and "though the model called it" in v, v

    # The model's issuer must never corroborate the model's ticker.
    circ = {"GS": "GOLDMAN SACHS GROUP INC", "GOOGL": "Alphabet Inc."}
    t, v = verify("GEORGIA ST SER Rate/Coupon: 4.0%", "GS", "Listed equity", circ,
                  yes, "Goldman Sachs Group Inc")
    assert t == "" and "does not specifically match" in v, v
    t, v = verify("GOLDMAN SACHS GROUP INC", "GOOGL", "Listed equity", circ, yes,
                  "Alphabet Inc.")
    assert t == "" and "does not specifically match" in v, v
    assert verify("GOLDMAN SACHS GROUP INC", "GS", "Listed equity", circ, yes,
                  "")[0] == "GS", "the genuine match must survive on filing text alone"

    # Free text that is not a symbol at all.
    t, v = verify("BIRMINGHAM ALA GO WTS SER. 2018 B", "BIRMINGHAM ALA GO WTS",
                  "Listed equity", titles, yes)
    assert t == "" and "shaped like a symbol" in v, v
    # Shaped like one, but unregistered.
    t, v = verify("Nonesuch Holdings", "ZZZZ", "Listed equity", titles, yes)
    assert t == "" and "SEC's registered symbol list" in v, v
    # Registered and matching, but unpriceable: cannot be scored, so not accepted.
    t, v = verify("Analog Devices", "ADI", "Listed equity", titles, no)
    assert t == "" and "no price history" in v, v
    # An empty proposal is the expected answer for most rows, and is not a refusal.
    t, v = verify("U.S Treasury Bills [GS]", "", "Treasuries", titles, yes)
    assert (t, v) == ("", ""), (t, v)

    assert set(TICKERABLE) <= set(assets.VOCAB)
    sc = schema()
    enum = sc["properties"]["items"]["items"]["properties"]["label"]["enum"]
    assert enum == list(assets.VOCAB), "the schema enum drifted from the vocabulary"
    assert json.dumps(sc)

    with db.connect(cfg.db_path) as conn:
        n_all = len(targets(conn, "all"))
        n_un = len(targets(conn, "unlabelled"))
    assert n_un <= n_all
    print(f"selftest ok: 4 gates, {len(assets.VOCAB)} labels, "
          f"{n_un:,} unlabelled of {n_all:,} untickered names")


if __name__ == "__main__":
    selftest()
