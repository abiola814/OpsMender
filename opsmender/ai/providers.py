"""Bring-your-own-key LLM providers.

Two wire protocols cover nearly every provider:
  * OpenAI-compatible ``/chat/completions`` (OpenAI, DeepSeek, Gemini, OpenRouter, Groq, Mistral, Ollama, …)
  * Anthropic's Messages API for Claude, through the official ``anthropic`` SDK
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class Preset:
    id: str
    label: str
    protocol: str  # "openai" | "anthropic"
    base_url: str
    model_hint: str
    needs_key: bool = True
    default_model: str = ""


PRESETS: dict[str, Preset] = {p.id: p for p in (
    Preset("anthropic", "Anthropic Claude", "anthropic", "https://api.anthropic.com",
           "claude-opus-5", default_model="claude-opus-5"),
    Preset("gemini", "Google Gemini", "openai", "https://generativelanguage.googleapis.com/v1beta/openai",
           "e.g. a current gemini-*-flash or -pro model"),
    Preset("deepseek", "DeepSeek", "openai", "https://api.deepseek.com/v1", "e.g. deepseek-chat"),
    Preset("openai", "OpenAI", "openai", "https://api.openai.com/v1", "e.g. a current GPT model id"),
    Preset("openrouter", "OpenRouter", "openai", "https://openrouter.ai/api/v1", "e.g. vendor/model-name"),
    Preset("groq", "Groq", "openai", "https://api.groq.com/openai/v1", "e.g. a Groq-hosted model id"),
    Preset("mistral", "Mistral", "openai", "https://api.mistral.ai/v1", "e.g. mistral-large-latest"),
    Preset("ollama", "Ollama (local, free)", "openai", "http://host.docker.internal:11434/v1",
           "e.g. llama3.1 or qwen2.5", needs_key=False),
    Preset("custom", "Other OpenAI-compatible", "openai", "", "model id"),
)}

# Claude models that support server-side refusal fallbacks (fallbacks: "default").
_FALLBACK_MODELS = ("claude-opus-5", "claude-fable-5")

SYSTEM_PROMPT = (
    "You are OpsMender, a senior DevOps/SRE engineer helping someone who runs their apps on Coolify "
    "(a self-hosted PaaS built on Docker). You are given evidence about one incident: resource details, "
    "status, recent deployments, metrics, rule-based findings, and a redacted log excerpt.\n\n"
    "Work from the evidence. When it is not enough to be sure, say what is missing and how to get it. "
    "Prefer the simplest explanation that fits all the evidence. Never invent log lines or numbers.\n\n"
    "Answer in Markdown with these sections:\n"
    "## What happened\nOne or two plain sentences.\n"
    "## Likely root cause\nThe cause, citing the specific evidence (quote short log fragments).\n"
    "## Confidence\nhigh, medium or low, with one sentence why.\n"
    "## Fix\nNumbered, concrete steps. Name the exact Coolify setting, environment variable, file, or command.\n"
    "## Safe actions in OpsMender\nWhich of restart, redeploy, stop, start would help now, or 'none'.\n"
    "## Prevent it next time\nOne or two short suggestions."
)


class AIError(Exception):
    pass


@dataclass
class AIConfig:
    provider: str
    model: str
    api_key: str | None
    base_url: str | None = None

    @property
    def preset(self) -> Preset:
        return PRESETS.get(self.provider, PRESETS["custom"])


async def complete(cfg: AIConfig, user_prompt: str, system: str = SYSTEM_PROMPT, max_tokens: int = 16000) -> str:
    if not cfg.model:
        raise AIError("No model configured. Set one in Settings → AI provider.")
    if cfg.preset.needs_key and not cfg.api_key:
        raise AIError("No API key configured for the AI provider.")
    if cfg.preset.protocol == "anthropic":
        return await _anthropic(cfg, user_prompt, system, max_tokens)
    return await _openai_compatible(cfg, user_prompt, system, min(max_tokens, 4000))


async def _anthropic(cfg: AIConfig, prompt: str, system: str, max_tokens: int) -> str:
    import anthropic  # imported on first use; keeps idle memory low for non-Claude setups

    client = anthropic.AsyncAnthropic(api_key=cfg.api_key, base_url=cfg.base_url or None, timeout=180.0)
    messages = [{"role": "user", "content": prompt}]
    try:
        if cfg.model.startswith(_FALLBACK_MODELS):
            # If a safety classifier declines, the API re-runs the request on Anthropic's recommended
            # fallback model inside the same call.
            response = await client.beta.messages.create(
                model=cfg.model, max_tokens=max_tokens, system=system, messages=messages,
                betas=["server-side-fallback-2026-07-01"], extra_body={"fallbacks": "default"},
            )
        else:
            response = await client.messages.create(
                model=cfg.model, max_tokens=max_tokens, system=system, messages=messages,
            )
    except anthropic.AuthenticationError as exc:
        raise AIError("Anthropic rejected the API key.") from exc
    except anthropic.NotFoundError as exc:
        raise AIError(f"Unknown Claude model '{cfg.model}'.") from exc
    except anthropic.RateLimitError as exc:
        raise AIError("Anthropic rate limit reached; try again shortly.") from exc
    except anthropic.APIStatusError as exc:
        raise AIError(f"Anthropic API error ({exc.status_code}): {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise AIError(f"Cannot reach the Anthropic API: {exc}") from exc
    finally:
        await client.close()

    if response.stop_reason == "refusal":
        raise AIError("The model declined to answer this request.")
    text = "".join(block.text for block in response.content if block.type == "text").strip()
    if not text:
        raise AIError("The model returned an empty answer.")
    if response.stop_reason == "max_tokens":
        text += "\n\n*(answer truncated)*"
    return text


async def _openai_compatible(cfg: AIConfig, prompt: str, system: str, max_tokens: int) -> str:
    base_url = (cfg.base_url or cfg.preset.base_url).rstrip("/")
    if not base_url:
        raise AIError("No base URL configured for the AI provider.")
    headers = {"Content-Type": "application/json"}
    if cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"
    if cfg.provider == "openrouter":
        headers["X-Title"] = "OpsMender"
    body = {
        "model": cfg.model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
    }
    async with httpx.AsyncClient(timeout=180.0) as client:
        try:
            resp = await client.post(f"{base_url}/chat/completions", json=body, headers=headers)
            if resp.status_code == 400 and "max_completion_tokens" in resp.text:
                # Newer OpenAI reasoning models renamed the parameter.
                body["max_completion_tokens"] = body.pop("max_tokens")
                resp = await client.post(f"{base_url}/chat/completions", json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise AIError(f"Cannot reach {base_url}: {exc}") from exc
    if resp.status_code in (401, 403):
        raise AIError(f"{cfg.preset.label} rejected the API key ({resp.status_code}).")
    if resp.status_code >= 400:
        raise AIError(f"{cfg.preset.label} error ({resp.status_code}): {resp.text[:300]}")
    try:
        data = resp.json()
        message = data["choices"][0]["message"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise AIError(f"Unexpected response from {cfg.preset.label}: {resp.text[:200]}") from exc
    content = message.get("content")
    if isinstance(content, list):  # some providers return content parts
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    text = (content or "").strip()
    if not text:
        raise AIError("The model returned an empty answer.")
    return text
