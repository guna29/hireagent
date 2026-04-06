"""Local Ollama-backed LLM service (no external network calls).

Uses the OpenAI-compatible endpoint exposed by Ollama on localhost:11434.
Deterministic defaults: low temperature, bounded max tokens.
"""
from __future__ import annotations

import logging
import os
from typing import List, Dict, Any

import requests

log = logging.getLogger(__name__)


class LocalLLMService:
    def __init__(self, model: str | None = None, host: str | None = None):
        self.host = host or os.environ.get("OLLAMA_BASE_URL") or os.environ.get("LLM_URL", "http://localhost:11434")
        self.base_url = self.host.rstrip("/") + "/v1"
        self.model = model or os.environ.get("LLM_MODEL", "llama3:8b")
        self.session = requests.Session()

    def chat(self, messages: List[Dict[str, str]], max_tokens: int = 512, temperature: float = 0.1) -> str:
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        url = f"{self.base_url}/chat/completions"
        resp = self.session.post(url, json=payload, timeout=120)
        resp.raise_for_status()
        data = resp.json()
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as e:
            log.error("Unexpected LLM response: %s", data)
            raise RuntimeError(f"Malformed LLM response: {e}")


__all__ = ["LocalLLMService"]
