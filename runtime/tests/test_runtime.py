from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mae_runtime.analytics import (
    execute_readonly_sql,
    profile_dataset,
    reconcile_evidence,
    run_python_analysis,
    run_sql_analysis,
)
from mae_runtime.config import Settings
from mae_runtime.contracts import LLMTrace
from mae_runtime.dataset import build_fixture_database, estimate_dataset
from mae_runtime.graph import GRAPH_MERMAID, RobustHarness
from mae_runtime.model_client import QwenClient


class StubModel:
    model_id = "qwen/qwen3.6-35b-a3b"

    def __init__(self) -> None:
        self.systems: dict[str, str] = {}

    def health(self) -> dict[str, Any]:
        return {"connected": True, "available": True, "model": self.model_id}

    def complete_json(
        self, role: str, system: str, user: str, max_tokens: int | None = None
    ) -> tuple[dict[str, Any], LLMTrace]:
        del user, max_tokens
        self.systems[role] = system
        if role in ("business_agent", "business_analyst"):
            payload = {
                "business_questions": ["What changed?"],
                "metrics": ["production"],
                "units": {"production": "tonnes"},
                "acceptance_criteria": ["SQL and Python agree"],
                "exclusions": ["prediction"],
            }
        elif role in ("sql_agent", "sql_analyst"):
            payload = {
                "selected_metrics": ["production"],
                "comparison_period": [2019, 2024],
                "risks": ["missing values"],
            }
        elif role in ("dashboard_agent", "dashboard_engineer"):
            payload = {
                "title": "Municipal Crop Intelligence",
                "subtitle": "Strategic Executive Highlights",
                "insights": ["Planted area increased in grains", "Yield efficiency improved"],
                "visual_theme": "cyber_dark",
            }
        else:
            payload = {
                "selected_checks": ["independent totals"],
                "comparison_period": [2019, 2024],
                "risks": ["missing values"],
            }
        return payload, LLMTrace(role=role, content="{}", completion_tokens=10)

    def complete(self, role: str, system: str, user: str, max_tokens: int | None = None) -> LLMTrace:
        del user, max_tokens
        self.systems[role] = system
        return LLMTrace(
            role=role,
            content="Approved evidence [sql:40124:production_tonnes].",
            completion_tokens=12,
        )


@pytest.fixture
def dataset_path(tmp_path: Path) -> Path:
    path = tmp_path / "fixture.duckdb"
    build_fixture_database(path)
    return path


def settings_for(tmp_path: Path, dataset_path: Path) -> Settings:
    return Settings(
        runtime_host="127.0.0.1",
        runtime_port=8788,
        model_base_url="http://127.0.0.1:1234/v1",
        model_id="qwen/qwen3.6-35b-a3b",
        model_api_key=None,
        model_timeout_seconds=30,
        max_completion_tokens=512,
        temperature=0,
        dataset_path=dataset_path,
        artifacts_dir=tmp_path / "outputs",
        checkpoint_path=tmp_path / "checkpoints.sqlite",
        numeric_tolerance=1e-9,
        max_repair_attempts=2,
    )


def test_dataset_estimate_is_bounded() -> None:
    estimate = estimate_dataset()
    assert estimate["maximum_wide_rows"] == 233_940
    assert estimate["estimated_duckdb_mb"][1] <= 25


def test_fixture_profile_and_independent_agreement(dataset_path: Path) -> None:
    profile = profile_dataset(dataset_path)
    assert profile["rows"] == 12
    assert profile["manifest_present"]
    assert len(profile["dataset_sha256"]) == 64
    sql_evidence = run_sql_analysis(dataset_path)
    python_evidence = run_python_analysis(dataset_path)
    checks, approved = reconcile_evidence(sql_evidence, python_evidence, tolerance=1e-9)
    assert all(check.passed for check in checks)
    assert len(approved) == 8
    assert all(len(item.provenance["dataset_sha256"]) == 64 for item in approved)


def test_readonly_sql_rejects_mutation(dataset_path: Path) -> None:
    assert execute_readonly_sql(dataset_path, "SELECT crop_code FROM crop_metrics")["rows"]
    with pytest.raises(ValueError, match="Only SELECT"):
        execute_readonly_sql(dataset_path, "DELETE FROM crop_metrics")


def test_model_adapter_separates_structured_and_narrative_reasoning(
    dataset_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reasoning_modes: list[str] = []

    class Response:
        @staticmethod
        def raise_for_status() -> None:
            return None

        @staticmethod
        def json() -> dict[str, Any]:
            return {
                "output": [{"type": "message", "content": "{}"}],
                "stats": {
                    "input_tokens": 5,
                    "total_output_tokens": 1,
                    "reasoning_output_tokens": 0,
                },
            }

    def fake_post(*_args: Any, **kwargs: Any) -> Response:
        reasoning_modes.append(kwargs["json"]["reasoning"])
        assert kwargs["json"]["store"] is False
        return Response()

    monkeypatch.setattr("mae_runtime.model_client.httpx.post", fake_post)
    client = QwenClient(settings_for(tmp_path, dataset_path))
    client.complete_json("contract", "system", "user")
    client.complete("editor", "system", "user")
    assert reasoning_modes == ["off", "on"]


def test_robust_langgraph_completes_with_checkpoints(dataset_path: Path, tmp_path: Path) -> None:
    events: list[tuple[str, str]] = []
    settings = settings_for(tmp_path, dataset_path)
    model = StubModel()
    harness = RobustHarness(model, settings)
    result = harness.run(
        "robust-test",
        "[TASK:mae-certified-release-v2] Analyze agricultural changes in the controlled fixture dataset.",
        lambda node, event_type, _message, _data=None: events.append((node, event_type)),
        agent_prompts={"sql_agent": "CUSTOM SQL AGENT SYSTEM"},
    )
    assert result["harness"] == "robust"
    assert result["terminal_status"] == "completed"
    assert result["repair_count"] == 0
    assert result["release_certificate"]["status"] == "certified"
    assert result["release_certificate"]["approved_metrics"] == 8
    assert "CUSTOM SQL AGENT SYSTEM" in model.systems["sql_agent"]
    assert result["applied_prompt_overrides"][0]["agent_id"] == "sql_agent"
    assert len(result["applied_prompt_overrides"][0]["sha256"]) == 64
    assert len(result["approved_evidence"]) == 8
    assert len(result["inter_agent_messages"]) >= 6
    assert settings.checkpoint_path.exists()
    assert ("sql_agent", "started") in events
    assert ("python_agent", "started") in events
    assert ("dashboard_agent", "started") in events
    assert ("final_editor", "completed") in events
    assert "reconciliation_gate" in GRAPH_MERMAID

    # Verify generated artifacts including HTML dashboard
    run_dir = settings.artifacts_dir / "robust-test"
    json_dashboard = run_dir / "dashboard.json"
    html_dashboard = run_dir / "dashboard.html"
    assert json_dashboard.exists() and json_dashboard.stat().st_size > 0
    assert html_dashboard.exists() and html_dashboard.stat().st_size > 0
    html_text = html_dashboard.read_text(encoding="utf-8")
    assert "<!DOCTYPE html>" in html_text
    assert 'id="kpis"' in html_text


def test_robust_rejects_prompt_override_for_deterministic_role(
    dataset_path: Path, tmp_path: Path
) -> None:
    harness = RobustHarness(StubModel(), settings_for(tmp_path, dataset_path))
    with pytest.raises(ValueError, match="not inference-backed"):
        harness.run(
            "invalid-prompt-role",
            "Analyze agricultural changes in the controlled fixture dataset.",
            lambda *_args: None,
            agent_prompts={"sql_reviewer": "This cannot affect a deterministic reviewer."},
        )


def test_robust_uses_certified_fallback_when_final_editor_returns_no_text(
    dataset_path: Path, tmp_path: Path
) -> None:
    events: list[tuple[str, str]] = []

    class EmptyFinalEditorModel(StubModel):
        def complete(
            self, role: str, system: str, user: str, max_tokens: int | None = None
        ) -> LLMTrace:
            del system, user, max_tokens
            return LLMTrace(role=role, content="", completion_tokens=512)

    harness = RobustHarness(EmptyFinalEditorModel(), settings_for(tmp_path, dataset_path))
    result = harness.run(
        "empty-final-editor",
        "[TASK:mae-certified-release-v2] Analyze agricultural changes in the controlled fixture dataset.",
        lambda node, event_type, _message, _data=None: events.append((node, event_type)),
    )

    assert result["terminal_status"] == "completed"
    assert "Certified evidence release" in result["narrative"]
    assert "[sql:" in result["narrative"]
    assert ("final_editor", "deterministic_fallback") in events
