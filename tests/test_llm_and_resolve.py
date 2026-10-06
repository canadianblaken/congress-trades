#!/usr/bin/env python3
"""Checks for the model layer: what the .env loader does, what each provider
reads, and every gate a proposed ticker has to pass.

No model is called and no network is touched, so this runs anywhere.

  python3 tests/test_llm_and_resolve.py
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from congress_trades import assets, llm, prices, resolve
from congress_trades.config import load_env

# --------------------------------------------------------------- the .env file
with tempfile.TemporaryDirectory() as d:
    env = Path(d) / ".env"
    env.write_text(
        "# a comment\n"
        "\n"
        "CONGRESS_TEST_PLAIN=hello\n"
        "  CONGRESS_TEST_SPACED  =  spaced  \n"
        "export CONGRESS_TEST_EXPORTED=exported\n"
        'CONGRESS_TEST_QUOTED="has spaces"\n'
        "CONGRESS_TEST_SINGLE='single'\n"
        "CONGRESS_TEST_URL=http://host/v1#frag\n"
        "CONGRESS_TEST_EMPTY=\n"
        "not a valid line\n"
        "9INVALID=x\n"
        "CONGRESS_TEST_WINS=from-file\n")
    os.environ["CONGRESS_TEST_WINS"] = "from-shell"
    for k in ("PLAIN", "SPACED", "EXPORTED", "QUOTED", "SINGLE", "URL", "EMPTY"):
        os.environ.pop(f"CONGRESS_TEST_{k}", None)
    n = load_env(env)

    assert os.environ["CONGRESS_TEST_PLAIN"] == "hello"
    assert os.environ["CONGRESS_TEST_SPACED"] == "spaced", "key and value are trimmed"
    assert os.environ["CONGRESS_TEST_EXPORTED"] == "exported", "an export prefix is stripped"
    assert os.environ["CONGRESS_TEST_QUOTED"] == "has spaces", "one quote pair comes off"
    assert os.environ["CONGRESS_TEST_SINGLE"] == "single"
    # An unquoted value keeps a '#': tokens and URL fragments contain them, and
    # treating one as a comment would silently truncate a key.
    assert os.environ["CONGRESS_TEST_URL"] == "http://host/v1#frag"
    assert os.environ["CONGRESS_TEST_EMPTY"] == ""
    assert "9INVALID" not in os.environ, "a non-identifier is not a variable"
    # The whole point of the precedence: `FOO=x python -m ...` must still win,
    # or you could never override the file for a single run.
    assert os.environ["CONGRESS_TEST_WINS"] == "from-shell", "the file beat the shell"
    assert n == 7, f"expected 7 new variables, set {n}"

assert load_env(Path("/nonexistent/.env")) == 0, "a missing file is not an error"

# Importing the model layer ALONE must already have loaded the .env file. llm.py
# reads os.environ directly and imports config only for that side effect, so
# without it `from congress_trades import llm` fell back to the default provider
# and silently ignored the file the README calls the place to configure it.
# A subprocess, for a clean import graph -- that is the whole point of the check.
_probe = subprocess.run(
    [sys.executable, "-c", "import congress_trades.llm, sys; "
                           "print('congress_trades.config' in sys.modules)"],
    capture_output=True, text=True,
    cwd=str(Path(__file__).resolve().parent.parent))
assert _probe.stdout.strip() == "True", \
    f"importing llm did not load config, so .env is ignored: {_probe.stderr[-300:]}"

# ------------------------------------------------------------------ providers
_saved = {k: v for k, v in os.environ.items() if k.startswith("CONGRESS_")}


def _only(**kw):
    for k in list(os.environ):
        if k.startswith("CONGRESS_"):
            del os.environ[k]
    os.environ.update(kw)


_only(CONGRESS_LLM_PROVIDER="ollama", CONGRESS_OLLAMA_MODEL="qwen3.8:27b")
s = llm.settings()
assert (s.provider, s.model) == ("ollama", "qwen3.8:27b")
assert s.base == "http://127.0.0.1:11434", "the native port, not the /v1 shim"
assert s.num_ctx == 8192 and s.think is False, "thinking off and a real context by default"
assert "num_ctx" in s.describe()

_only(CONGRESS_LLM_PROVIDER="ollama", CONGRESS_OLLAMA_MODEL="m",
      CONGRESS_OLLAMA_NUM_CTX="32768", CONGRESS_OLLAMA_THINK="true",
      CONGRESS_OLLAMA_BASE="http://box:11434/")
s = llm.settings()
assert s.num_ctx == 32768 and s.think is True and s.base == "http://box:11434"

_only(CONGRESS_LLM_MODEL="gpt-4o")
s = llm.settings()
assert s.provider == "openai", "openai stays the default provider"
assert s.base == "http://127.0.0.1:4000/v1", "the LiteLLM default is unchanged"

_only(CONGRESS_LLM_PROVIDER="OLLAMA", CONGRESS_OLLAMA_MODEL="m")
assert llm.settings().provider == "ollama", "the provider name is case-insensitive"

for bad in ({"CONGRESS_LLM_PROVIDER": "anthropic", "CONGRESS_LLM_MODEL": "x"},
            {"CONGRESS_LLM_PROVIDER": "ollama"},
            {}):
    _only(**bad)
    try:
        llm.settings()
    except SystemExit as e:
        assert str(e), "an unusable configuration must say what to set"
    else:
        raise AssertionError(f"{bad} should not have been accepted")

_only(**_saved)

# ------------------------------------------------------------ dotted symbols
# Filings write class shares with a dot; the chart endpoint 404s on every one.
assert prices.yahoo_symbol("BRK.B") == "BRK-B"
assert prices.yahoo_symbol(" brk.b ") == "BRK-B"
assert prices.yahoo_symbol("BRK-B") == "BRK-B", "already-hyphenated is untouched"
assert prices.yahoo_symbol("AAPL") == "AAPL"

# --------------------------------------------------------- the ticker gates
TITLES = {"AMD": "ADVANCED MICRO DEVICES INC", "ADI": "ANALOG DEVICES INC",
          "BMO": "BANK OF MONTREAL", "AVGO": "Broadcom Inc."}
yes, no = (lambda t: True), (lambda t: False)

# Passes: the symbol is the filing text, or SEC's name for it matches that text.
assert resolve.verify("AMD", "AMD", "Listed equity", TITLES, yes)[0] == "AMD"
assert resolve.verify("Analog Devices", "ADI", "Listed equity", TITLES, yes)[0] == "ADI"
assert resolve.verify("Broadcom Inc. - Common Stock", "AVGO", "Listed equity",
                      TITLES, yes)[0] == "AVGO"
assert resolve.verify("amd", "amd", "Listed equity", TITLES, yes)[0] == "AMD", \
    "a lower-cased symbol still resolves"

# Refusals, one per gate.
for name, cand, label, fragment in [
    ("Bank of Montreal 13-Month Digital", "BMO", "Structured notes", "no equity symbol"),
    ("U.S Treasury Bills", "BMO", "Treasuries", "no equity symbol"),
    ("BIRMINGHAM ALA GO WTS SER. 2018 B", "BIRMINGHAM ALA GO", "Listed equity",
     "shaped like a symbol"),
    ("Nonesuch Holdings", "ZZZZ", "Listed equity", "registered symbol list"),
    ("ALPHAKEYS BLACKSTONE LIFE", "BMO", "Listed equity", "does not specifically match"),
    ("Analog Devices", "ADI", "Listed equity", "no price history"),
]:
    got, verdict = resolve.verify(name, cand, label, TITLES,
                                  no if "price history" in fragment else yes)
    assert got == "", f"{name!r} + {cand!r} should have been refused, got {got!r}"
    assert fragment in verdict, f"expected {fragment!r} in {verdict!r}"

# The model's own `issuer` field must never corroborate its own ticker: both come
# from the same place, so one invention would simply vouch for the other. Letting
# it in briefly accepted a Georgia state bond as GS ("Goldman Sachs" was in the
# issuer field) and GOLDMAN SACHS GROUP INC as GOOGL ("Alphabet Inc." likewise).
CIRC = {"GS": "GOLDMAN SACHS GROUP INC", "GOOGL": "Alphabet Inc."}
got, verdict = resolve.verify("GEORGIA ST SER Rate/Coupon: 4.0% Matures: 2034-07-01",
                              "GS", "Listed equity", CIRC, yes,
                              "Goldman Sachs Group Inc")
assert got == "" and "does not specifically match" in verdict, verdict
got, verdict = resolve.verify("GOLDMAN SACHS GROUP INC", "GOOGL", "Listed equity",
                              CIRC, yes, "Alphabet Inc.")
assert got == "" and "does not specifically match" in verdict, verdict
# ...and the genuine one still passes on the filing text alone, no issuer at all.
assert resolve.verify("GOLDMAN SACHS GROUP INC", "GS", "Listed equity", CIRC,
                      yes, "")[0] == "GS"

# Proposing nothing is the right answer for most rows, and is not a refusal.
assert resolve.verify("U.S Treasury Bills [GS]", "", "Treasuries", TITLES, yes) == ("", "")
# A blank label cannot smuggle a symbol through.
assert resolve.verify("AMD", "AMD", assets.UNLABELLED, TITLES, yes)[0] == ""

# ------------------------------------------------------------- the vocabulary
enum = resolve.schema()["properties"]["items"]["items"]["properties"]["label"]["enum"]
assert enum == list(assets.VOCAB), "the schema enum drifted from assets.VOCAB"
assert assets.UNLABELLED in enum, "the model needs a way to say it does not know"
assert set(resolve.TICKERABLE) <= set(assets.VOCAB)
assert json.dumps(resolve.schema()), "the schema must be JSON-serialisable"

print("ok: env precedence, two providers, dotted symbols, 11 refusal gates")
