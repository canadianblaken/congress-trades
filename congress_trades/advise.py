"""Hand the digest to a model and keep the reply.

The transport moved to llm.py when a second provider arrived; what stays here is
the part that is actually about this data. The system prompt is the deliverable,
not decoration: these filings lag the trade by 45 days, report brackets instead of
amounts, and carry no market benchmark, so a model left to itself will happily
turn convergence counts into confident stock picks. It is told the limits and told
to rank by evidence.

`--check` audits the reply against the digest it was built from. The system
prompt above shapes generation and nothing reads the result, which leaves the
one failure this project actually worries about unmeasured: a brief that reads
exactly like the others while citing a ticker, a number or a ranking the data
never supported. The audit is a second pass with one job -- list what the digest
does not support -- and it catches a different class than the prompt does,
because a prompt can only ask and a check can look.

Two halves, and the deterministic one comes first:

  - Tickers and numbers are checked by string, not by a model. A symbol in the
    brief that appears nowhere in the digest, or a percentage the digest never
    printed, is found by comparing text, which is cheap, certain, and cannot
    itself hallucinate.
  - Everything else needs judgement -- a ranking presented as predictive when
    persistence is near zero, a causal claim from a correlation, a caveat
    dropped -- so a model is asked, and then held to the same bar resolve holds
    a proposed ticker to: every finding must quote the draft, and a finding
    whose quote is not in the draft is discarded. A model asked to find fault
    will find some, and an invented quotation is how an audit starts
    manufacturing the very thing it exists to catch.

Nothing here edits the brief. It reports, and the reader decides.

Pick a provider with CONGRESS_LLM_PROVIDER (openai | ollama); llm.py documents
the variables each one reads.
"""
from __future__ import annotations

import re

from . import digest, llm
from .config import CONFIG

SYSTEM = """\
You analyse US congressional stock-disclosure data. You are given a digest built \
from official STOCK Act filings.

Hard constraints on what this data can support:
- Filings lag the trade by up to 45 days. Returns in the digest are measured from \
the DISCLOSURE date, which is the earliest a reader could have acted.
- Amounts are the brackets members report, not position sizes. Never treat a \
bracket as a real dollar amount.
- Returns are alpha over SPY across the identical window, in the direction the \
member took, so +0% means they matched the index. Alpha is still not proof of \
skill: the windows overlap, a few prolific filers dominate the set, and a median \
across trades is not a portfolio return.
- Many disclosures are spouse-directed or index funds, and the filer may not have \
chosen the trade at all.
- You have no data after the digest's date, and no prices beyond what is shown.
- The digest reports a persistence figure: the rank correlation between members' first-half and second-half alpha. When it is near zero, a member's past record does NOT predict their next trade, and you must not present the ranking as a list of people to follow. Say what it is: history.
- A member whose scored trades concentrate in one ticker has one bet, not a record, however large their alpha. Check the top-name share before citing anyone.
- `congress-trades backtest` is the out-of-sample test. If its intervals span zero, no strategy has been shown to beat the index, and any recommendation you make must carry that.

Write for a reader deciding whether any of this is actionable:
1. What changed since the previous digest, if one is given.
2. The strongest signals, ranked, each with the specific evidence from the digest \
and the reason it might be noise.
3. What is NOT supported by the data, including anything a casual reader would \
wrongly infer from it.
4. If, and only if, the evidence supports it, concrete watch candidates — always \
with the disconfirming evidence next to each.

Never invent a ticker, price, date or member that is not in the digest. Prefer \
"the data does not say" over a confident guess. This is disclosure analysis, not \
investment advice, and you should not pretend otherwise.\
"""


CHECK_SYSTEM = """\
You audit a draft analysis against the source data it was written from. You are \
given the SOURCE (a digest of congressional stock-disclosure filings) and the \
DRAFT written from it.

List only claims the SOURCE does not support. You are not reviewing the writing, \
the structure, or the judgement calls. You are answering one question per \
sentence: is this in the source, or does it follow from what is in the source?

Before anything else, three things are NOT findings, and the first audit run \
produced six of them:

1. Hedged alternatives. The draft is required to put disconfirming evidence \
beside every candidate it raises, so "this could be rebalancing", "it may be a \
trust-directed trade", "possibly tax-loss harvesting" are the draft doing its \
job. Speculation offered AS an alternative or a caveat is never a finding. Flag \
it only when the draft asserts the motive as established: "this was tax-loss \
harvesting" is a finding, "this could be tax-loss harvesting" is not.
2. Ordinary knowledge about what a company or instrument IS. Calling BWXT a \
defence contractor or grouping tickers as industrials is general knowledge, not \
a claim about the filings. Only a claim about what THIS DATA shows can be \
unsupported by it.
3. Reading the source's own tables. Naming two members the source lists, or \
adding up rows it prints, is use of the source, not invention.

Report:
- invented: a ticker, member, company name, date or instrument description that \
does not appear in the source. This kind is for NAMED THINGS only -- a symbol \
the source never lists, a fund name attached to a symbol that is not that fund, \
a member who does not appear. An interpretation is never "invented"; if a claim \
overreaches, it is "overstated".
- number: a figure that is not in the source and does not follow arithmetically \
from figures that are.
- prediction: the member ranking, or any part of it, presented as a guide to \
what will happen next. The source reports a persistence figure -- the rank \
correlation between a member's first-half and second-half alpha. When it is near \
zero, the ranking is history, and calling anyone worth following, worth \
watching, or a strong performer going forward is unsupported however good their \
past number is.
- causal: cause claimed from what is only co-occurrence. Committee overlap is \
proximity, not evidence of acting on information.
- overstated: a real figure stripped of the limit the source attaches to it -- \
alpha quoted as skill, a bracket quoted as a position size, a concentrated \
record quoted without its top-name share, a return quoted without its benchmark.
- missing-caveat: a recommendation the source requires a disconfirming note \
beside, given without one.

For each finding, `quote` must be copied EXACTLY from the draft, word for word, \
long enough to locate but no longer than one sentence. A finding whose quote is \
not in the draft verbatim will be discarded, so copy, never paraphrase.

`why` says what the source actually shows, in one sentence.

An empty list is a valid and good answer, and on a careful draft it is the \
expected one. Do not manufacture findings to seem useful, and do not flag a \
claim merely because the draft states it more plainly than the source does. If \
the draft says the data does not support something, or offers a reason its own \
signal might be noise, that is the draft being correct, not a finding.\
"""

KINDS = ("invented", "number", "prediction", "causal", "overstated",
         "missing-caveat")


def check_schema() -> dict:
    return {
        "type": "object",
        "properties": {"findings": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": list(KINDS)},
                "quote": {"type": "string"},
                "why": {"type": "string"},
            },
            "required": ["kind", "quote", "why"],
            "additionalProperties": False}}},
        "required": ["findings"], "additionalProperties": False}


# ------------------------------------------------------- the deterministic half
# Upper-case words that are not symbols. Without these every brief trips on its
# own prose: "US", "SPY" and "ETF" are in nearly all of them, and a false
# positive in an audit is worse than in most places -- it teaches the reader to
# skim the findings, which is the one thing the pass must not do.
_NOT_TICKERS = {
    "A", "I", "AI", "AN", "AND", "ALL", "ARE", "AS", "AT", "BE", "BUT", "BY",
    "CEO", "CFO", "DC", "DO", "ETF", "ETFS", "FOR", "GDP", "GO", "IF", "IN",
    "IPO", "IRA", "IS", "IT", "ITS", "LLC", "NO", "NOR", "NOT", "OF", "ON",
    "OR", "PTR", "Q1", "Q2", "Q3", "Q4", "SEC", "SIC", "SO", "THE", "TO", "UK",
    "UP", "US", "USA", "VS", "WHO", "WHY", "YES", "ETC", "FAQ", "NB", "OK",
    "ROI", "SPY", "TL", "DR", "EPS", "PE", "YTD", "YOY", "QOQ", "MTD", "ATH",
}
# A symbol, as these filings and SEC's own list write them: BRK.B, BF-B, AAPL.
_TICKERISH = re.compile(r"\b[A-Z]{1,5}(?:[.\-][A-Z]{1,2})?\b")
_PCT = re.compile(r"[+-]?\d+(?:\.\d+)?\s?%")
_MONEY = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?\s?[kKmMbB]?\b")


def _norm_num(tok: str) -> str:
    """One spelling for a figure, so +6.1% and 6.1 % compare equal."""
    t = tok.replace(",", "").replace(" ", "").lstrip("+").lower()
    if t.endswith("%"):
        t = t[:-1]
    t = t.lstrip("$")
    # Trailing zeros after a decimal point are decoration, not precision.
    if "." in t:
        head, _, tail = t.partition(".")
        suffix = ""
        while tail and tail[-1].isalpha():
            suffix = tail[-1] + suffix
            tail = tail[:-1]
        tail = tail.rstrip("0")
        t = head + ("." + tail if tail else "") + suffix
    return t


def stray_tickers(draft: str, source: str) -> list[str]:
    """Symbols in the draft that appear nowhere in the digest.

    A string comparison rather than a judgement: the digest is the only place a
    ticker could legitimately have come from, so a symbol that is not in it was
    supplied by the model.
    """
    have = set(_TICKERISH.findall(source))
    seen: list[str] = []
    for t in _TICKERISH.findall(draft):
        if t in have or t in _NOT_TICKERS or t in seen:
            continue
        # A single letter inside prose is never a symbol worth reporting.
        if len(t) < 2:
            continue
        seen.append(t)
    return seen


def stray_numbers(draft: str, source: str) -> list[str]:
    """Percentages and dollar figures in the draft that the digest never printed.

    Deliberately reported as candidates rather than errors. A brief may
    legitimately add two rows together, and this cannot tell that from an
    invention -- but it can tell you which figures to look up, which is the part
    that is tedious to do by hand and easy to skip.
    """
    have = {_norm_num(x) for x in _PCT.findall(source) + _MONEY.findall(source)}
    seen: list[str] = []
    for tok in _PCT.findall(draft) + _MONEY.findall(draft):
        n = _norm_num(tok)
        if n in have or tok in seen:
            continue
        # 0% and 0 are in every digest's caveats in words if not in figures.
        if n in ("0", "0.0"):
            continue
        seen.append(tok)
    return seen


def _appears(quote: str, draft: str) -> bool:
    """Is this quote really in the draft? Whitespace and dashes are forgiven."""
    def flat(x: str) -> str:
        x = re.sub(r"[\u2010-\u2015\u2212]", "-", x)
        x = re.sub(r"[\u2018\u2019]", "'", x)
        x = re.sub(r"[\u201c\u201d]", '"', x)
        return re.sub(r"\s+", " ", x).strip().lower()
    q = flat(quote)
    return len(q) >= 8 and q in flat(draft)


def audit(source: str, draft: str, s: llm.Settings | None = None,
          timeout: int = 300) -> dict:
    """Everything the digest does not support, deterministic checks first.

    The model half is allowed to fail: a brief with its tickers and figures
    checked is worth more than no audit at all, and an endpoint that fell over
    is not a reason to throw the deterministic findings away.
    """
    out = {
        "tickers": stray_tickers(draft, source),
        "numbers": stray_numbers(draft, source),
        "findings": [], "discarded": 0, "error": "",
    }
    prompt = (f"SOURCE\n======\n{source}\n\nDRAFT\n=====\n{draft}\n\n"
              "List the claims in the DRAFT that the SOURCE does not support. "
              "Quote the draft exactly for each one.")
    try:
        got = llm.ask_json(prompt, CHECK_SYSTEM, check_schema(), timeout, s)
    except llm.LLMError as e:
        out["error"] = str(e)
        return out
    items = got.get("findings") if isinstance(got, dict) else got
    if not isinstance(items, list):
        out["error"] = f"expected a list of findings, got {type(items).__name__}"
        return out
    for it in items:
        if not isinstance(it, dict):
            continue
        quote = (it.get("quote") or "").strip()
        # The gate. A finding that cannot point at the draft is not a finding,
        # and letting one through would put an invented sentence in front of a
        # reader under the heading of a check.
        if not _appears(quote, draft):
            out["discarded"] += 1
            continue
        out["findings"].append({
            "kind": it.get("kind") if it.get("kind") in KINDS else "overstated",
            "quote": quote[:300],
            "why": (it.get("why") or "")[:300]})
    return out


def check_to_text(a: dict) -> str:
    """The audit, for reading under the brief it audits."""
    L = ["", "=" * 72, "CHECK — what the digest does not support", "=" * 72, ""]
    if a["tickers"]:
        L += [f"Symbols not in the digest ({len(a['tickers'])}):",
              "  " + ", ".join(a["tickers"]),
              "  Each was supplied by the model, not by the data.", ""]
    else:
        L += ["Symbols: every ticker in the brief appears in the digest.", ""]
    if a["numbers"]:
        L += [f"Figures not printed in the digest ({len(a['numbers'])}):",
              "  " + ", ".join(a["numbers"]),
              "  Some will be arithmetic on digest rows. Check rather than "
              "assume.", ""]
    else:
        L += ["Figures: every percentage and dollar figure is in the digest.", ""]
    if a["error"]:
        L += [f"The judgement pass did not run: {a['error']}",
              "The string checks above still stand.", ""]
        return "\n".join(L)
    if not a["findings"]:
        L += ["No unsupported claims found.", ""]
    else:
        L += [f"Unsupported claims ({len(a['findings'])}):", ""]
        for f in a["findings"]:
            L += [f"  [{f['kind']}] \u201c{f['quote']}\u201d", f"      {f['why']}", ""]
    if a["discarded"]:
        L += [f"{a['discarded']} finding(s) discarded: the quoted text was not "
              "in the brief.", ""]
    return "\n".join(L)


def ask(prompt: str, system: str = SYSTEM, timeout: int = 300) -> str:
    """One prompt, one reply. A failure here is fatal by design: this command is
    a single call, and a half-written brief is worse than none."""
    try:
        return llm.ask(prompt, system, timeout)
    except llm.LLMError as e:
        raise SystemExit(str(e))


def run(days=90, floor=15001, dry_run=False, prev: str = "", cfg=CONFIG,
        check: bool = False) -> int:
    md = digest.to_markdown(digest.build(days, floor, cfg))
    prompt = md if not prev else f"{md}\n---\nPrevious digest for comparison:\n\n{prev}"
    if dry_run:
        print(prompt)
        return 0
    # Resolve and check the target before building nothing twice: an unreachable
    # endpoint or an unpulled model should say so immediately.
    llm.preflight()
    brief = ask(prompt)
    print(brief)
    if not check:
        return 0
    # The audit reads the digest, never the previous one bolted onto it: a claim
    # is supported by this data or it is not, and giving the checker the old
    # digest too would let last quarter's numbers excuse this quarter's.
    print(check_to_text(audit(md, brief)))
    return 0


def selftest():
    """The gates, with no model and no network."""
    src = ("| AAPL | 4 | +2 | $1.2M | Software & IT Services | +6.1% |\n"
           "r = -0.06 across 91 members")
    # A symbol the digest never mentions is the model's, not the data's.
    assert stray_tickers("We like AAPL and NVDA.", src) == ["NVDA"], \
        stray_tickers("We like AAPL and NVDA.", src)
    # Prose must not read as symbols, or every brief trips on itself.
    assert stray_tickers("US ETF vs SPY: AI and the SEC say IT is OK.", src) == []
    # Case matters: lower-case words are never symbols.
    assert stray_tickers("we like apple and it is up", src) == []
    # A repeated invention is reported once.
    assert stray_tickers("NVDA, then NVDA again", src) == ["NVDA"]

    # Figures are compared after normalisation, so a sign or a comma is not news.
    assert stray_numbers("up 6.1% on $1.2M", src) == []
    assert stray_numbers("up +6.10% on $1,200,000", src) == ["$1,200,000"], \
        stray_numbers("up +6.10% on $1,200,000", src)
    assert stray_numbers("returned 41%", src) == ["41%"]
    assert stray_numbers("r = -0.06", src) == []      # not a percentage or a sum

    # The gate that stops an audit inventing what it is meant to catch.
    draft = "Members bought AAPL heavily this quarter, and it is a strong signal."
    assert _appears("bought AAPL heavily", draft)
    assert _appears("bought   AAPL\n heavily", draft), "whitespace must be forgiven"
    assert not _appears("bought TSLA heavily", draft), "a fabricated quote must fail"
    assert not _appears("AAPL", draft), "a quote too short to locate is not a quote"

    sc = check_schema()
    assert sc["properties"]["findings"]["items"]["properties"]["kind"]["enum"] \
        == list(KINDS)
    a = audit.__doc__ and True
    assert a
    print(f"selftest ok: {len(KINDS)} finding kinds, ticker/number/quote gates")
