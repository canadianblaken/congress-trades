"""Hand the digest to any OpenAI-compatible chat endpoint and keep the reply.

One code path on purpose. "Works with any AI by API" in practice means the
OpenAI /chat/completions shape, which OpenAI, LiteLLM, Ollama, vLLM, OpenRouter,
Groq and Together all speak. Anthropic models reach this through LiteLLM rather
than a second client in here.

Configure entirely by environment, so nothing about your setup is committed:

    CONGRESS_LLM_BASE   default http://127.0.0.1:4000/v1   (any compatible host)
    CONGRESS_LLM_MODEL  required, e.g. reason / gpt-4o / claude-opus-5
    CONGRESS_LLM_KEY    bearer token, if the endpoint wants one

The system prompt is part of the deliverable, not decoration: this data has a
45-day disclosure lag, reports brackets instead of amounts, and carries no
market benchmark, so a model left to itself will happily turn convergence counts
into confident stock picks. It is told the limits and told to rank by evidence.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from . import digest
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


def ask(prompt: str, system: str = SYSTEM, timeout: int = 300) -> str:
    base = os.getenv("CONGRESS_LLM_BASE", "http://127.0.0.1:4000/v1").rstrip("/")
    model = os.getenv("CONGRESS_LLM_MODEL", "")
    key = os.getenv("CONGRESS_LLM_KEY", "")
    if not model:
        raise SystemExit("CONGRESS_LLM_MODEL is not set. Pick a model your endpoint "
                         "serves, e.g. CONGRESS_LLM_MODEL=reason for a LiteLLM route.")
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": prompt}],
        "temperature": 0.2,
    }).encode()
    req = urllib.request.Request(
        f"{base}/chat/completions", data=body,
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {key}"} if key else {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{base} answered {e.code}: {e.read()[:400].decode(errors='replace')}")
    except urllib.error.URLError as e:
        raise SystemExit(f"cannot reach {base}: {e.reason}")
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        raise SystemExit(f"unexpected response shape: {json.dumps(data)[:400]}")


def run(days=90, floor=15001, dry_run=False, prev: str = "", cfg=CONFIG) -> int:
    md = digest.to_markdown(digest.build(days, floor, cfg))
    prompt = md if not prev else f"{md}\n---\nPrevious digest for comparison:\n\n{prev}"
    if dry_run:
        print(prompt)
        return 0
    print(ask(prompt))
    return 0
