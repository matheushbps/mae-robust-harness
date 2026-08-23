from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class RunRequest(BaseModel):
    harness: Literal["simple", "robust"]
    prompt: str = Field(min_length=20, max_length=20_000)
    provider: Literal["local-qwen"] = "local-qwen"
    agent_prompts: dict[str, str] | None = None

    @field_validator("prompt")
    @classmethod
    def normalize_prompt(cls, value: str) -> str:
        return value.strip()

    @field_validator("agent_prompts")
    @classmethod
    def validate_agent_prompts(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        if value is None:
            return None
        if len(value) > 8:
            raise ValueError("At most eight agent prompt overrides are allowed.")
        normalized: dict[str, str] = {}
        for role, prompt in value.items():
            role = role.strip()
            prompt = prompt.strip()
            if not role or len(role) > 64:
                raise ValueError("Agent roles must be non-empty and at most 64 characters.")
            if not prompt or len(prompt) > 6_000:
                raise ValueError("Agent prompt overrides must be non-empty and at most 6000 characters.")
            normalized[role] = prompt
        return normalized


class RunEvent(BaseModel):
    sequence: int
    timestamp: datetime = Field(default_factory=utc_now)
    node: str
    event_type: str
    message: str
    data: dict[str, Any] = Field(default_factory=dict)


class LLMTrace(BaseModel):
    role: str
    content: str
    reasoning_content: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    latency_seconds: float = 0.0
    finish_reason: str | None = None


class EvidenceItem(BaseModel):
    evidence_id: str
    match_key: str
    branch: str
    crop_code: str
    crop_name: str
    metric: str
    start_year: int
    end_year: int
    start_value: float | None
    end_value: float | None
    change_percent: float | None
    unit: str
    method: str
    provenance: dict[str, Any] = Field(default_factory=dict)


class ValidationCheck(BaseModel):
    check_id: str
    passed: bool
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class RunRecord(BaseModel):
    run_id: str
    harness: str
    status: Literal["queued", "running", "completed", "failed"] = "queued"
    prompt_hash: str
    model_id: str
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    events: list[RunEvent] = Field(default_factory=list)
    result: dict[str, Any] | None = None
    error: str | None = None
