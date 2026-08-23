from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mae_runtime.analytics import (
    execute_readonly_sql,
    profile_dataset,
    reconcile_evidence,
    render_dashboard_html,
    run_python_analysis,
    run_sql_analysis,
)
from mae_runtime.config import Settings
from mae_runtime.contracts import LLMTrace
from mae_runtime.dataset import CROPS, build_database_from_rows, build_fixture_database, estimate_dataset
from mae_runtime.graph import GRAPH_MERMAID, RobustHarness, branch_repair_context
from mae_runtime.model_client import QwenClient


class StubModel:
    model_id = "qwen/qwen3.6-35b-a3b"

    def __init__(self) -> None:
        self.systems: dict[str, str] = {}
        self.users: dict[str, str] = {}

    def health(self) -> dict[str, Any]:
        return {"connected": True, "available": True, "model": self.model_id}

    def complete_json(
        self, role: str, system: str, user: str, max_tokens: int | None = None
    ) -> tuple[dict[str, Any], LLMTrace]:
        del max_tokens
        self.systems[role] = system
        self.users[role] = user
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
        del max_tokens
        self.systems[role] = system
        self.users[role] = user
        return LLMTrace(
            role=role,
            content="Approved evidence [sql:40124:production_tonnes].",
            completion_tokens=12,
        )


TEMPORAL_SQL = """
WITH annual AS (
  SELECT crop_code, crop_name, year,
         sum(production_tonnes) AS production_tonnes,
         CASE WHEN sum(harvested_area_ha) > 0
              THEN sum(production_tonnes) * 1000.0 / sum(harvested_area_ha) END
           AS weighted_yield_kg_ha
  FROM crop_metrics
  GROUP BY crop_code, crop_name, year
), changes AS (
  SELECT *,
         lag(production_tonnes) OVER (PARTITION BY crop_code ORDER BY year) AS prior_production,
         dense_rank() OVER (PARTITION BY year ORDER BY production_tonnes DESC) AS production_rank
  FROM annual
), rolling AS (
  SELECT *,
         avg(weighted_yield_kg_ha) OVER (
           PARTITION BY crop_code ORDER BY year ROWS BETWEEN 2 PRECEDING AND CURRENT ROW
         ) AS trailing_3y_yield_kg_ha
  FROM changes
)
SELECT crop_code, crop_name, year, production_tonnes, weighted_yield_kg_ha,
       CASE WHEN prior_production > 0
            THEN (production_tonnes / prior_production - 1) * 100 END AS yoy_production_pct,
       production_rank,
       trailing_3y_yield_kg_ha,
       CASE WHEN trailing_3y_yield_kg_ha > 0
            THEN (weighted_yield_kg_ha / trailing_3y_yield_kg_ha - 1) * 100 END
         AS yield_vs_trailing_pct
FROM rolling
ORDER BY crop_code, year
"""

TEMPORAL_PYTHON = """
def analyze(rows):
    annual = {}
    names = {}
    for row in rows:
        key = (row["crop_code"], row["year"])
        names[row["crop_code"]] = row["crop_name"]
        value = annual.setdefault(key, {"production": 0.0, "harvested": 0.0})
        value["production"] = value["production"] + (row["production_tonnes"] or 0.0)
        value["harvested"] = value["harvested"] + (row["harvested_area_ha"] or 0.0)
    yields = {}
    for key, value in annual.items():
        yields[key] = value["production"] * 1000.0 / value["harvested"] if value["harvested"] else None
    ranks = {}
    for year in range(2019, 2025):
        distinct = sorted(set(annual[(crop, year)]["production"] for crop in names), reverse=True)
        rank_values = {}
        for rank, value in enumerate(distinct, start=1):
            rank_values[value] = rank
        for crop in names:
            ranks[(crop, year)] = rank_values[annual[(crop, year)]["production"]]
    result = []
    for crop in sorted(names):
        history = []
        previous = None
        for year in range(2019, 2025):
            key = (crop, year)
            production = annual[key]["production"]
            current_yield = yields[key]
            history.append(current_yield)
            window = history[max(0, len(history) - 3):]
            trailing = sum(window) / len(window)
            result.append({
                "crop_code": crop,
                "crop_name": names[crop],
                "year": year,
                "production_tonnes": production,
                "weighted_yield_kg_ha": current_yield,
                "yoy_production_pct": (production / previous - 1) * 100 if previous else None,
                "production_rank": ranks[key],
                "trailing_3y_yield_kg_ha": trailing,
                "yield_vs_trailing_pct": (current_yield / trailing - 1) * 100 if trailing else None,
            })
            previous = production
    return result
"""


class GeneratedRepairModel(StubModel):
    def __init__(self) -> None:
        super().__init__()
        self.calls: dict[str, int] = {}

    def complete_json(
        self, role: str, system: str, user: str, max_tokens: int | None = None
    ) -> tuple[dict[str, Any], LLMTrace]:
        self.calls[role] = self.calls.get(role, 0) + 1
        self.systems[role] = system
        self.users[role] = user
        if role == "sql_agent":
            code = "SELECT crop_code FROM crop_metrics" if self.calls[role] == 1 else TEMPORAL_SQL
            return {"code": code, "assumptions": []}, LLMTrace(
                role=role, content="{}", completion_tokens=10
            )
        if role == "python_agent":
            return {"code": TEMPORAL_PYTHON, "assumptions": []}, LLMTrace(
                role=role, content="{}", completion_tokens=10
            )
        return super().complete_json(role, system, user, max_tokens)


class AlwaysInvalidPythonModel(StubModel):
    def complete_json(
        self, role: str, system: str, user: str, max_tokens: int | None = None
    ) -> tuple[dict[str, Any], LLMTrace]:
        self.systems[role] = system
        self.users[role] = user
        if role == "sql_agent":
            return {"code": TEMPORAL_SQL, "assumptions": []}, LLMTrace(
                role=role, content="{}", completion_tokens=10
            )
        if role == "python_agent":
            return {"code": "import math\ndef analyze(rows):\n    return []", "assumptions": []}, LLMTrace(
                role=role, content="{}", completion_tokens=10
            )
        return super().complete_json(role, system, user, max_tokens)


class ExhaustingJsonModel(StubModel):
    def complete_json(
        self, role: str, system: str, user: str, max_tokens: int | None = None
    ) -> tuple[dict[str, Any], LLMTrace]:
        self.systems[role] = system
        self.users[role] = user
        if role == "sql_agent":
            return {"code": TEMPORAL_SQL, "assumptions": []}, LLMTrace(
                role=role, content="{}", completion_tokens=10
            )
        if role == "python_agent":
            raise ValueError("The model returned invalid JSON: Expecting ',' delimiter.")
        return super().complete_json(role, system, user, max_tokens)


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


def full_temporal_fixture(tmp_path: Path) -> Path:
    path = tmp_path / "temporal.duckdb"
    rows: list[dict[str, Any]] = []
    for crop_index, (crop_code, crop_name) in enumerate(CROPS.items(), start=1):
        for year_index, year in enumerate(range(2019, 2025)):
            production = float(crop_index * 100 + year_index * 10)
            harvested = float(50 + crop_index)
            rows.append(
                {
                    "municipality_code": "0000001",
                    "municipality_name": "Fixture",
                    "state_code": "SP",
                    "year": year,
                    "crop_code": crop_code,
                    "crop_name": crop_name,
                    "planted_area_ha": harvested,
                    "harvested_area_ha": harvested,
                    "production_tonnes": production,
                    "yield_kg_ha": production * 1000.0 / harvested,
                    "production_value_thousand_brl": production * 2.0,
                }
            )
    build_database_from_rows(path, rows, mode="temporal-fixture")
    return path


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


def test_robust_dashboard_agent_receives_the_original_visual_request(
    dataset_path: Path, tmp_path: Path
) -> None:
    model = StubModel()
    harness = RobustHarness(model, settings_for(tmp_path, dataset_path))

    harness.run(
        "robust-black-theme",
        "[TASK:mae-certified-release-v2] Analyze the fixture. The dashboard should have a black background!",
        lambda *_args: None,
    )

    assert "black background" in model.users["dashboard_agent"]
    html = (tmp_path / "outputs/robust-black-theme/dashboard.html").read_text()
    assert "--bg: #000000;" in html


def test_robust_dashboard_renderer_applies_structured_visual_theme() -> None:
    rendered = render_dashboard_html(
        {
            "title": "Theme fixture",
            "source": "Fixture",
            "evidence": [],
            "validation": [],
        },
        dashboard_briefing={
            "title": "Theme fixture",
            "visual_theme": {"background": "#ffffff", "accent": "#2563eb"},
        },
    )

    assert "--bg: #ffffff;" in rendered
    assert "--accent: #2563eb;" in rendered


def test_robust_dashboard_renderer_uses_temporal_rows_when_evidence_is_empty() -> None:
    rendered = render_dashboard_html(
        {
            "title": "Temporal fixture",
            "source": "Fixture",
            "evidence": [],
            "validation": [],
            "temporal_rows": [
                {
                    "crop_code": "40124",
                    "crop_name": "Upland cotton (seed)",
                    "year": 2019,
                    "production_tonnes": 10.0,
                    "weighted_yield_kg_ha": 100.0,
                    "yoy_production_pct": None,
                    "production_rank": 2,
                    "trailing_3y_yield_kg_ha": 100.0,
                    "yield_vs_trailing_pct": 0.0,
                },
                {
                    "crop_code": "40124",
                    "crop_name": "Upland cotton (seed)",
                    "year": 2024,
                    "production_tonnes": 20.0,
                    "weighted_yield_kg_ha": 140.0,
                    "yoy_production_pct": 100.0,
                    "production_rank": 1,
                    "trailing_3y_yield_kg_ha": 120.0,
                    "yield_vs_trailing_pct": 16.6666666667,
                },
                {
                    "crop_code": "00001",
                    "crop_name": "Paddy rice",
                    "year": 2019,
                    "production_tonnes": 30.0,
                    "weighted_yield_kg_ha": 200.0,
                    "yoy_production_pct": None,
                    "production_rank": 1,
                    "trailing_3y_yield_kg_ha": 200.0,
                    "yield_vs_trailing_pct": 0.0,
                },
                {
                    "crop_code": "00001",
                    "crop_name": "Paddy rice",
                    "year": 2024,
                    "production_tonnes": 15.0,
                    "weighted_yield_kg_ha": 180.0,
                    "yoy_production_pct": -50.0,
                    "production_rank": 2,
                    "trailing_3y_yield_kg_ha": 190.0,
                    "yield_vs_trailing_pct": -5.2631578947,
                },
            ],
            "generated_analysis": {
                "sql": {"status": "completed"},
                "python": {"status": "completed"},
            },
            "temporal_label": "4 reconciled crop-year rows",
        }
    )

    assert "Reconciled Crop-Year Rows" in rendered
    assert "4" in rendered
    assert "Total Production" in rendered
    assert "Paddy rice" in rendered
    assert "0 ha" not in rendered


def test_robust_dashboard_renderer_shows_placeholder_when_no_data_is_released() -> None:
    rendered = render_dashboard_html(
        {
            "title": "Empty fixture",
            "source": "Fixture",
            "evidence": [],
            "validation": [],
        }
    )

    assert "No released data available" in rendered
    assert "0 ha" not in rendered


def test_robust_failed_temporal_release_keeps_prompt_visual_theme(
    dataset_path: Path, tmp_path: Path
) -> None:
    settings = settings_for(tmp_path, full_temporal_fixture(tmp_path)).model_copy(
        update={"max_repair_attempts": 1}
    )
    result = RobustHarness(AlwaysInvalidPythonModel(), settings).run(
        "robust-failed-black-theme",
        "[TASK:mae-temporal-window-analysis-v3] Analyze the fixture. The dashboard should have a black background!",
        lambda *_args: None,
    )

    html = (tmp_path / "outputs/robust-failed-black-theme/dashboard.html").read_text()
    assert result["terminal_status"] == "failed"
    assert result["failure_reason"]
    assert "--bg: #000000;" in html
    assert "black background" in html


def test_robust_publishes_final_artifact_when_python_json_exhausts(
    dataset_path: Path, tmp_path: Path
) -> None:
    settings = settings_for(tmp_path, full_temporal_fixture(tmp_path)).model_copy(
        update={"max_repair_attempts": 1}
    )
    result = RobustHarness(ExhaustingJsonModel(), settings).run(
        "robust-final-artifact-fallback",
        "[TASK:mae-temporal-window-analysis-v3] Analyze the fixture. The dashboard should have a black background!",
        lambda *_args: None,
    )

    artifact = tmp_path / "outputs/robust-final-artifact-fallback/dashboard.html"
    assert artifact.exists()
    assert result["artifacts"]
    assert result["terminal_status"] == "failed"


def test_robust_final_product_snapshots_temporal_rows_before_reporting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dataset = full_temporal_fixture(tmp_path)
    settings = settings_for(tmp_path, dataset)

    def mutating_temporal_fallback(rows: list[dict[str, Any]]) -> str:
        snapshot = [dict(row) for row in rows]
        rows.clear()
        return "\n".join(
            [
                "## Reconciled temporal analysis",
                "",
                f"SQL and Python independently reproduced all {len(snapshot)} crop-year rows.",
            ]
        )

    monkeypatch.setattr("mae_runtime.graph.render_temporal_fallback", mutating_temporal_fallback)

    result = RobustHarness(GeneratedRepairModel(), settings).run(
        "robust-temporal-snapshot",
        "[TASK:mae-temporal-window-analysis-v3] Run the standard temporal analysis.",
        lambda *_args: None,
    )

    html = (tmp_path / "outputs/robust-temporal-snapshot/dashboard.html").read_text()
    assert result["terminal_status"] == "completed"
    assert len(result["temporal_rows"]) == 42
    assert "Prompt-Driven Temporal Analysis" in html
    assert "42 crop-year rows" in html


def test_branch_repair_context_guides_nullable_python_aggregation() -> None:
    context = branch_repair_context(
        "python",
        "def analyze(rows):\n    return []",
        [
            {
                "code": "python_execution_error",
                "message": "Restricted Python execution failed.",
                "details": {
                    "error": "TypeError: unsupported operand type(s) for +=: 'float' and 'NoneType'"
                },
            }
        ],
    )

    assert "NoneType" in context
    assert "missing numeric values" in context.lower()
    assert "guard every" in context.lower()


def test_robust_repairs_only_rejected_sql_branch_and_preserves_python(tmp_path: Path) -> None:
    dataset = full_temporal_fixture(tmp_path)
    model = GeneratedRepairModel()
    events: list[tuple[str, str]] = []
    result = RobustHarness(model, settings_for(tmp_path, dataset)).run(
        "robust-local-repair",
        "[TASK:mae-temporal-window-analysis-v3] Run the standard temporal analysis.",
        lambda node, event_type, *_args: events.append((node, event_type)),
    )

    assert model.calls["sql_agent"] == 2
    assert model.calls["python_agent"] == 1
    assert result["sql_repair_count"] == 1
    assert result["python_repair_count"] == 0
    assert result["generated_analysis"]["sql"]["status"] == "completed"
    assert result["generated_analysis"]["python"]["status"] == "completed"
    assert len(result["temporal_rows"]) == 42
    assert result["approved_evidence"] == []
    assert result["release_certificate"]["task_id"] == "mae-temporal-window-analysis-v3"
    assert result["release_certificate"]["status"] == "certified"
    assert result["release_certificate"]["approved_rows"] == 42
    assert "improved production rank" in result["narrative"]
    assert ("sql_reviewer", "branch_repair") in events
    html = (tmp_path / "outputs" / "robust-local-repair" / "dashboard.html").read_text()
    assert 'id="temporal-analysis"' in html
    assert "42 reconciled crop-year rows" in html


def test_branch_repair_context_contains_only_prior_code_and_diagnostics() -> None:
    sql = branch_repair_context(
        "sql", "SELECT 1", [{"code": "wrong_row_count", "message": "zero rows"}]
    )
    python = branch_repair_context(
        "python",
        "x = lambda value: value",
        [{"code": "unsafe_python", "message": "Lambda"}],
        repair_attempt=2,
    )

    assert "SELECT 1" in sql
    assert "zero rows" in sql
    assert "40099" not in sql
    assert "lambda" in python.lower()
    assert "must change" in python.lower()
    assert "full corrected code" in python.lower()
    assert "assumptions" in python.lower()
    assert "unchanged" in python.lower()
    assert "zero ast nodes named lambda" in python.lower()
    assert "repair attempt: 2" in python.lower()


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
    assert events.count(("final_editor", "model_retry")) == 2
    assert ("final_editor", "deterministic_fallback") in events
