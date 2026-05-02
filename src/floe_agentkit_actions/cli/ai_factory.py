"""AI model factory — creates OpenAI, Anthropic, or Ollama clients."""

from __future__ import annotations

import json
from typing import Any


class AIClient:
    """Thin wrapper normalizing tool-calling across OpenAI, Anthropic, and Ollama."""

    def __init__(
        self,
        provider: str,
        api_key: str | None = None,
        model: str | None = None,
        ollama_base_url: str | None = None,
    ):
        self.provider = provider
        self.model = model or _default_model(provider)
        self._client: Any = None

        if provider == "openai":
            import openai

            self._client = openai.OpenAI(api_key=api_key)
        elif provider == "claude":
            import anthropic

            self._client = anthropic.Anthropic(api_key=api_key)
        elif provider == "ollama":
            import openai

            base_url = (ollama_base_url or "http://localhost:11434").rstrip("/")
            if not base_url.endswith("/v1"):
                base_url += "/v1"
            self._client = openai.OpenAI(base_url=base_url, api_key="ollama")
        else:
            raise ValueError(f"Unknown AI provider: {provider}")

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> dict[str, Any]:
        """Send a chat completion request and return the response.

        Returns dict with keys:
            content: str | None  -- text response
            tool_calls: list[dict] | None  -- tool call requests
        """
        if self.provider == "claude":
            return self._chat_anthropic(messages, tools, system)
        else:
            return self._chat_openai(messages, tools, system)

    def _chat_openai(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        system: str | None,
    ) -> dict[str, Any]:
        full_messages = []
        if system:
            full_messages.append({"role": "system", "content": system})
        full_messages.extend(messages)

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": full_messages,
        }
        if tools:
            kwargs["tools"] = tools

        response = self._client.chat.completions.create(**kwargs)
        choice = response.choices[0]

        result: dict[str, Any] = {"content": None, "tool_calls": None}

        if choice.message.content:
            result["content"] = choice.message.content

        if choice.message.tool_calls:
            result["tool_calls"] = [
                {
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": json.loads(tc.function.arguments),
                }
                for tc in choice.message.tool_calls
            ]

        return result

    def _chat_anthropic(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        system: str | None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 4096,
            "messages": messages,
        }
        if system:
            kwargs["system"] = system
        if tools:
            # Convert OpenAI-style tools to Anthropic format
            kwargs["tools"] = [
                {
                    "name": t["function"]["name"],
                    "description": t["function"].get("description", ""),
                    "input_schema": t["function"].get(
                        "parameters", {"type": "object", "properties": {}}
                    ),
                }
                for t in tools
            ]

        response = self._client.messages.create(**kwargs)
        result: dict[str, Any] = {"content": None, "tool_calls": None}

        for block in response.content:
            if block.type == "text":
                result["content"] = (result["content"] or "") + block.text
            elif block.type == "tool_use":
                if result["tool_calls"] is None:
                    result["tool_calls"] = []
                result["tool_calls"].append(
                    {
                        "id": block.id,
                        "name": block.name,
                        "arguments": block.input,
                    }
                )

        return result


def create_ai_client(config: dict[str, Any]) -> AIClient:
    """Create an AIClient from config dict.

    Config keys:
        provider: "openai" | "claude" | "ollama"
        api_key: str | None
        model: str | None
        ollama_base_url: str | None
    """
    return AIClient(
        provider=config["provider"],
        api_key=config.get("api_key"),
        model=config.get("model"),
        ollama_base_url=config.get("ollama_base_url"),
    )


def validate_ollama_connection(base_url: str) -> bool:
    """Check if Ollama is reachable."""
    import urllib.request

    url = base_url.rstrip("/").removesuffix("/api") + "/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False


def _default_model(provider: str) -> str:
    return {
        "openai": "gpt-4o",
        "claude": "claude-sonnet-4-5-20250514",
        "ollama": "llama3.1",
    }.get(provider, "unknown")
