"""Hybrid LLM Pipeline — NVIDIA NIM (Cloud) + Ollama (Local).

Routing strategy:
  Stage 2  ENRICH  → Ollama gemma3:4b         (fast local extraction)
  Stage 3  SCORE   → NVIDIA deepseek-ai/deepseek-v3 (cloud, high reasoning)
  Stage 4  TAILOR  → NVIDIA deepseek-ai/deepseek-r1-distill-qwen-14b (cloud, writing)
  Stage 5  COVER   → NVIDIA deepseek-ai/deepseek-r1-distill-qwen-14b (cloud, writing)
  Stage 7  APPLY   → Claude Code subprocess (unchanged)

All NVIDIA calls fall back to local Ollama on failure.
DeepSeek-R1 <think>...</think> blocks are stripped from output and logged.
"""
from __future__ import annotations

import logging
import os
import re

log = logging.getLogger(__name__)

_NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"


# ── Utilities ─────────────────────────────────────────────────────────────

def strip_thinking_tags(text: str) -> str:
    """Strip <think>...</think> blocks from DeepSeek-R1 output.

    The thinking content is logged at DEBUG level for inspection.
    Only the final answer after </think> is returned.
    """
    pattern = re.compile(r"<think>(.*?)</think>", re.DOTALL | re.IGNORECASE)
    thoughts = pattern.findall(text)
    if thoughts:
        for i, thought in enumerate(thoughts, 1):
            log.debug("[DeepSeek thinking #%d]: %s", i, thought.strip()[:1500])
    cleaned = pattern.sub("", text).strip()
    return cleaned


def _ollama_base_url() -> str:
    base = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
    return base + "/v1"


# ── NVIDIA NIM Client ──────────────────────────────────────────────────────

class NvidiaClient:
    """NVIDIA NIM via OpenAI-compatible endpoint.

    Falls back to local Ollama automatically on any API failure.
    DeepSeek-R1 thinking tags are stripped and logged.
    """

    def __init__(self, model: str, fallback_model: str = "llama3:8b") -> None:
        from openai import OpenAI
        api_key = os.environ.get("NVIDIA_API_KEY", "")
        if not api_key:
            raise RuntimeError("NVIDIA_API_KEY not set")
        self._client = OpenAI(api_key=api_key, base_url=_NVIDIA_BASE_URL)
        self.model = model
        self.base_url = _NVIDIA_BASE_URL
        self._fallback_model = fallback_model

    def chat(self, messages: list[dict], temperature: float = 0.6,
             max_tokens: int = 4096) -> str:
        try:
            response = self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            content = response.choices[0].message.content or ""
            return strip_thinking_tags(content)
        except Exception as e:
            log.warning(
                "NVIDIA API failed (model=%s): %s — falling back to Ollama %s",
                self.model, e, self._fallback_model,
            )
            return self._ollama_fallback(messages, temperature, max_tokens)

    def ask(self, prompt: str, temperature: float = 0.6, max_tokens: int = 4096,
            system_prompt: str | None = None) -> str:
        messages: list[dict] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        return self.chat(messages, temperature=temperature, max_tokens=max_tokens)

    def _ollama_fallback(self, messages: list[dict], temperature: float,
                         max_tokens: int) -> str:
        from openai import OpenAI
        try:
            client = OpenAI(api_key="ollama", base_url=_ollama_base_url())
            response = client.chat.completions.create(
                model=self._fallback_model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            return strip_thinking_tags(response.choices[0].message.content or "")
        except Exception as e2:
            log.error("Ollama fallback also failed (model=%s): %s",
                      self._fallback_model, e2)
            return ""


# ── Ollama Client ──────────────────────────────────────────────────────────

class OllamaClient:
    """Local Ollama via OpenAI-compatible endpoint (http://localhost:11434/v1)."""

    def __init__(self, model: str) -> None:
        from openai import OpenAI
        self._client = OpenAI(api_key="ollama", base_url=_ollama_base_url())
        self.model = model
        self.base_url = _ollama_base_url()

    def chat(self, messages: list[dict], temperature: float = 0.0,
             max_tokens: int = 4096) -> str:
        try:
            response = self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            return strip_thinking_tags(response.choices[0].message.content or "")
        except Exception as e:
            log.error("Ollama call failed (model=%s): %s", self.model, e)
            return ""

    def ask(self, prompt: str, temperature: float = 0.0, max_tokens: int = 4096,
            system_prompt: str | None = None) -> str:
        messages: list[dict] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        return self.chat(messages, temperature=temperature, max_tokens=max_tokens)


# ── Legacy Clients (kept for backward compatibility) ───────────────────────

class GeminiClient:
    """Gemini via its OpenAI-compatible endpoint."""

    _GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"

    def __init__(self, model: str | None = None) -> None:
        from openai import OpenAI
        api_key = os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY not set")
        self._client = OpenAI(api_key=api_key, base_url=self._GEMINI_BASE_URL)
        self.model = model or os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
        self.base_url = self._GEMINI_BASE_URL

    def chat(self, messages: list[dict], temperature: float = 0.0,
             max_tokens: int = 2048) -> str:
        response = self._client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return response.choices[0].message.content or ""

    def ask(self, prompt: str, temperature: float = 0.0, max_tokens: int = 2048,
            system_prompt: str | None = None) -> str:
        messages: list[dict] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        return self.chat(messages, temperature=temperature, max_tokens=max_tokens)


class LLMClient:
    """Legacy Ollama shim via LocalLLMService (kept for backward compat)."""

    def __init__(self) -> None:
        from hireagent.local_llm import LocalLLMService
        from hireagent.config import get_llm_config
        cfg = get_llm_config()
        model = cfg.get("model", "llama3:8b")
        host = cfg.get("base_url", "http://localhost:11434")
        self._svc = LocalLLMService(model=model, host=host)
        self.base_url = self._svc.base_url
        self.model = self._svc.model

    def chat(self, messages: list[dict], temperature: float = 0.0,
             max_tokens: int = 1024) -> str:
        return self._svc.chat(messages, max_tokens=max_tokens, temperature=temperature)

    def ask(self, prompt: str, temperature: float = 0.0, max_tokens: int = 1024,
            system_prompt: str | None = None) -> str:
        messages: list[dict] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        return self.chat(messages, temperature=temperature, max_tokens=max_tokens)


class ClaudeClient:
    """Claude API client (legacy fallback)."""

    session_input_tokens: int = 0
    session_output_tokens: int = 0
    session_calls: int = 0
    _PRICE_INPUT_PER_M = 0.80
    _PRICE_OUTPUT_PER_M = 4.00

    def __init__(self, model: str = "claude-haiku-4-5-20251001") -> None:
        import anthropic
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        self._client = anthropic.Anthropic(api_key=api_key)
        self.model = model
        self.base_url = "https://api.anthropic.com"

    def chat(self, messages: list[dict], temperature: float = 0.0,
             max_tokens: int = 1024) -> str:
        system = ""
        filtered = []
        for m in messages:
            if m["role"] == "system":
                system = m["content"]
            else:
                filtered.append(m)
        kwargs = dict(model=self.model, max_tokens=max_tokens, messages=filtered)
        if system:
            kwargs["system"] = system
        response = self._client.messages.create(**kwargs)
        usage = getattr(response, "usage", None)
        if usage:
            inp = getattr(usage, "input_tokens", 0) or 0
            out = getattr(usage, "output_tokens", 0) or 0
            ClaudeClient.session_input_tokens += inp
            ClaudeClient.session_output_tokens += out
            ClaudeClient.session_calls += 1
            cost = (inp / 1_000_000 * self._PRICE_INPUT_PER_M) + (out / 1_000_000 * self._PRICE_OUTPUT_PER_M)
            log.debug(
                "Claude API call #%d: in=%d out=%d cost=$%.5f",
                ClaudeClient.session_calls, inp, out, cost,
            )
        return response.content[0].text

    def ask(self, prompt: str, temperature: float = 0.0, max_tokens: int = 1024,
            system_prompt: str | None = None) -> str:
        messages: list[dict] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        return self.chat(messages, temperature=temperature, max_tokens=max_tokens)


# ── Stage-Specific Client Getters ─────────────────────────────────────────

def get_score_client():
    """Stage 3 (SCORE): NVIDIA DeepSeek-V3.1, fallback to Ollama llama3:8b."""
    if os.environ.get("NVIDIA_API_KEY"):
        try:
            client = NvidiaClient(
                model="deepseek-ai/deepseek-v3.1",
                fallback_model=os.environ.get("LLM_MODEL", "llama3:8b"),
            )
            log.info("Score LLM: NVIDIA deepseek-ai/deepseek-v3.1")
            return client
        except Exception as e:
            log.warning("NVIDIA score client init failed (%s), using Ollama", e)
    model = os.environ.get("LLM_MODEL", "llama3:8b")
    log.info("Score LLM: Ollama %s", model)
    return OllamaClient(model=model)


def get_tailor_client():
    """Stage 4 (TAILOR): NVIDIA DeepSeek-R1-Distill-Qwen-14B, fallback to Ollama."""
    if os.environ.get("NVIDIA_API_KEY"):
        try:
            client = NvidiaClient(
                model="deepseek-ai/deepseek-r1-distill-qwen-14b",
                fallback_model=os.environ.get("LLM_MODEL", "llama3:8b"),
            )
            log.info("Tailor LLM: NVIDIA deepseek-ai/deepseek-r1-distill-qwen-14b")
            return client
        except Exception as e:
            log.warning("NVIDIA tailor client init failed (%s), using Ollama", e)
    model = os.environ.get("LLM_MODEL", "llama3:8b")
    log.info("Tailor LLM: Ollama %s", model)
    return OllamaClient(model=model)


def get_cover_client():
    """Stage 5 (COVER): NVIDIA DeepSeek-R1-Distill-Qwen-14B, fallback to Ollama."""
    if os.environ.get("NVIDIA_API_KEY"):
        try:
            client = NvidiaClient(
                model="deepseek-ai/deepseek-r1-distill-qwen-14b",
                fallback_model=os.environ.get("LLM_MODEL", "llama3:8b"),
            )
            log.info("Cover LLM: NVIDIA deepseek-ai/deepseek-r1-distill-qwen-14b")
            return client
        except Exception as e:
            log.warning("NVIDIA cover client init failed (%s), using Ollama", e)
    model = os.environ.get("LLM_MODEL", "llama3:8b")
    log.info("Cover LLM: Ollama %s", model)
    return OllamaClient(model=model)


def get_enrich_client():
    """Stage 2 (ENRICH) + Smart Extract: Local Ollama gemma3:4b."""
    model = os.environ.get("ENRICH_LLM_MODEL", "gemma3:4b")
    log.info("Enrich LLM: Ollama %s", model)
    return OllamaClient(model=model)


def get_select_client():
    """Bullet selection + summary generation: Claude Haiku (fast, reliable JSON)."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            model = os.environ.get("SELECT_LLM_MODEL", "claude-haiku-4-5-20251001")
            client = ClaudeClient(model=model)
            log.info("Select LLM: Claude %s", model)
            return client
        except Exception as e:
            log.warning("Claude select client init failed (%s), using Ollama", e)
    model = os.environ.get("LLM_MODEL", "llama3:8b")
    log.info("Select LLM: Ollama %s (fallback)", model)
    return OllamaClient(model=model)


def get_apply_client():
    """Stage 7 (APPLY form fill): NVIDIA → Ollama (no Anthropic, no paid APIs).

    NVIDIA NIM free tier is used when NVIDIA_API_KEY is set.
    Falls back to local Ollama (completely free, no API key needed).
    """
    if os.environ.get("NVIDIA_API_KEY"):
        try:
            # llama-3.1-70b gives best JSON reliability; 8b is faster fallback
            model = os.environ.get("APPLY_LLM_MODEL", "meta/llama-3.1-70b-instruct")
            client = NvidiaClient(
                model=model,
                fallback_model=os.environ.get("LLM_MODEL", "llama3:8b"),
            )
            log.info("Apply form LLM: NVIDIA %s", model)
            return client
        except Exception as e:
            log.warning("NVIDIA apply client init failed (%s), using Ollama", e)
    model = os.environ.get("LLM_MODEL", "llama3:8b")
    log.info("Apply form LLM: Ollama %s", model)
    return OllamaClient(model=model)


def get_client():
    """Legacy getter — routes to score client. Use stage-specific getters instead."""
    return get_score_client()


def get_tailor_token_summary() -> dict:
    """Session-level token usage for the Claude client (legacy)."""
    inp = ClaudeClient.session_input_tokens
    out = ClaudeClient.session_output_tokens
    cost = (inp / 1_000_000 * ClaudeClient._PRICE_INPUT_PER_M) + (out / 1_000_000 * ClaudeClient._PRICE_OUTPUT_PER_M)
    return {
        "calls": ClaudeClient.session_calls,
        "input_tokens": inp,
        "output_tokens": out,
        "cost_usd": cost,
        "model": "claude-haiku-4-5-20251001",
    }


__all__ = [
    "strip_thinking_tags",
    "NvidiaClient",
    "get_apply_client",
    "OllamaClient",
    "GeminiClient",
    "LLMClient",
    "ClaudeClient",
    "get_score_client",
    "get_tailor_client",
    "get_cover_client",
    "get_enrich_client",
    "get_select_client",
    "get_client",
    "get_tailor_token_summary",
]
