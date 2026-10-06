"""One way to reach a model, whichever one you happen to run.

Two providers, because they are two genuinely different things:

  openai   any /chat/completions endpoint -- OpenAI, LiteLLM, vLLM, OpenRouter,
           Groq, Together, and Ollama's own compatibility shim.
  ollama   Ollama's native /api/chat.
  anthropic  Claude through the official `anthropic` SDK, imported only when this
           provider is chosen, so everyone else keeps the one-dependency install.
           Not Anthropic's OpenAI-compatible shim: structured output and refusal
           handling are native-only.

Ollama already answers the OpenAI shape on :11434/v1, so a second client needs a
reason to exist. Three:

  - Context. The shim serves whatever num_ctx the model's Modelfile sets, often
    4096, and silently discards the overflow. The digest is ~90 lines and the
    classification batches are longer; a truncated prompt yields a confident
    answer about whichever half survived, which is the worst failure on offer
    because nothing about the reply looks wrong.
  - Schema. Native `format` takes a JSON schema and constrains decoding to it. The
    classification passes need a label from a closed vocabulary, and an enum in
    the schema is a guarantee where a sentence in the prompt is a request.
  - Thinking. Local models increasingly think by default, which for a labelling
    pass buys nothing and costs a multiple of the tokens. `think: false` is
    native-only.

Configured entirely by environment -- from your shell, or from the .env that
config.load_env() now reads on every entry point rather than only under run.sh:

    CONGRESS_LLM_PROVIDER       openai | ollama | anthropic   (default: openai)

    # provider = openai
    CONGRESS_LLM_BASE           default http://127.0.0.1:4000/v1
    CONGRESS_LLM_MODEL          required
    CONGRESS_LLM_KEY            bearer token, if the endpoint wants one

    # provider = anthropic  (pip install anthropic)
    CONGRESS_LLM_MODEL          e.g. claude-opus-5-5
    CONGRESS_LLM_KEY            or ANTHROPIC_API_KEY -- or neither, after
                                `ant auth login`, which the SDK reads itself

    # provider = ollama
    CONGRESS_OLLAMA_BASE        default http://127.0.0.1:11434
    CONGRESS_OLLAMA_MODEL       required
    CONGRESS_OLLAMA_NUM_CTX     default 8192
    CONGRESS_OLLAMA_THINK       default 0
    CONGRESS_OLLAMA_KEEP_ALIVE  default 5m

Read at call time, not at import, so a test or a one-off export takes effect
without reloading the package.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

# For the side effect: config.load_env() puts the .env file into os.environ, and
# everything below reads os.environ directly. Imported here as well as in
# __init__ so `python -m congress_trades.llm` behaves like the package does.
from . import config as _config  # noqa: F401

DEFAULT_TIMEOUT = 300
ANTHROPIC_BASE = "https://api.anthropic.com"
# Models that run Anthropic's safety classifiers and accept the server-side
# fallback, which re-runs a declined request on another model inside the same
# call. Elsewhere the parameter is left off.
_FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5",
                    "claude-sonnet-5-5"}


class LLMError(RuntimeError):
    """A call failed in a way the caller may want to survive.

    Distinct from SystemExit on purpose: `advise` is one prompt and dying is
    correct, but the batch passes make hundreds of calls and one bad batch must
    not discard the work already cached.

    `status` carries the HTTP code when there was one, so the response_format
    fallback can test it directly instead of reading it back out of the message.
    """

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Settings:
    provider: str
    model: str
    base: str
    key: str = ""
    num_ctx: int = 8192
    think: bool = False
    keep_alive: str = "5m"

    def describe(self) -> str:
        if self.provider == "ollama":
            return f"ollama {self.model} at {self.base} (num_ctx={self.num_ctx})"
        return f"{self.model} at {self.base}"


def settings() -> Settings:
    provider = os.getenv("CONGRESS_LLM_PROVIDER", "openai").strip().lower()
    if provider not in ("openai", "ollama", "anthropic"):
        raise SystemExit(
            f"CONGRESS_LLM_PROVIDER={provider!r} is not a provider. Use 'openai' "
            "for any /chat/completions endpoint, 'anthropic' for Claude, or "
            "'ollama' for a local Ollama.")
    if provider == "ollama":
        model = os.getenv("CONGRESS_OLLAMA_MODEL", "").strip()
        if not model:
            raise SystemExit(
                "CONGRESS_OLLAMA_MODEL is not set. Pick one you have pulled -- "
                "`ollama list` shows them.")
        return Settings(
            provider="ollama", model=model,
            base=os.getenv("CONGRESS_OLLAMA_BASE",
                           "http://127.0.0.1:11434").rstrip("/"),
            num_ctx=int(os.getenv("CONGRESS_OLLAMA_NUM_CTX", "8192")),
            think=os.getenv("CONGRESS_OLLAMA_THINK", "0").strip().lower()
            in ("1", "true", "yes", "on"),
            keep_alive=os.getenv("CONGRESS_OLLAMA_KEEP_ALIVE", "5m"))
    model = os.getenv("CONGRESS_LLM_MODEL", "").strip()
    if provider == "anthropic":
        if not model:
            raise SystemExit("CONGRESS_LLM_MODEL is not set. Pick a Claude model, "
                             "e.g. CONGRESS_LLM_MODEL=claude-opus-5-5.")
        return Settings(provider="anthropic", model=model, base=ANTHROPIC_BASE,
                        key=os.getenv("CONGRESS_LLM_KEY", "").strip()
                        or os.getenv("ANTHROPIC_API_KEY", "").strip())
    if not model:
        raise SystemExit(
            "CONGRESS_LLM_MODEL is not set. Pick a model your endpoint serves, "
            "e.g. CONGRESS_LLM_MODEL=reason for a LiteLLM route -- or set "
            "CONGRESS_LLM_PROVIDER=ollama to use a local Ollama instead.")
    return Settings(
        provider="openai", model=model,
        base=os.getenv("CONGRESS_LLM_BASE", "http://127.0.0.1:4000/v1").rstrip("/"),
        key=os.getenv("CONGRESS_LLM_KEY", ""))


# ------------------------------------------------------------------ transport
def _post(url: str, body: dict, timeout: int, headers: dict | None = None) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read()[:400].decode(errors="replace")
        raise LLMError(f"{url} answered {e.code}: {detail}", e.code) from e
    except urllib.error.URLError as e:
        raise LLMError(f"cannot reach {url}: {e.reason}") from e
    except (ValueError, TimeoutError) as e:
        raise LLMError(f"{url}: {e}") from e


def _ollama(s: Settings, system: str, prompt: str, schema: dict | None,
            temperature: float, timeout: int) -> str:
    body = {
        "model": s.model, "stream": False, "think": s.think,
        "keep_alive": s.keep_alive,
        "options": {"num_ctx": s.num_ctx, "temperature": temperature},
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": prompt}],
    }
    if schema:
        body["format"] = schema
    d = _post(f"{s.base}/api/chat", body, timeout)
    msg = (d.get("message") or {})
    content = msg.get("content")
    if not content:
        # A thinking model with think left on can spend the whole budget in
        # `thinking` and return empty content. Say which, rather than "no reply".
        if msg.get("thinking"):
            raise LLMError(
                f"{s.model} returned only reasoning and no answer. Leave "
                "CONGRESS_OLLAMA_THINK unset, or raise CONGRESS_OLLAMA_NUM_CTX.")
        raise LLMError(f"unexpected response shape: {json.dumps(d)[:400]}")
    return content


def _openai(s: Settings, system: str, prompt: str, schema: dict | None,
            temperature: float, timeout: int) -> str:
    base_body = {
        "model": s.model, "temperature": temperature,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": prompt}],
    }
    headers = {"Authorization": f"Bearer {s.key}"} if s.key else {}
    # Structured-output support across "OpenAI-compatible" endpoints is uneven,
    # and an endpoint that does not know a response_format usually 400s rather
    # than ignoring it. Degrade rather than fail: the schema is also spelled out
    # in the system prompt by every caller here, so a bare call still answers.
    attempts: list[dict] = []
    if schema:
        attempts.append({"response_format": {
            "type": "json_schema",
            "json_schema": {"name": "reply", "schema": schema, "strict": True}}})
        attempts.append({"response_format": {"type": "json_object"}})
    attempts.append({})
    last: LLMError | None = None
    for extra in attempts:
        try:
            d = _post(f"{s.base}/chat/completions", {**base_body, **extra},
                      timeout, headers)
        except LLMError as e:
            last = e
            # A 4xx means this endpoint rejected the request as written, so the
            # next, simpler shape may be accepted. Anything else -- unreachable,
            # a 5xx, a timeout -- will not be fixed by asking differently.
            if e.status is not None and 400 <= e.status < 500:
                continue
            raise
        try:
            return d["choices"][0]["message"]["content"]
        except (KeyError, IndexError):
            raise LLMError(f"unexpected response shape: {json.dumps(d)[:400]}")
    raise last or LLMError("no attempt succeeded")


def anthropic_login() -> str:
    """How the SDK will authenticate when no key is given, or "" if it cannot.

    `ant auth login` (Anthropic's CLI) signs in through the browser and leaves a
    profile the SDK finds on its own, so there is no key to paste or store.
    """
    if os.getenv("ANTHROPIC_AUTH_TOKEN", "").strip():
        return "ANTHROPIC_AUTH_TOKEN"
    if os.getenv("ANTHROPIC_PROFILE", "").strip():
        return f"ant profile {os.environ['ANTHROPIC_PROFILE'].strip()}"
    cfg = os.getenv("ANTHROPIC_CONFIG_DIR") or os.path.join(
        os.path.expanduser("~"), ".config", "anthropic")
    try:
        if any(f.endswith(".json") for f in os.listdir(os.path.join(cfg, "credentials"))):
            return "ant auth login"
    except OSError:
        pass
    return ""


def _anthropic_sdk():
    try:
        import anthropic
    except ImportError:
        raise LLMError("the anthropic provider needs the official SDK: "
                       "pip install anthropic") from None
    return anthropic


def _anthropic(s: Settings, system: str, prompt: str, schema: dict | None,
               temperature: float, timeout: int) -> str:
    # `temperature` is not sent: current Claude models reject sampling
    # parameters outright, and the schema below is what keeps a reply in shape.
    anthropic = _anthropic_sdk()
    client = anthropic.Anthropic(api_key=s.key or None, timeout=timeout)
    body = {"model": s.model, "max_tokens": 16000, "system": system,
            "messages": [{"role": "user", "content": prompt}]}
    if s.model in _FALLBACK_MODELS:
        body |= {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
    create = client.beta.messages.create if "betas" in body else client.messages.create
    # Like _openai: a schema the API will not take (it wants additionalProperties
    # false throughout) is a 400, and the prompt spells the schema out anyway.
    attempts = ([{"output_config": {"format": {"type": "json_schema", "schema": schema}}}]
                if schema else []) + [{}]
    for i, extra in enumerate(attempts):
        try:
            r = create(**body, **extra)
        except anthropic.BadRequestError as e:
            if i + 1 < len(attempts):
                continue
            raise LLMError(f"Anthropic answered 400: {e.message}", 400) from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"Anthropic answered {e.status_code}: {e.message}",
                           e.status_code) from e
        except anthropic.APIConnectionError as e:
            raise LLMError(f"cannot reach Anthropic: {e}") from e
        if r.stop_reason == "refusal":
            cat = getattr(r.stop_details, "category", None) if r.stop_details else None
            raise LLMError(f"{s.model} declined the request"
                           + (f" ({cat})" if cat else ""))
        text = "".join(b.text for b in r.content if b.type == "text")
        if not text:
            raise LLMError(f"{s.model} returned no text (stop_reason {r.stop_reason})")
        return text
    raise LLMError("no attempt succeeded")


def ask(prompt: str, system: str, timeout: int = DEFAULT_TIMEOUT,
        schema: dict | None = None, temperature: float = 0.2,
        s: Settings | None = None) -> str:
    s = s or settings()
    fn = {"ollama": _ollama, "anthropic": _anthropic}.get(s.provider, _openai)
    return fn(s, system, prompt, schema, temperature, timeout)


def list_models(s: Settings, timeout: int = 15) -> list[str]:
    """The model ids an endpoint says it serves -- the live answer to "which
    model can I pick here", where any list written into this file would age."""
    if s.provider == "anthropic":
        anthropic = _anthropic_sdk()
        try:
            return [m.id for m in anthropic.Anthropic(
                api_key=s.key or None, timeout=timeout).models.list()]
        except anthropic.APIError as e:
            raise LLMError(f"Anthropic: {getattr(e, 'message', e)}") from e
    if s.provider == "ollama":
        url, headers = f"{s.base}/api/tags", {}
    else:
        url = f"{s.base}/models"
        headers = {"Authorization": f"Bearer {s.key}"} if s.key else {}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers),
                                    timeout=timeout) as r:
            d = json.load(r)
    except urllib.error.HTTPError as e:
        raise LLMError(f"{url} answered {e.code}: "
                       f"{e.read()[:200].decode(errors='replace')}", e.code) from e
    except (urllib.error.URLError, ValueError, TimeoutError) as e:
        raise LLMError(f"cannot reach {url}: {getattr(e, 'reason', e)}") from e
    rows = d.get("models") if s.provider == "ollama" else d.get("data")
    return sorted(m.get("name") or m.get("id") for m in rows or []
                  if m.get("name") or m.get("id"))


def ask_json(prompt: str, system: str, schema: dict,
             timeout: int = DEFAULT_TIMEOUT, s: Settings | None = None):
    """A reply parsed as JSON, with one retry and no salvage heuristics.

    Deliberately no regex hunt for the first {...} in the text. A model that
    wrapped its answer in prose has not followed the schema, and quietly digging
    the JSON out of a reply that ignored its instructions is how a classification
    pass ends up storing a label that was never in the vocabulary.
    """
    s = s or settings()
    err = ""
    for attempt in range(2):
        text = ask(prompt if not err else
                   f"{prompt}\n\nYour previous reply was not valid JSON ({err}). "
                   "Reply with JSON only, matching the schema, and nothing else.",
                   system, timeout, schema, 0.0 if attempt else 0.1, s)
        try:
            return json.loads(text.strip())
        except ValueError as e:
            err = str(e)[:120]
    raise LLMError(f"{s.model} did not return valid JSON after two attempts: {err}")


def preflight(s: Settings | None = None) -> str:
    """Fail before a long batch rather than on its first call.

    For Ollama this also catches the most common setup mistake by far -- a model
    name that is not pulled -- which otherwise surfaces as a 404 several minutes
    into a run.
    """
    s = s or settings()
    if s.provider == "anthropic":
        try:
            _anthropic_sdk()
        except LLMError as e:
            raise SystemExit(str(e))
        if not s.key and not anthropic_login():
            raise SystemExit("No Anthropic credentials: run `ant auth login`, or "
                             "set CONGRESS_LLM_KEY to an API key.")
    if s.provider == "ollama":
        try:
            with urllib.request.urlopen(f"{s.base}/api/tags", timeout=10) as r:
                have = [m["name"] for m in (json.load(r).get("models") or [])]
        except (urllib.error.URLError, ValueError, TimeoutError) as e:
            raise SystemExit(f"cannot reach Ollama at {s.base}: {e}. Is `ollama "
                             "serve` running?")
        # `ollama run qwen3` resolves the implicit :latest; match that here so a
        # name that works on the command line works in the .env too.
        names = set(have) | {n.split(":")[0] for n in have}
        if s.model not in names and f"{s.model}:latest" not in set(have):
            raise SystemExit(
                f"Ollama has no model {s.model!r}. Pulled: "
                f"{', '.join(sorted(have)) or 'none'}. "
                f"Run `ollama pull {s.model}` first.")
    return s.describe()


def check(s: Settings | None = None, timeout: int = 120) -> int:
    """A live round trip against whatever is configured, and a report on it.

    selftest() deliberately calls no model so it can run anywhere; this is the
    other half. It answers the question you actually have after changing a
    setting -- "is it talking to the thing I think it is, and can it do what
    resolve and topics need?" -- and the second of those is the one that bites:
    an endpoint that ignores a JSON schema still answers, plausibly, and the
    damage only shows up as labels outside their vocabulary much later.

        congress-trades llm
    """
    s = s or settings()
    print(f"provider     {s.provider}")
    print(f"model        {s.model}")
    print(f"endpoint     {s.base}")
    if s.provider == "ollama":
        print(f"num_ctx      {s.num_ctx}")
        print(f"think        {'on' if s.think else 'off'}")
        print(f"keep_alive   {s.keep_alive}")
    elif s.provider == "anthropic" and not s.key:
        print(f"credentials  {anthropic_login() or 'none'}")
    else:
        print(f"api key      {'set' if s.key else 'not set'}")
    print(f"\nreachable    ", end="", flush=True)
    print(preflight(s))          # raises SystemExit with something actionable

    print("completion   ", end="", flush=True)
    t0 = time.monotonic()
    try:
        txt = ask("Reply with the single word: ready.",
                  "You reply with one word and nothing else.", timeout, s=s)
    except LLMError as e:
        print(f"FAILED — {e}")
        return 1
    print(f"ok, {time.monotonic() - t0:.1f}s, said {txt.strip()[:40]!r}")

    # The capability resolve and topics actually rest on. An endpoint that
    # ignores `format`/`response_format` gets this far and fails here.
    print("json schema  ", end="", flush=True)
    demo = {"type": "object",
            "properties": {"label": {"type": "string",
                                     "enum": ["Treasuries", "Crypto"]},
                           "n": {"type": "integer"}},
            "required": ["label", "n"], "additionalProperties": False}
    t0 = time.monotonic()
    try:
        got = ask_json("Classify 'Bitcoin' and set n to 7.",
                       "You return JSON matching the schema, nothing else.",
                       demo, timeout, s)
    except LLMError as e:
        print(f"FAILED — {e}")
        print("\n  This endpoint did not return JSON matching a schema. `advise` "
              "will still\n  work; `resolve` and `topics` need it and would store "
              "labels that fell\n  outside their vocabulary.")
        return 1
    if not isinstance(got, dict) or got.get("label") not in ("Treasuries", "Crypto") \
            or not isinstance(got.get("n"), int):
        print(f"FAILED — schema not honoured, got {got!r}")
        return 1
    print(f"ok, {time.monotonic() - t0:.1f}s, enum respected, got {got!r}")
    print(f"\n{s.describe()} is ready for advise, resolve and topics.")
    return 0


def selftest():
    """Shape checks only -- no model is called, so this runs in CI."""
    import contextlib

    @contextlib.contextmanager
    def env(**kw):
        old = {k: os.environ.get(k) for k in kw}
        os.environ.update({k: v for k, v in kw.items() if v is not None})
        for k, v in kw.items():
            if v is None:
                os.environ.pop(k, None)
        try:
            yield
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    with env(CONGRESS_LLM_PROVIDER="ollama", CONGRESS_OLLAMA_MODEL="m",
             CONGRESS_OLLAMA_NUM_CTX="4096"):
        s = settings()
        assert s.provider == "ollama" and s.num_ctx == 4096 and not s.think
        assert s.base == "http://127.0.0.1:11434"
    with env(CONGRESS_LLM_PROVIDER="ollama", CONGRESS_OLLAMA_MODEL="m",
             CONGRESS_OLLAMA_THINK="yes"):
        assert settings().think is True
    with env(CONGRESS_LLM_PROVIDER="openai", CONGRESS_LLM_MODEL="gpt-4o",
             CONGRESS_LLM_BASE="https://api.example.com/v1/"):
        s = settings()
        assert s.provider == "openai" and s.base == "https://api.example.com/v1", s
    with env(CONGRESS_LLM_PROVIDER="anthropic", CONGRESS_LLM_MODEL="claude-opus-5-5",
             CONGRESS_LLM_KEY="k"):
        s = settings()
        assert (s.provider, s.key, s.base) == ("anthropic", "k", ANTHROPIC_BASE), s
    for bad in ({"CONGRESS_LLM_PROVIDER": "nonesuch"},
                {"CONGRESS_LLM_PROVIDER": "anthropic", "CONGRESS_LLM_MODEL": ""},
                {"CONGRESS_LLM_PROVIDER": "ollama", "CONGRESS_OLLAMA_MODEL": ""},
                {"CONGRESS_LLM_PROVIDER": "openai", "CONGRESS_LLM_MODEL": ""}):
        with env(**{k: v for k, v in bad.items()}):
            try:
                settings()
            except SystemExit:
                pass
            else:
                raise AssertionError(f"{bad} should not have been accepted")
    print("selftest ok: three providers, settings read at call time")


if __name__ == "__main__":
    selftest()
