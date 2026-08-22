from __future__ import annotations

import json
import time
from typing import Any, Protocol

import httpx

from .config import Settings
from .contracts import LLMTrace


class ModelOutputError(RuntimeError):
    pass


class ModelGateway(Protocol):
    model_id: str

    def health(self) -> dict[str, Any]: ...

    def complete(self, role: str, system: str, user: str, max_tokens: int | None = None) -> LLMTrace: ...

    def complete_json(
        self, role: str, system: str, user: str, max_tokens: int | None = None
    ) -> tuple[dict[str, Any], LLMTrace]: ...


class QwenClient:
    def __init__(self, settings: Settings) -> None:
        self.base_url = settings.model_base_url.rstrip("/")
        self.native_base_url = self.base_url.removesuffix("/v1")
        self.model_id = settings.model_id
        self.api_key = settings.model_api_key
        self.timeout = settings.model_timeout_seconds
        self.max_completion_tokens = settings.max_completion_tokens
        self.temperature = settings.temperature

    @property
    def headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def health(self) -> dict[str, Any]:
        response = httpx.get(f"{self.base_url}/models", headers=self.headers, timeout=10.0)
        response.raise_for_status()
        payload = response.json()
        models = [item.get("id") for item in payload.get("data", []) if isinstance(item.get("id"), str)]
        return {
            "connected": True,
            "model": self.model_id,
            "available": self.model_id in models,
            "models": models,
        }

    def complete(self, role: str, system: str, user: str, max_tokens: int | None = None) -> LLMTrace:
        return self._complete_native(role, system, user, max_tokens, reasoning="on")

    def _complete_native(
        self,
        role: str,
        system: str,
        user: str,
        max_tokens: int | None,
        reasoning: str,
    ) -> LLMTrace:
        started = time.perf_counter()
        response = httpx.post(
            f"{self.native_base_url}/api/v1/chat",
            headers=self.headers,
            json={
                "model": self.model_id,
                "system_prompt": system,
                "input": user,
                "temperature": self.temperature,
                "max_output_tokens": max_tokens or self.max_completion_tokens,
                "reasoning": reasoning,
                "stream": False,
                "store": False,
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        output = payload.get("output", [])
        content = "\n".join(
            item.get("content", "") for item in output if item.get("type") == "message"
        ).strip()
        reasoning_content = "\n".join(
            item.get("content", "") for item in output if item.get("type") == "reasoning"
        ).strip()
        stats = payload.get("stats", {})
        return LLMTrace(
            role=role,
            content=content,
            reasoning_content=reasoning_content,
            prompt_tokens=int(stats.get("input_tokens", 0) or 0),
            completion_tokens=int(stats.get("total_output_tokens", 0) or 0),
            reasoning_tokens=int(stats.get("reasoning_output_tokens", 0) or 0),
            latency_seconds=round(time.perf_counter() - started, 4),
            finish_reason="stop" if content else "length_or_empty",
        )

    def complete_json(
        self, role: str, system: str, user: str, max_tokens: int | None = None
    ) -> tuple[dict[str, Any], LLMTrace]:
        trace = self._complete_native(role, system, user, max_tokens, reasoning="off")
        try:
            return extract_json_object(trace.content), trace
        except ModelOutputError as error:
            detail = (
                f"{error} completion_tokens={trace.completion_tokens}, "
                f"reasoning_tokens={trace.reasoning_tokens}, finish={trace.finish_reason}."
            )
            raise ModelOutputError(detail) from error


def extract_json_object(content: str) -> dict[str, Any]:
    cleaned = content.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.replace("```json", "", 1).replace("```", "", 1).strip()
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            raise ModelOutputError("The model did not return a JSON object.") from None
        try:
            value = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as error:
            raise ModelOutputError(f"The model returned invalid JSON: {error.msg}.") from error
    if not isinstance(value, dict):
        raise ModelOutputError("The model returned JSON, but not an object.")
    return value
