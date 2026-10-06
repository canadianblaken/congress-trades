"""The model picker: provider presets, .env writes, live model lists."""
from __future__ import annotations

import re

# --- model picker --------------------------------------------------------------
# Every provider the Maintenance page offers. A cloud provider's endpoint is fixed
# here and never taken from the request: the endpoint is where the API key gets
# sent, so a page that could rewrite it could hand your key to anyone. Only the
# local and custom entries take a base URL from the form.
# Suggestions are a starting point; "List models" asks the provider itself.
PRESETS = [
    {"id": "anthropic", "label": "Anthropic (Claude)", "provider": "anthropic",
     "base": "https://api.anthropic.com", "key": True,
     "models": ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5",
                "claude-fable-5-1"]},
    {"id": "openai", "label": "OpenAI", "provider": "openai",
     "base": "https://api.openai.com/v1", "key": True},
    {"id": "google", "label": "Google (Gemini)", "provider": "openai",
     "base": "https://generativelanguage.googleapis.com/v1beta/openai", "key": True},
    {"id": "xai", "label": "xAI (Grok)", "provider": "openai",
     "base": "https://api.x.ai/v1", "key": True},
    {"id": "mistral", "label": "Mistral", "provider": "openai",
     "base": "https://api.mistral.ai/v1", "key": True},
    {"id": "deepseek", "label": "DeepSeek", "provider": "openai",
     "base": "https://api.deepseek.com/v1", "key": True},
    {"id": "groq", "label": "Groq", "provider": "openai",
     "base": "https://api.groq.com/openai/v1", "key": True},
    {"id": "openrouter", "label": "OpenRouter (many providers)", "provider": "openai",
     "base": "https://openrouter.ai/api/v1", "key": True},
    {"id": "together", "label": "Together AI", "provider": "openai",
     "base": "https://api.together.xyz/v1", "key": True},
    {"id": "ollama", "label": "Local: Ollama", "provider": "ollama",
     "base": "http://127.0.0.1:11434", "key": False, "editable": True},
    {"id": "local", "label": "Local: LiteLLM / LM Studio / vLLM", "provider": "openai",
     "base": "http://127.0.0.1:4000/v1", "key": False, "editable": True},
    {"id": "custom", "label": "Other OpenAI-compatible endpoint", "provider": "openai",
     "base": "", "key": False, "editable": True},
]
_PRESET = {p["id"]: p for p in PRESETS}
MODEL_KEYS = ("CONGRESS_LLM_PRESET", "CONGRESS_LLM_PROVIDER", "CONGRESS_LLM_BASE",
              "CONGRESS_LLM_MODEL", "CONGRESS_LLM_KEY", "CONGRESS_OLLAMA_BASE",
              "CONGRESS_OLLAMA_MODEL")


def model_current() -> dict:
    """What is configured, for the form. The key itself never leaves the server."""
    import os
    g = lambda k: os.getenv(k, "").strip()
    provider = g("CONGRESS_LLM_PROVIDER").lower() or "openai"
    preset = g("CONGRESS_LLM_PRESET")
    if preset not in _PRESET:
        preset = {"ollama": "ollama", "anthropic": "anthropic"}.get(provider, "local")
    ollama = provider == "ollama"
    key = g("CONGRESS_LLM_KEY") or (g("ANTHROPIC_API_KEY") if provider == "anthropic" else "")
    from .. import llm
    return {"preset": preset, "login": llm.anthropic_login(),
            "base": g("CONGRESS_OLLAMA_BASE" if ollama else "CONGRESS_LLM_BASE")
                    or _PRESET[preset]["base"],
            "model": g("CONGRESS_OLLAMA_MODEL" if ollama else "CONGRESS_LLM_MODEL"),
            "key_set": bool(key), "key_tail": key[-4:] if len(key) >= 12 else ""}


def _model_settings(body: dict, for_save: bool):
    """(preset, llm.Settings) from a form submission, or raise ValueError."""
    from .. import llm
    import os
    preset = _PRESET.get(str(body.get("preset", "")))
    if not preset:
        raise ValueError("unknown provider")
    base = preset["base"]
    if preset.get("editable"):
        base = str(body.get("base", "")).strip().rstrip("/")
        if not re.fullmatch(r"https?://[^\s/]+(/[^\s]*)?", base):
            raise ValueError("the endpoint must be an http(s) URL")
    model = str(body.get("model", "")).strip()
    if for_save and not re.fullmatch(r"[\w.:/@+-]{1,200}", model):
        raise ValueError("pick a model")
    key = str(body.get("key", "")).strip()
    if any(c in key for c in "\r\n") or len(key) > 500:
        raise ValueError("that does not look like an API key")
    login = preset["provider"] == "anthropic" and model_current()["login"]
    if not key and not login and model_current()["preset"] == preset["id"]:
        # Blank means "keep the saved one" -- but only for the same provider, so
        # switching from one company to another never sends the old key along.
        key = os.getenv("CONGRESS_LLM_KEY", "").strip() or (
            os.getenv("ANTHROPIC_API_KEY", "").strip()
            if preset["provider"] == "anthropic" else "")
    if preset["key"] and not key and not login:
        raise ValueError(f"{preset['label']} needs an API key"
                         + (", or sign in with `ant auth login`"
                            if preset["provider"] == "anthropic" else ""))
    return preset, llm.Settings(provider=preset["provider"], model=model or "-",
                                base=base, key=key)


def _write_env(updates: dict[str, str]) -> None:
    """Set KEY=value lines in .env in place, keeping every other line and comment.
    Mode 600, because it now holds API keys."""
    import os
    from ..config import ENV_FILE
    lines = ENV_FILE.read_text().splitlines() if ENV_FILE.exists() else []
    left = dict(updates)
    out = []
    for line in lines:
        k = line.split("=", 1)[0].strip().removeprefix("export ").strip()
        if k in left:
            v = left.pop(k)
            if v:
                out.append(f"{k}={v}")
        else:
            out.append(line)
    out += [f"{k}={v}" for k, v in left.items() if v]
    tmp = ENV_FILE.with_suffix(".tmp")
    tmp.write_text("\n".join(out) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(ENV_FILE)


def model_save(body: dict) -> dict:
    """Write the choice to .env and into this process's environment, which every
    job and report subprocess inherits -- so it applies to the next run without a
    restart. A shell that exports these before starting the portal is overridden
    here on purpose: the newest choice is the one on screen."""
    import os
    preset, st = _model_settings(body, for_save=True)
    ollama = st.provider == "ollama"
    updates = {"CONGRESS_LLM_PRESET": preset["id"], "CONGRESS_LLM_PROVIDER": st.provider}
    if ollama:
        updates |= {"CONGRESS_OLLAMA_BASE": st.base, "CONGRESS_OLLAMA_MODEL": st.model}
    else:
        updates |= {"CONGRESS_LLM_BASE": st.base if st.provider == "openai" else "",
                    "CONGRESS_LLM_MODEL": st.model, "CONGRESS_LLM_KEY": st.key}
    _write_env(updates)
    for k, v in updates.items():
        if v:
            os.environ[k] = v
        else:
            os.environ.pop(k, None)
    return model_current()


def model_list(body: dict) -> list[str]:
    from .. import llm
    return llm.list_models(_model_settings(body, for_save=False)[1])


