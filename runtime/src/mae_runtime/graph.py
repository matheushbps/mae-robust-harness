from __future__ import annotations

import argparse
import json
import operator
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, Literal, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from .analytics import (
    profile_dataset,
    reconcile_evidence,
    run_python_analysis,
    run_sql_analysis,
    validate_dashboard,
    write_dashboard_artifact,
)
from .config import RUNTIME_ROOT, Settings
from .contracts import EvidenceItem, LLMTrace, ValidationCheck
from .model_client import ModelGateway
from .skill_library import load_skill, render_agent_skills

Emit = Callable[[str, str, str, dict[str, Any] | None], None]

GRAPH_MERMAID = """flowchart TD
    START --> business_contract --> data_profile
    data_profile --> sql_analysis
    data_profile --> python_analysis
    sql_analysis --> evidence_reconciliation
    python_analysis --> evidence_reconciliation
    evidence_reconciliation -->|pass| dashboard_build
    evidence_reconciliation -->|retry| targeted_repair
    evidence_reconciliation -->|exhausted| failed_with_evidence
    targeted_repair --> sql_analysis
    targeted_repair --> python_analysis
    dashboard_build --> visual_review --> final_editor --> END
    failed_with_evidence --> final_editor
"""


class RobustState(TypedDict, total=False):
    run_id: str
    prompt: str
    contract: dict[str, Any]
    profile: dict[str, Any]
    sql_plan: dict[str, Any]
    python_plan: dict[str, Any]
    sql_evidence: list[dict[str, Any]]
    python_evidence: list[dict[str, Any]]
    approved_evidence: list[dict[str, Any]]
    validation_checks: Annotated[list[dict[str, Any]], operator.add]
    llm_traces: Annotated[list[dict[str, Any]], operator.add]
    repair_count: int
    validation_status: str
    dashboard_path: str
    final_report: str
    terminal_status: str
    failure_reason: str


class RobustHarness:
    def __init__(self, model: ModelGateway, settings: Settings) -> None:
        self.model = model
        self.settings = settings
        config = json.loads((RUNTIME_ROOT / "config/agents.json").read_text(encoding="utf-8"))
        self.agents = {agent["id"]: agent for agent in config["agents"]}

    def run(
        self,
        run_id: str,
        prompt: str,
        emit: Emit,
        agent_prompts: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if not self.settings.dataset_path.exists():
            raise FileNotFoundError(f"Dataset not found: {self.settings.dataset_path}")
        active_agents = {
            k: {**v, "system": agent_prompts[k]} if (agent_prompts and k in agent_prompts) else dict(v)
            for k, v in self.agents.items()
        }
        runner = _GraphRun(self.model, self.settings, active_agents, run_id, emit)
        return runner.invoke(prompt)


class _GraphRun:
    def __init__(
        self,
        model: ModelGateway,
        settings: Settings,
        agents: dict[str, dict[str, Any]],
        run_id: str,
        emit: Emit,
    ) -> None:
        self.model = model
        self.settings = settings
        self.agents = agents
        self.run_id = run_id
        self.emit = emit
        self.output_dir = settings.artifacts_dir / run_id
        settings.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        self.checkpoint_connection = sqlite3.connect(str(settings.checkpoint_path), check_same_thread=False)
        self.checkpointer = SqliteSaver(self.checkpoint_connection)
        self.checkpointer.setup()
        self.graph = self._build_graph().compile(checkpointer=self.checkpointer)

    def _trace_metadata(self, trace: LLMTrace) -> dict[str, Any]:
        return trace.model_dump(mode="json", exclude={"content", "reasoning_content"})

    def _system_prompt(self, role_id: str) -> str:
        agent = self.agents[role_id]
        skill_text = render_agent_skills(agent.get("skills", []))
        return f"{agent['system']}\n\n{skill_text}" if skill_text else agent["system"]

    def _skill_names(self, role_id: str) -> list[str]:
        return list(self.agents[role_id].get("skills", []))

    def _json_call(self, role_id: str, user: str) -> tuple[dict[str, Any], dict[str, Any]]:
        last_error: Exception | None = None
        for attempt in range(self.settings.max_repair_attempts + 1):
            try:
                payload, trace = self.model.complete_json(
                    role=role_id,
                    system=self._system_prompt(role_id) + "\n\nReturn one valid JSON object and no markdown.",
                    user=user,
                )
                return payload, self._trace_metadata(trace)
            except Exception as error:  # noqa: BLE001 - errors are classified in the run trace.
                last_error = error
                self.emit(
                    role_id,
                    "model_retry",
                    f"Structured output retry {attempt + 1}/{self.settings.max_repair_attempts}.",
                    {"error": str(error)},
                )
        raise RuntimeError(f"{role_id} exhausted structured-output retries") from last_error

    def _text_call(
        self, role_id: str, user: str, max_tokens: int | None = None
    ) -> tuple[str, dict[str, Any]]:
        last_error: Exception | None = None
        for attempt in range(self.settings.max_repair_attempts + 1):
            try:
                trace = self.model.complete(
                    role_id,
                    self._system_prompt(role_id),
                    user,
                    max_tokens=max_tokens or self.settings.max_completion_tokens,
                )
                if not trace.content.strip():
                    raise ValueError("Model returned empty visible content.")
                return trace.content.strip(), self._trace_metadata(trace)
            except Exception as error:  # noqa: BLE001 - errors are classified in the run trace.
                last_error = error
                self.emit(
                    role_id,
                    "model_retry",
                    f"Visible-output retry {attempt + 1}/{self.settings.max_repair_attempts}.",
                    {"error": str(error)},
                )
        raise RuntimeError(f"{role_id} exhausted visible-output retries") from last_error

    def business_contract(self, state: RobustState) -> dict[str, Any]:
        node = "business_analyst"
        self.emit(
            node,
            "started",
            "Building the metric and acceptance contract.",
            {"skills": self._skill_names(node)},
        )
        contract, trace = self._json_call(
            node,
            "Return keys business_questions, metrics, units, acceptance_criteria, and exclusions.\n"
            f"FROZEN REQUEST:\n{state['prompt']}",
        )
        required = {"business_questions", "metrics", "units", "acceptance_criteria", "exclusions"}
        missing = sorted(required - contract.keys())
        if missing:
            raise ValueError(f"Business contract is missing keys: {missing}")
        self.emit(node, "completed", "Business contract validated.", {"keys": sorted(contract)})
        return {"contract": contract, "llm_traces": [trace]}

    def data_profile(self, _state: RobustState) -> dict[str, Any]:
        node = "data_profiler"
        load_skill("dataset-profiling")
        self.emit(
            node,
            "started",
            "Profiling dataset grain and quality.",
            {"skills": self._skill_names(node)},
        )
        profile = profile_dataset(self.settings.dataset_path)
        checks = [
            ValidationCheck(
                check_id="schema:non_empty",
                passed=profile["rows"] > 0,
                message="Dataset contains analytical rows.",
            ),
            ValidationCheck(
                check_id="schema:unique_grain",
                passed=profile["duplicate_keys"] == 0,
                message="Municipality-year-crop keys are unique.",
            ),
            ValidationCheck(
                check_id="schema:source_identity",
                passed=profile["manifest_present"] and bool(profile["dataset_sha256"]),
                message="Dataset manifest and content hash are available.",
            ),
        ]
        if not all(check.passed for check in checks):
            raise ValueError("Dataset profile failed required invariants.")
        self.emit(node, "completed", "Dataset profile passed.", {"rows": profile["rows"]})
        return {
            "profile": profile,
            "validation_checks": [check.model_dump(mode="json") for check in checks],
        }

    def sql_analysis(self, state: RobustState) -> dict[str, Any]:
        node = "sql_analyst"
        self.emit(
            node,
            "started",
            "Planning and executing read-only SQL evidence.",
            {"skills": self._skill_names(node)},
        )
        plan, trace = self._json_call(
            node,
            "Return keys selected_metrics, comparison_period, and risks. Do not return SQL.\n"
            f"CONTRACT:\n{json.dumps(state['contract'])}\n"
            f"PROFILE:\n{json.dumps(state['profile'], default=str)}",
        )
        evidence = run_sql_analysis(self.settings.dataset_path)
        self.emit(node, "completed", "SQL evidence produced.", {"items": len(evidence)})
        return {
            "sql_plan": plan,
            "sql_evidence": [item.model_dump(mode="json") for item in evidence],
            "llm_traces": [trace],
        }

    def python_analysis(self, state: RobustState) -> dict[str, Any]:
        node = "python_analyst"
        self.emit(
            node,
            "started",
            "Planning and executing independent Python evidence.",
            {"skills": self._skill_names(node)},
        )
        plan, trace = self._json_call(
            node,
            "Return keys selected_checks, comparison_period, and risks. Do not return code.\n"
            f"CONTRACT:\n{json.dumps(state['contract'])}\n"
            f"PROFILE:\n{json.dumps(state['profile'], default=str)}",
        )
        evidence = run_python_analysis(self.settings.dataset_path)
        self.emit(node, "completed", "Python evidence produced.", {"items": len(evidence)})
        return {
            "python_plan": plan,
            "python_evidence": [item.model_dump(mode="json") for item in evidence],
            "llm_traces": [trace],
        }

    def evidence_reconciliation(self, state: RobustState) -> dict[str, Any]:
        node = "evidence_reconciler"
        load_skill("cross-method-reconciliation")
        self.emit(
            node,
            "started",
            "Reconciling SQL and Python evidence.",
            {"skills": self._skill_names(node)},
        )
        sql_evidence = [EvidenceItem.model_validate(item) for item in state["sql_evidence"]]
        python_evidence = [EvidenceItem.model_validate(item) for item in state["python_evidence"]]
        checks, approved = reconcile_evidence(sql_evidence, python_evidence, self.settings.numeric_tolerance)
        passed = bool(checks) and all(check.passed for check in checks)
        repair_count = state.get("repair_count", 0)
        status = (
            "passed"
            if passed
            else ("repair" if repair_count < self.settings.max_repair_attempts else "failed")
        )
        self.emit(
            node,
            "completed" if passed else "failed",
            "Evidence agreement passed." if passed else "Evidence agreement requires repair.",
            {"approved": len(approved), "checks": len(checks), "route": status},
        )
        return {
            "approved_evidence": [item.model_dump(mode="json") for item in approved],
            "validation_checks": [check.model_dump(mode="json") for check in checks],
            "validation_status": status,
        }

    @staticmethod
    def route_after_reconciliation(
        state: RobustState,
    ) -> Literal["dashboard_build", "targeted_repair", "failed_with_evidence"]:
        return {
            "passed": "dashboard_build",
            "repair": "targeted_repair",
            "failed": "failed_with_evidence",
        }[state["validation_status"]]

    def targeted_repair(self, state: RobustState) -> dict[str, Any]:
        next_count = state.get("repair_count", 0) + 1
        self.emit(
            "targeted_repair",
            "started",
            "Re-running the failed independent evidence branches.",
            {"repair_attempt": next_count},
        )
        return {"repair_count": next_count, "sql_evidence": [], "python_evidence": []}

    def dashboard_build(self, state: RobustState) -> dict[str, Any]:
        node = "dashboard_engineer"
        load_skill("dashboard-visual-qa")
        self.emit(
            node,
            "started",
            "Designing visual layout and strategic briefing from approved evidence.",
            {"skills": self._skill_names(node)},
        )
        evidence = [EvidenceItem.model_validate(item) for item in state["approved_evidence"]]
        checks = [ValidationCheck.model_validate(item) for item in state["validation_checks"]]
        briefing, trace = self._json_call(
            node,
            "Generate visual executive briefing metadata for this agricultural dashboard. "
            "Return a JSON object with keys: title, subtitle, insights, and visual_theme.\n"
            f"EVIDENCE SAMPLE:\n{json.dumps([item.model_dump(mode='json') for item in evidence[:10]])}",
        )
        path = write_dashboard_artifact(
            self.output_dir,
            evidence,
            checks,
            dashboard_briefing=briefing,
            metadata={"harness": "Robust Harness (Condition B)", "run_id": self.run_id},
        )
        self.emit(
            node,
            "completed",
            "Dashboard artifact created.",
            {"artifact": str(path), "title": briefing.get("title")},
        )
        return {
            "dashboard_path": str(path),
            "dashboard_briefing": briefing,
            "llm_traces": [trace],
        }

    def visual_review(self, state: RobustState) -> dict[str, Any]:
        node = "visual_reviewer"
        load_skill("dashboard-visual-qa")
        self.emit(
            node,
            "started",
            "Running visual artifact contract checks.",
            {"skills": self._skill_names(node)},
        )
        checks = validate_dashboard(Path(state["dashboard_path"]))
        if not all(check.passed for check in checks):
            raise ValueError("Visual review failed.")
        self.emit(node, "completed", "Visual artifact checks passed.", {"checks": len(checks)})
        return {"validation_checks": [check.model_dump(mode="json") for check in checks]}

    def failed_with_evidence(self, state: RobustState) -> dict[str, Any]:
        reason = f"Evidence agreement failed after {state.get('repair_count', 0)} repair attempts."
        self.emit("failed_with_evidence", "failed", reason, None)
        return {"terminal_status": "failed", "failure_reason": reason}

    def final_editor(self, state: RobustState) -> dict[str, Any]:
        node = "final_editor"
        self.emit(
            node,
            "started",
            "Writing a provenance-bound final report.",
            {"skills": self._skill_names(node)},
        )
        evidence = state.get("approved_evidence", [])
        briefing = state.get("dashboard_briefing", {})
        report, trace = self._text_call(
            node,
            "Write a concise executive analysis synthesizing the 2019–2024 agricultural "
            "dynamics across all analyzed crops according to your role.\n"
            "Cite evidence IDs in brackets after every material number (e.g. [sql:40099:production_tonnes]). "
            "Organize into: 1. Executive Summary, 2. Key Crop Shifts, 3. Strategic Business Implications. "
            "If validation failed, explain the failure instead of presenting unsupported conclusions.\n"
            f"REQUEST:\n{state['prompt']}\nSTATUS:\n{state.get('terminal_status', 'completed')}\n"
            f"FAILURE:\n{state.get('failure_reason', '')}\nAPPROVED EVIDENCE:\n"
            f"{json.dumps(evidence)}",
            max_tokens=self.settings.max_completion_tokens,
        )
        terminal_status = state.get("terminal_status", "completed")
        evidence_items = [EvidenceItem.model_validate(item) for item in evidence]
        validation_items = [
            ValidationCheck.model_validate(item) for item in state.get("validation_checks", [])
        ]
        write_dashboard_artifact(
            self.output_dir,
            evidence_items,
            validation_items,
            narrative=report,
            dashboard_briefing=briefing,
            metadata={"harness": "Robust Harness (Condition B)", "run_id": self.run_id},
        )
        self.emit(node, "completed", "Final report created.", {"terminal_status": terminal_status})
        return {
            "final_report": report,
            "terminal_status": terminal_status,
            "llm_traces": [trace],
        }

    def _build_graph(self) -> StateGraph[RobustState]:
        builder = StateGraph(RobustState)
        builder.add_node("business_contract", self.business_contract)
        builder.add_node("data_profile", self.data_profile)
        builder.add_node("sql_analysis", self.sql_analysis)
        builder.add_node("python_analysis", self.python_analysis)
        builder.add_node("evidence_reconciliation", self.evidence_reconciliation)
        builder.add_node("targeted_repair", self.targeted_repair)
        builder.add_node("dashboard_build", self.dashboard_build)
        builder.add_node("visual_review", self.visual_review)
        builder.add_node("failed_with_evidence", self.failed_with_evidence)
        builder.add_node("final_editor", self.final_editor)
        builder.add_edge(START, "business_contract")
        builder.add_edge("business_contract", "data_profile")
        builder.add_edge("data_profile", "sql_analysis")
        builder.add_edge("data_profile", "python_analysis")
        # A list edge is an explicit fan-in barrier: reconciliation cannot run
        # until both independent branches have completed.
        builder.add_edge(["sql_analysis", "python_analysis"], "evidence_reconciliation")
        builder.add_conditional_edges(
            "evidence_reconciliation",
            self.route_after_reconciliation,
            {
                "dashboard_build": "dashboard_build",
                "targeted_repair": "targeted_repair",
                "failed_with_evidence": "failed_with_evidence",
            },
        )
        builder.add_edge("targeted_repair", "sql_analysis")
        builder.add_edge("targeted_repair", "python_analysis")
        builder.add_edge("dashboard_build", "visual_review")
        builder.add_edge("visual_review", "final_editor")
        builder.add_edge("failed_with_evidence", "final_editor")
        builder.add_edge("final_editor", END)
        return builder

    def invoke(self, prompt: str) -> dict[str, Any]:
        initial: RobustState = {
            "run_id": self.run_id,
            "prompt": prompt,
            "repair_count": 0,
            "validation_checks": [],
            "llm_traces": [],
        }
        try:
            final_state = self.graph.invoke(
                initial,
                config={
                    "configurable": {"thread_id": self.run_id},
                    "recursion_limit": 30,
                },
            )
        finally:
            self.checkpoint_connection.close()
        return {
            "harness": "robust",
            "contract": final_state.get("contract", {}),
            "profile": final_state.get("profile", {}),
            "approved_evidence": final_state.get("approved_evidence", []),
            "validation": final_state.get("validation_checks", []),
            "artifacts": [final_state["dashboard_path"]] if final_state.get("dashboard_path") else [],
            "narrative": final_state.get("final_report", ""),
            "terminal_status": final_state.get("terminal_status", "failed"),
            "failure_reason": final_state.get("failure_reason"),
            "repair_count": final_state.get("repair_count", 0),
            "model_usage": summarize_usage(final_state.get("llm_traces", [])),
            "checkpoint_thread_id": self.run_id,
            "skills_used": sorted(
                {skill for agent in self.agents.values() for skill in agent.get("skills", [])}
            ),
        }


def summarize_usage(traces: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "calls": len(traces),
        "prompt_tokens": sum(int(trace.get("prompt_tokens", 0)) for trace in traces),
        "completion_tokens": sum(int(trace.get("completion_tokens", 0)) for trace in traces),
        "reasoning_tokens": sum(int(trace.get("reasoning_tokens", 0)) for trace in traces),
        "latency_seconds": round(sum(float(trace.get("latency_seconds", 0.0)) for trace in traces), 4),
        "traces": traces,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect the MAE robust LangGraph definition.")
    parser.add_argument("--describe", action="store_true")
    arguments = parser.parse_args()
    if arguments.describe:
        print(GRAPH_MERMAID)
    else:
        parser.error("Use --describe to print the frozen graph.")


if __name__ == "__main__":
    main()
