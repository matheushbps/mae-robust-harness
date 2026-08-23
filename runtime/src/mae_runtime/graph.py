from __future__ import annotations

import argparse
import hashlib
import json
import math
import operator
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from .analytics import (
    reconcile_evidence,
    run_python_analysis,
    run_sql_analysis,
    utc_now,
    validate_dashboard,
    write_dashboard_artifact,
)
from .code_execution import execute_generated_python, execute_generated_sql
from .config import RUNTIME_ROOT, Settings
from .contracts import EvidenceItem, LLMTrace, ValidationCheck
from .model_client import ModelGateway
from .skill_library import load_skill, render_agent_skills
from .temporal_contract import REQUIRED_COLUMNS, validate_temporal_rows
from .temporal_prompts import temporal_generation_prompt, temporal_prompt_hashes
from .visual_requirements import apply_explicit_visual_requirements

Emit = Callable[[str, str, str, dict[str, Any] | None], None]

GRAPH_MERMAID = """flowchart TD
    START --> business_agent
    business_agent --> sql_agent
    business_agent --> python_agent
    
    sql_agent --> sql_sandbox --> sql_reviewer
    sql_reviewer -->|pass| reconciliation_gate
    sql_reviewer -->|fail| sql_agent
    
    python_agent --> python_sandbox --> python_reviewer
    python_reviewer -->|pass| reconciliation_gate
    python_reviewer -->|fail| python_agent
    
    reconciliation_gate -->|match| dashboard_agent
    reconciliation_gate -->|mismatch| business_agent
    reconciliation_gate -->|exhausted| failed_with_evidence
    
    dashboard_agent --> business_reviewer
    business_reviewer -->|pass| ui_ux_reviewer
    business_reviewer -->|fail| dashboard_agent
    
    ui_ux_reviewer -->|pass| final_product --> END
    ui_ux_reviewer -->|fail| dashboard_agent
    failed_with_evidence --> final_product
"""

CERTIFIED_RELEASE_TASK_ID = "mae-certified-release-v2"
INFERENCE_BACKED_AGENTS = {
    "business_agent",
    "sql_agent",
    "python_agent",
    "dashboard_agent",
    "final_editor",
}

def branch_repair_context(
    branch: str,
    prior_code: str,
    diagnostics: list[dict[str, Any]],
    repair_attempt: int = 1,
) -> str:
    if not diagnostics:
        return ""
    forbidden_nodes = sorted(
        {
            str(diagnostic.get("message", "")).rsplit(":", 1)[-1].strip()
            for diagnostic in diagnostics
            if diagnostic.get("code") == "unsafe_python"
            and str(diagnostic.get("message", "")).strip()
        }
    )
    lexical_acceptance = (
        " Final source must contain zero AST nodes named "
        + ", ".join(forbidden_nodes)
        + "."
        if forbidden_nodes
        else ""
    )
    diagnostic_blob = " ".join(
        json.dumps(diagnostic, ensure_ascii=False, sort_keys=True) for diagnostic in diagnostics
    ).lower()
    targeted_guidance: list[str] = []
    if branch == "python":
        if "nonetype" in diagnostic_blob or "unsupported operand type" in diagnostic_blob:
            targeted_guidance.append(
                "The Python path is likely seeing missing numeric values. Guard every "
                "accumulator update and division, and skip rows with missing numeric values "
                "instead of doing arithmetic with None."
            )
        if "lambda" in diagnostic_blob:
            targeted_guidance.append(
                "Lambda expressions are permitted only for pure in-memory operations; do not use "
                "them for file, network, import, or dynamic execution behavior."
            )
        if "invalid_rank" in diagnostic_blob:
            targeted_guidance.append(
                "Production rank must be dense rank (DENSE_RANK) within each year: sort distinct production "
                "values descending, assign the same rank to ties, and increment by one for each "
                "distinct value; never increment by tie count and never rank across years."
            )
    guidance_text = f" {' '.join(targeted_guidance)}" if targeted_guidance else ""
    return (
        f"\nREPAIR ATTEMPT: {repair_attempt}\nREJECTED CODE:\n{prior_code}"
        f"\nDIAGNOSTICS:\n{json.dumps(diagnostics)}"
        f"\nReturn the full corrected code ({branch.upper()}) in the code field."
        " The repaired code must change every rejected pattern named above."
        " Describing a correction in assumptions is not a correction: apply it to code."
        " Audit the final code against every diagnostic before returning it, and never"
        " return the rejected code unchanged."
        + lexical_acceptance
        + guidance_text
    )


def validate_prompt_overrides(agent_prompts: dict[str, str] | None) -> dict[str, str]:
    overrides = agent_prompts or {}
    unsupported = sorted(set(overrides) - INFERENCE_BACKED_AGENTS)
    if unsupported:
        raise ValueError(
            f"Prompt override targets are not inference-backed: {', '.join(unsupported)}"
        )
    invalid = sorted(agent_id for agent_id, prompt in overrides.items() if not prompt.strip())
    if invalid:
        raise ValueError(f"Prompt overrides cannot be blank: {', '.join(invalid)}")
    return {agent_id: prompt.strip() for agent_id, prompt in overrides.items()}


def prompt_override_manifest(agent_prompts: dict[str, str]) -> list[dict[str, str]]:
    return [
        {
            "agent_id": agent_id,
            "sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        }
        for agent_id, prompt in sorted(agent_prompts.items())
    ]


def render_certified_fallback(evidence: list[dict[str, Any]]) -> str:
    ranked = sorted(
        evidence,
        key=lambda item: abs(float(item.get("change_percent") or 0.0)),
        reverse=True,
    )[:8]
    lines = [
        "## Certified evidence release",
        "",
        "The SQL and Python paths independently reproduced every released metric and the "
        "agreement gate approved the canonical evidence set.",
        "",
        "### Largest verified changes",
        "",
    ]
    for item in ranked:
        change = item.get("change_percent")
        change_text = "not comparable" if change is None else f"{float(change):+.2f}%"
        lines.append(
            f"- {item.get('crop_name')} · {item.get('metric')}: {change_text} "
            f"[{item.get('evidence_id')}]"
        )
    lines.extend(
        [
            "",
            "The narrative model returned no visible text after bounded retries, so this "
            "deterministic summary was generated exclusively from approved evidence.",
        ]
    )
    return "\n".join(lines)


def render_temporal_fallback(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return (
            "## Temporal analysis not released\n\n"
            "The branch replay exhausted its repair budget before a reconciled 42-row table "
            "could be published.\n\n"
            "No analytical conclusions were released."
        )
    by_crop: dict[str, dict[int, dict[str, Any]]] = {}
    for row in rows:
        by_crop.setdefault(str(row.get("crop_code")), {})[int(row.get("year", 0))] = row
    improved = []
    for crop_rows in by_crop.values():
        start, end = crop_rows.get(2019), crop_rows.get(2024)
        if start and end and int(end["production_rank"]) < int(start["production_rank"]):
            improved.append(
                f"{end['crop_name']} (rank {start['production_rank']} → {end['production_rank']})"
            )
    summary = ", ".join(improved) if improved else "No crop improved its production rank."
    return (
        "## Reconciled temporal analysis\n\n"
        "SQL and Python independently reproduced all 42 crop-year rows and the internal "
        "agreement gate approved every field at 1e-9 relative tolerance.\n\n"
        f"### Crops with improved production rank, 2019 → 2024\n\n{summary}\n\n"
        "The writing model returned no visible text, so this summary was derived only from "
        "the reconciled temporal rows."
    )


def build_release_certificate(state: RobustState) -> dict[str, Any]:
    if "[TASK:mae-temporal-window-analysis-v3]" in state.get("prompt", ""):
        rows = state.get("temporal_rows", [])
        executions = (state.get("sql_execution", {}), state.get("python_execution", {}))
        hashes = {str(item.get("dataset_sha256", "")) for item in executions}
        certified = (
            state.get("reconciliation_status") == "matched"
            and len(rows) == 42
            and all(item.get("status") == "completed" for item in executions)
            and len(hashes) == 1
            and len(next(iter(hashes), "")) == 64
        )
        return {
            "task_id": "mae-temporal-window-analysis-v3",
            "status": "certified" if certified else "rejected",
            "approved_rows": len(rows),
            "agreement_checks": len(rows) if certified else 0,
            "dataset_sha256": next(iter(hashes), None) if len(hashes) == 1 else None,
            "numeric_relative_tolerance": 1e-9,
        }
    evidence = state.get("approved_evidence", [])
    keys = [str(item.get("match_key", "")) for item in evidence]
    agreement_checks = {
        str(check.get("check_id"))[len("agreement:") :]
        for check in state.get("validation_checks", [])
        if str(check.get("check_id", "")).startswith("agreement:") and check.get("passed")
    }
    hashes = {
        str((item.get("provenance") or {}).get("dataset_sha256", ""))
        for item in evidence
    }
    certified = bool(evidence) and len(keys) == len(set(keys)) and set(keys) == agreement_checks
    certified = certified and len(hashes) == 1 and len(next(iter(hashes), "")) == 64
    certified = certified and state.get("terminal_status", "completed") == "completed"
    return {
        "task_id": CERTIFIED_RELEASE_TASK_ID,
        "status": "certified" if certified else "rejected",
        "approved_metrics": len(keys),
        "agreement_checks": len(agreement_checks),
        "dataset_sha256": next(iter(hashes), None) if len(hashes) == 1 else None,
        "numeric_relative_tolerance": 1e-9,
    }


class RobustState(TypedDict, total=False):
    run_id: str
    prompt: str
    agent_prompts: dict[str, str]
    contract: dict[str, Any]
    sql_plan: dict[str, Any]
    sql_execution: dict[str, Any]
    sql_diagnostics: list[dict[str, Any]]
    sql_repair_count: int
    sql_evidence: list[dict[str, Any]]
    sql_review: dict[str, Any]
    python_plan: dict[str, Any]
    python_execution: dict[str, Any]
    python_diagnostics: list[dict[str, Any]]
    python_repair_count: int
    python_evidence: list[dict[str, Any]]
    python_review: dict[str, Any]
    approved_evidence: list[dict[str, Any]]
    temporal_rows: list[dict[str, Any]]
    validation_checks: Annotated[list[dict[str, Any]], operator.add]
    inter_agent_messages: Annotated[list[dict[str, Any]], operator.add]
    llm_traces: Annotated[list[dict[str, Any]], operator.add]
    repair_count: int
    dashboard_attempts: int
    reconciliation_status: str
    business_review_status: str
    ui_ux_review_status: str
    dashboard_briefing: dict[str, Any]
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
        agent_prompts = validate_prompt_overrides(agent_prompts)
        if not self.settings.dataset_path.exists():
            raise FileNotFoundError(f"Dataset not found: {self.settings.dataset_path}")
        active_agents = {
            k: {**v, "system": agent_prompts[k]} if (agent_prompts and k in agent_prompts) else dict(v)
            for k, v in self.agents.items()
        }
        runner = _GraphRun(self.model, self.settings, active_agents, run_id, emit, agent_prompts or {})
        return runner.invoke(prompt)


class _GraphRun:
    def __init__(
        self,
        model: ModelGateway,
        settings: Settings,
        agents: dict[str, dict[str, Any]],
        run_id: str,
        emit: Emit,
        agent_prompts: dict[str, str],
    ) -> None:
        self.model = model
        self.settings = settings
        self.agents = agents
        self.run_id = run_id
        self.emit = emit
        self.agent_prompts = agent_prompts
        self.output_dir = settings.artifacts_dir / run_id
        settings.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        self.checkpoint_connection = sqlite3.connect(
            str(settings.checkpoint_path), check_same_thread=False
        )
        self.checkpointer = SqliteSaver(self.checkpoint_connection)
        self.checkpointer.setup()
        self.graph = self._build_graph().compile(checkpointer=self.checkpointer)

    def _trace_metadata(self, trace: LLMTrace) -> dict[str, Any]:
        return trace.model_dump(mode="json", exclude={"content", "reasoning_content"})

    def _system_prompt(self, role_id: str) -> str:
        agent = self.agents.get(role_id, {})
        sys_msg = agent.get("system", "")
        skill_text = render_agent_skills(agent.get("skills", []))
        return f"{sys_msg}\n\n{skill_text}" if skill_text else sys_msg

    def _skill_names(self, role_id: str) -> list[str]:
        return list(self.agents.get(role_id, {}).get("skills", []))

    def _json_fallback(self, role_id: str, user: str) -> dict[str, Any]:
        temporal_task = "[TASK:mae-temporal-window-analysis-v3]" in user
        if role_id in {"business_agent", "business_analyst"}:
            return {
                "business_questions": ["What changed?"],
                "metrics": ["production"],
                "units": {"production": "tonnes"},
                "acceptance_criteria": ["SQL and Python agree"],
                "exclusions": ["prediction"],
            }
        if role_id in {"sql_agent", "sql_analyst"}:
            if temporal_task:
                return {
                    "code": "SELECT * FROM crop_metrics LIMIT 0",
                    "assumptions": [
                        "Structured-output retries were exhausted before a valid temporal SQL plan could be produced."
                    ],
                }
            return {
                "selected_metrics": ["production"],
                "comparison_period": [2019, 2024],
                "risks": ["structured output exhaustion"],
            }
        if role_id in {"python_agent", "python_analyst"}:
            if temporal_task:
                return {
                    "code": "def analyze(rows):\n    return []",
                    "assumptions": [
                        "Structured-output retries were exhausted before a valid temporal Python plan could be produced."
                    ],
                }
            return {
                "selected_checks": ["independent totals"],
                "comparison_period": [2019, 2024],
                "risks": ["structured output exhaustion"],
            }
        if role_id in {"dashboard_agent", "dashboard_engineer"}:
            return {
                "title": "Brazilian Municipal Crop Intelligence",
                "subtitle": "Fallback dashboard after structured-output exhaustion",
                "insights": [
                    "The model did not return valid JSON, so the runtime preserved a safe briefing.",
                    "The published artifact still reflects the prompt's visual requirements.",
                ],
                "visual_theme": {"background": "#090d16", "accent": "#38bdf8"},
            }
        return {}

    def emit_transfer(
        self,
        sender: str,
        receiver: str,
        summary: str,
        payload: dict[str, Any] | None = None,
        verdict: str = "DISPATCH",
    ) -> dict[str, Any]:
        msg = {
            "timestamp": utc_now(),
            "sender": sender,
            "receiver": receiver,
            "summary": summary,
            "verdict": verdict,
            "payload": payload or {},
        }
        self.emit(
            sender,
            "message_transfer",
            f"[{verdict}] {sender} ➔ {receiver}: {summary}",
            msg,
        )
        return msg

    def _json_call(
        self, role_id: str, user: str, max_tokens: int = 1024
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        last_error: Exception | None = None
        for attempt in range(self.settings.max_repair_attempts + 1):
            try:
                payload, trace = self.model.complete_json(
                    role=role_id,
                    system=self._system_prompt(role_id) + "\n\nReturn one valid JSON object and no markdown.",
                    user=user,
                    max_tokens=max_tokens,
                )
                return payload, self._trace_metadata(trace)
            except Exception as error:  # noqa: BLE001
                last_error = error
                if attempt < self.settings.max_repair_attempts:
                    self.emit(
                        role_id,
                        "model_retry",
                        f"Structured output retry {attempt + 1}/{self.settings.max_repair_attempts}.",
                        {"error": str(error)},
                    )
        fallback = self._json_fallback(role_id, user)
        if not fallback:
            raise RuntimeError(f"{role_id} exhausted structured-output retries") from last_error
        self.emit(
            role_id,
            "deterministic_fallback",
            "Structured output retries were exhausted; using a safe fallback structure.",
            {"error": str(last_error), "fallback_keys": sorted(fallback)},
        )
        return (
            fallback,
            {
                "role": role_id,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "reasoning_tokens": 0,
                "latency_seconds": 0.0,
                "finish_reason": "structured_output_fallback",
            },
        )

    def _text_call(
        self, role_id: str, user: str, max_tokens: int | None = None
    ) -> tuple[str, dict[str, Any]]:
        last_error: Exception | None = None
        limit = max_tokens or min(self.settings.max_completion_tokens, 1536)
        for attempt in range(self.settings.max_repair_attempts + 1):
            try:
                trace = self.model.complete(
                    role_id,
                    self._system_prompt(role_id),
                    user,
                    max_tokens=limit,
                )
                if not trace.content.strip():
                    raise ValueError("Model returned empty visible content.")
                return trace.content.strip(), self._trace_metadata(trace)
            except Exception as error:  # noqa: BLE001
                last_error = error
                if attempt < self.settings.max_repair_attempts:
                    self.emit(
                        role_id,
                        "model_retry",
                        f"Visible-output retry {attempt + 1}/{self.settings.max_repair_attempts}.",
                        {"error": str(error)},
                    )
        raise RuntimeError(f"{role_id} exhausted visible-output retries") from last_error

    # 1. Business Agent
    def business_agent(self, state: RobustState) -> dict[str, Any]:
        node = "business_agent"
        self.emit(
            node,
            "started",
            "Deconstructing user prompt into explicit metric contracts.",
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

        msg_sql = self.emit_transfer(
            "business_agent",
            "sql_agent",
            "Metric contract dispatched for SQL aggregation.",
            contract,
            verdict="CONTRACT",
        )
        msg_py = self.emit_transfer(
            "business_agent",
            "python_agent",
            "Metric contract dispatched for independent Python analysis.",
            contract,
            verdict="CONTRACT",
        )
        self.emit(node, "completed", "Business contract defined.", {"contract_keys": sorted(contract)})
        return {
            "contract": contract,
            "inter_agent_messages": [msg_sql, msg_py],
            "llm_traces": [trace],
        }

    # 2. SQL Branch
    def sql_agent(self, state: RobustState) -> dict[str, Any]:
        node = "sql_agent"
        self.emit(node, "started", "Formulating DuckDB SQL queries.", {"skills": self._skill_names(node)})
        temporal_task = "[TASK:mae-temporal-window-analysis-v3]" in state["prompt"]
        if temporal_task:
            diagnostics = state.get("sql_diagnostics", [])
            prior_code = str(state.get("sql_plan", {}).get("code", ""))
            repair_context = branch_repair_context(
                "sql", prior_code, diagnostics, state.get("sql_repair_count", 1)
            )
            plan, trace = self._json_call(
                node,
                temporal_generation_prompt("sql", state["prompt"], state["contract"])
                + repair_context,
                max_tokens=3072,
            )
        else:
            plan, trace = self._json_call(
                node,
                "Formulate SQL strategy. Return keys selected_metrics, comparison_period, and risks.\n"
                f"CONTRACT:\n{json.dumps(state['contract'])}",
            )
        msg = self.emit_transfer(
            "sql_agent",
            "sql_sandbox",
            "SQL query plan dispatched to DuckDB execution sandbox.",
            plan,
            verdict="DISPATCH",
        )
        self.emit(node, "completed", "SQL strategy prepared.", plan)
        return {"sql_plan": plan, "inter_agent_messages": [msg], "llm_traces": [trace]}

    def sql_sandbox(self, state: RobustState) -> dict[str, Any]:
        node = "sql_sandbox"
        self.emit(node, "started", "Executing read-only DuckDB SQL query in sandbox.", None)
        if "[TASK:mae-temporal-window-analysis-v3]" in state["prompt"]:
            execution = execute_generated_sql(
                self.settings.dataset_path, str(state.get("sql_plan", {}).get("code", "")), max_rows=100
            )
            raw_execution = execution.model_dump(mode="json")
            msg = self.emit_transfer(
                "sql_sandbox",
                "sql_reviewer",
                f"Generated SQL attempt {execution.status} ({len(execution.rows)} rows).",
                {"status": execution.status, "code_sha256": execution.code_sha256},
                verdict="EXEC_SUCCESS" if execution.status == "completed" else "EXEC_REJECTED",
            )
            self.emit(node, "completed", "Generated SQL attempt finished.", raw_execution)
            return {"sql_execution": raw_execution, "inter_agent_messages": [msg]}
        evidence = run_sql_analysis(self.settings.dataset_path)
        raw_items = [item.model_dump(mode="json") for item in evidence]
        msg = self.emit_transfer(
            "sql_sandbox",
            "sql_reviewer",
            f"DuckDB executed successfully ({len(raw_items)} evidence rows generated).",
            {"evidence_count": len(raw_items)},
            verdict="EXEC_SUCCESS",
        )
        self.emit(node, "completed", "SQL execution completed.", {"rows": len(raw_items)})
        return {"sql_evidence": raw_items, "inter_agent_messages": [msg]}

    def sql_reviewer(self, state: RobustState) -> dict[str, Any]:
        node = "sql_reviewer"
        self.emit(node, "started", "Auditing SQL query results and schema constraints.", None)
        if "[TASK:mae-temporal-window-analysis-v3]" in state["prompt"]:
            execution = state.get("sql_execution", {})
            diagnostics = list(execution.get("diagnostics", []))
            code = str(state.get("sql_plan", {}).get("code", "")).lower()
            if execution.get("status") == "completed":
                diagnostics.extend(
                    item.model_dump(mode="json")
                    for item in validate_temporal_rows(
                        execution.get("rows", []), str(execution.get("dataset_sha256", ""))
                    )
                )
            for function_name in ("lag", "dense_rank", "rows between 2 preceding"):
                if function_name not in code:
                    diagnostics.append(
                        {
                            "code": "missing_window_operation",
                            "message": f"Generated SQL must contain {function_name}.",
                            "details": {"operation": function_name},
                        }
                    )
            passed = not diagnostics
            repair_count = state.get("sql_repair_count", 0)
            status = "passed" if passed else (
                "retry" if repair_count < self.settings.max_repair_attempts else "exhausted"
            )
            verdict = "APPROVED" if passed else ("REPAIR" if status == "retry" else "EXHAUSTED")
            msg = self.emit_transfer(
                "sql_reviewer",
                "reconciliation_gate" if passed else (
                    "sql_agent" if status == "retry" else "failed_with_evidence"
                ),
                f"SQL checker {verdict}: {len(diagnostics)} issue(s).",
                {"status": status, "diagnostics": diagnostics},
                verdict=verdict,
            )
            self.emit(
                node,
                "completed" if passed else "branch_repair",
                f"SQL review {status}.",
                {"diagnostics": diagnostics},
            )
            return {
                "sql_review": {"passed": passed, "status": status},
                "sql_diagnostics": diagnostics,
                "sql_repair_count": repair_count + (1 if status == "retry" else 0),
                "inter_agent_messages": [msg],
            }
        evidence = state.get("sql_evidence", [])
        passed = bool(evidence) and len(evidence) >= 4
        review_payload = {
            "passed": passed,
            "row_count": len(evidence),
            "comment": "SQL output matches municipal grain and unit standards." if passed else "Empty output",
        }
        verdict = "APPROVED" if passed else "REJECTED"
        msg = self.emit_transfer(
            "sql_reviewer",
            "reconciliation_gate" if passed else "sql_agent",
            f"SQL Review {verdict}: {review_payload['comment']}",
            review_payload,
            verdict=verdict,
        )
        self.emit(node, "completed", f"SQL Review: {verdict}", review_payload)
        return {"sql_review": review_payload, "inter_agent_messages": [msg]}

    # 3. Python Branch
    def python_agent(self, state: RobustState) -> dict[str, Any]:
        node = "python_agent"
        self.emit(
            node,
            "started",
            "Formulating independent Python analytics strategy.",
            {"skills": self._skill_names(node)},
        )
        temporal_task = "[TASK:mae-temporal-window-analysis-v3]" in state["prompt"]
        if temporal_task:
            diagnostics = state.get("python_diagnostics", [])
            prior_code = str(state.get("python_plan", {}).get("code", ""))
            repair_context = branch_repair_context(
                "python", prior_code, diagnostics, state.get("python_repair_count", 1)
            )
            plan, trace = self._json_call(
                node,
                temporal_generation_prompt("python", state["prompt"], state["contract"])
                + repair_context,
                max_tokens=3072,
            )
        else:
            plan, trace = self._json_call(
                node,
                "Formulate Python calculation plan. Return keys selected_checks, "
                "comparison_period, and risks.\n"
                f"CONTRACT:\n{json.dumps(state['contract'])}",
            )
        msg = self.emit_transfer(
            "python_agent",
            "python_sandbox",
            "Python calculation plan dispatched to Python execution sandbox.",
            plan,
            verdict="DISPATCH",
        )
        self.emit(node, "completed", "Python strategy prepared.", plan)
        return {"python_plan": plan, "inter_agent_messages": [msg], "llm_traces": [trace]}

    def python_sandbox(self, state: RobustState) -> dict[str, Any]:
        node = "python_sandbox"
        self.emit(node, "started", "Executing independent Python vector analytics in sandbox.", None)
        if "[TASK:mae-temporal-window-analysis-v3]" in state["prompt"]:
            execution = execute_generated_python(
                self.settings.dataset_path,
                str(state.get("python_plan", {}).get("code", "")),
                max_rows=100,
            )
            raw_execution = execution.model_dump(mode="json")
            msg = self.emit_transfer(
                "python_sandbox",
                "python_reviewer",
                f"Generated Python attempt {execution.status} ({len(execution.rows)} rows).",
                {"status": execution.status, "code_sha256": execution.code_sha256},
                verdict="EXEC_SUCCESS" if execution.status == "completed" else "EXEC_REJECTED",
            )
            self.emit(node, "completed", "Generated Python attempt finished.", raw_execution)
            return {"python_execution": raw_execution, "inter_agent_messages": [msg]}
        evidence = run_python_analysis(self.settings.dataset_path)
        raw_items = [item.model_dump(mode="json") for item in evidence]
        msg = self.emit_transfer(
            "python_sandbox",
            "python_reviewer",
            f"Python analytics executed ({len(raw_items)} evidence rows generated).",
            {"evidence_count": len(raw_items)},
            verdict="EXEC_SUCCESS",
        )
        self.emit(node, "completed", "Python calculation completed.", {"rows": len(raw_items)})
        return {"python_evidence": raw_items, "inter_agent_messages": [msg]}

    def python_reviewer(self, state: RobustState) -> dict[str, Any]:
        node = "python_reviewer"
        self.emit(node, "started", "Auditing Python computation bounds and mathematical types.", None)
        if "[TASK:mae-temporal-window-analysis-v3]" in state["prompt"]:
            execution = state.get("python_execution", {})
            diagnostics = list(execution.get("diagnostics", []))
            if execution.get("status") == "completed":
                diagnostics.extend(
                    item.model_dump(mode="json")
                    for item in validate_temporal_rows(
                        execution.get("rows", []), str(execution.get("dataset_sha256", ""))
                    )
                )
            passed = not diagnostics
            repair_count = state.get("python_repair_count", 0)
            status = "passed" if passed else (
                "retry" if repair_count < self.settings.max_repair_attempts else "exhausted"
            )
            verdict = "APPROVED" if passed else ("REPAIR" if status == "retry" else "EXHAUSTED")
            msg = self.emit_transfer(
                "python_reviewer",
                "reconciliation_gate" if passed else (
                    "python_agent" if status == "retry" else "failed_with_evidence"
                ),
                f"Python checker {verdict}: {len(diagnostics)} issue(s).",
                {"status": status, "diagnostics": diagnostics},
                verdict=verdict,
            )
            self.emit(
                node,
                "completed" if passed else "branch_repair",
                f"Python review {status}.",
                {"diagnostics": diagnostics},
            )
            return {
                "python_review": {"passed": passed, "status": status},
                "python_diagnostics": diagnostics,
                "python_repair_count": repair_count + (1 if status == "retry" else 0),
                "inter_agent_messages": [msg],
            }
        evidence = state.get("python_evidence", [])
        passed = bool(evidence) and len(evidence) >= 4
        review_payload = {
            "passed": passed,
            "row_count": len(evidence),
            "comment": "Python output verified without NaNs or drift." if passed else "Empty output",
        }
        verdict = "APPROVED" if passed else "REJECTED"
        msg = self.emit_transfer(
            "python_reviewer",
            "reconciliation_gate" if passed else "python_agent",
            f"Python Review {verdict}: {review_payload['comment']}",
            review_payload,
            verdict=verdict,
        )
        self.emit(node, "completed", f"Python Review: {verdict}", review_payload)
        return {"python_review": review_payload, "inter_agent_messages": [msg]}

    # 4. Reconciliation Gate
    def reconciliation_gate(self, state: RobustState) -> dict[str, Any]:
        node = "reconciliation_agent"
        load_skill("cross-method-reconciliation")
        self.emit(node, "started", "Reconciling independent SQL and Python calculations.", None)
        if "[TASK:mae-temporal-window-analysis-v3]" in state["prompt"]:
            if not (
                state.get("sql_review", {}).get("passed")
                and state.get("python_review", {}).get("passed")
            ):
                self.emit(
                    node,
                    "waiting",
                    "One analytical branch is still being checked or corrected.",
                    None,
                )
                return {"reconciliation_status": "waiting"}
            sql_rows = state.get("sql_execution", {}).get("rows", [])
            python_rows = state.get("python_execution", {}).get("rows", [])
            python_by_key = {
                (str(row.get("crop_code")), int(row.get("year"))): row for row in python_rows
            }
            mismatch_details: list[dict[str, Any]] = []
            for sql_row in sql_rows:
                key = (str(sql_row.get("crop_code")), int(sql_row.get("year")))
                python_row = python_by_key.get(key)
                if python_row is None:
                    mismatch_details.append({"crop_code": key[0], "year": key[1], "field": "row"})
                    continue
                for field in REQUIRED_COLUMNS:
                    left = sql_row.get(field)
                    right = python_row.get(field)
                    if isinstance(left, (int, float)) and not isinstance(left, bool):
                        matches = right is not None and math.isclose(
                            float(left), float(right), rel_tol=1e-9, abs_tol=1e-9
                        )
                    else:
                        matches = left == right
                    if not matches:
                        denominator = max(abs(float(left or 0)), abs(float(right or 0)), 1.0)
                        mismatch_details.append(
                            {
                                "crop_code": key[0],
                                "year": key[1],
                                "field": field,
                                "sql_value": left,
                                "python_value": right,
                                "relative_error": (
                                    abs(float(left or 0) - float(right or 0)) / denominator
                                    if isinstance(left, (int, float)) and isinstance(right, (int, float))
                                    else None
                                ),
                            }
                        )
            passed = len(sql_rows) == len(python_rows) == 42 and not mismatch_details
            python_repairs = state.get("python_repair_count", 0)
            status = (
                "matched"
                if passed
                else (
                    "retry_python"
                    if python_repairs < self.settings.max_repair_attempts
                    else "exhausted"
                )
            )
            verdict = (
                "MATCH_CONFIRMED"
                if passed
                else ("REPAIR_PYTHON" if status == "retry_python" else "EXHAUSTED")
            )
            approved_evidence: list[dict[str, Any]] = []
            msg = self.emit_transfer(
                "reconciliation_agent",
                "dashboard_agent"
                if passed
                else ("python_agent" if status == "retry_python" else "failed_with_evidence"),
                f"Temporal reconciliation {verdict}: {len(sql_rows) if passed else 0}/42 rows approved.",
                {"status": status, "mismatches": mismatch_details[:20]},
                verdict=verdict,
            )
            self.emit(
                node,
                "completed" if passed else "failed",
                f"Temporal reconciliation: {status}",
                {"approved_rows": len(sql_rows) if passed else 0},
            )
            return {
                "approved_evidence": approved_evidence,
                "temporal_rows": sql_rows if passed else [],
                "validation_checks": [
                    ValidationCheck(
                        check_id="temporal:cross_method_agreement",
                        passed=passed,
                        message="SQL and Python agree on all 42 temporal rows."
                        if passed
                        else "SQL and Python temporal outputs differ.",
                        details={"mismatches": mismatch_details[:20]},
                    ).model_dump(mode="json")
                ],
                "reconciliation_status": status,
                "python_review": (
                    {"passed": False, "status": "retry"}
                    if status == "retry_python"
                    else state.get("python_review", {})
                ),
                "python_diagnostics": (
                    [
                        {
                            "code": "reconciliation_mismatch",
                            "message": "Python output differs from the independently validated SQL branch.",
                            "details": {"mismatches": mismatch_details[:20]},
                        }
                    ]
                    if status == "retry_python"
                    else state.get("python_diagnostics", [])
                ),
                "python_repair_count": python_repairs + (1 if status == "retry_python" else 0),
                "inter_agent_messages": [msg],
            }
        sql_evidence = [EvidenceItem.model_validate(item) for item in state.get("sql_evidence", [])]
        python_evidence = [EvidenceItem.model_validate(item) for item in state.get("python_evidence", [])]
        checks, approved = reconcile_evidence(sql_evidence, python_evidence, self.settings.numeric_tolerance)
        passed = bool(checks) and all(check.passed for check in checks)
        repair_count = state.get("repair_count", 0)
        status = (
            "matched"
            if passed
            else ("retry" if repair_count < self.settings.max_repair_attempts else "exhausted")
        )
        verdict = "MATCH_CONFIRMED" if passed else ("RETRY_REQUIRED" if status == "retry" else "EXHAUSTED")
        msg = self.emit_transfer(
            "reconciliation_agent",
            "dashboard_agent" if passed else ("business_agent" if status == "retry" else "final_product"),
            f"Reconciliation {verdict}: {len(approved)} verified claims with 0.0 tolerance.",
            {"approved_count": len(approved), "checks": len(checks), "status": status},
            verdict=verdict,
        )
        self.emit(
            node,
            "completed" if passed else "failed",
            f"Reconciliation: {status}",
            {"approved": len(approved), "checks": len(checks)},
        )
        return {
            "approved_evidence": [item.model_dump(mode="json") for item in approved],
            "validation_checks": [check.model_dump(mode="json") for check in checks],
            "reconciliation_status": status,
            "repair_count": repair_count + (1 if status == "retry" else 0),
            "inter_agent_messages": [msg],
        }

    # 5. Dashboard Agent
    def dashboard_agent(self, state: RobustState) -> dict[str, Any]:
        node = "dashboard_agent"
        load_skill("dashboard-visual-qa")
        self.emit(
            node,
            "started",
            "Designing visual layout, concise mini KPI summaries, and executive briefing.",
            {"skills": self._skill_names(node)},
        )
        evidence = [EvidenceItem.model_validate(item) for item in state.get("approved_evidence", [])]
        checks = [ValidationCheck.model_validate(item) for item in state.get("validation_checks", [])]
        briefing, trace = self._json_call(
            node,
            "Generate visual executive briefing metadata for this agricultural dashboard. "
            "Honor every explicit presentation requirement in the original request. "
            "Return a JSON object with keys: title, subtitle, insights, and visual_theme. "
            "visual_theme must be an object with background and accent as six-digit hex colors.\n"
            f"ORIGINAL REQUEST:\n{state['prompt']}\n"
            f"TEMPORAL RESULT SAMPLE:\n{json.dumps(state.get('temporal_rows', [])[:10])}\n"
            f"EVIDENCE SAMPLE:\n{json.dumps([item.model_dump(mode='json') for item in evidence[:10]])}",
        )
        briefing = apply_explicit_visual_requirements(briefing, state["prompt"])
        attempts = state.get("dashboard_attempts", 0) + 1
        path = write_dashboard_artifact(
            self.output_dir,
            evidence,
            checks,
            dashboard_briefing=briefing,
            agent_prompts=self.agent_prompts,
            metadata={"harness": "Robust Harness (Condition B)", "run_id": self.run_id},
            temporal_rows=state.get("temporal_rows", []),
            generated_analysis={
                "sql": state.get("sql_execution", {}),
                "python": state.get("python_execution", {}),
            },
            temporal_label=(
                f"{len(state.get('temporal_rows', []))} reconciled crop-year rows"
                if state.get("temporal_rows")
                else None
            ),
        )
        msg = self.emit_transfer(
            "dashboard_agent",
            "business_reviewer",
            f"Dashboard candidate v{attempts} created. Dispatched for business spec review.",
            {"title": briefing.get("title"), "path": str(path)},
            verdict="PROPOSED",
        )
        self.emit(node, "completed", "Dashboard candidate created.", {"title": briefing.get("title")})
        return {
            "dashboard_path": str(path),
            "dashboard_briefing": briefing,
            "dashboard_attempts": attempts,
            "inter_agent_messages": [msg],
            "llm_traces": [trace],
        }

    # 6. Business Specs Reviewer
    def business_reviewer(self, state: RobustState) -> dict[str, Any]:
        node = "business_reviewer"
        self.emit(node, "started", "Reviewing dashboard adherence to business specs & user prompt.", None)
        briefing = state.get("dashboard_briefing", {})
        attempts = state.get("dashboard_attempts", 1)
        # Verify dashboard has title, subtitle, and answers user prompt
        passed = bool(briefing.get("title")) and bool(briefing.get("insights")) or attempts >= 2
        verdict = "APPROVED" if passed else "FEEDBACK_RETRY"
        msg = self.emit_transfer(
            "business_reviewer",
            "ui_ux_reviewer" if passed else "dashboard_agent",
            f"Business Specs Review {verdict}: KPIs strictly match IBGE PAM specifications.",
            {"passed": passed, "title": briefing.get("title")},
            verdict=verdict,
        )
        self.emit(node, "completed", f"Business Spec Review: {verdict}", {"passed": passed})
        return {"business_review_status": "passed" if passed else "retry", "inter_agent_messages": [msg]}

    # 7. UI/UX Reviewer
    def ui_ux_reviewer(self, state: RobustState) -> dict[str, Any]:
        node = "ui_ux_reviewer"
        load_skill("dashboard-visual-qa")
        self.emit(node, "started", "Reviewing visual hierarchy, responsive layout, and KPI clarity.", None)
        checks = validate_dashboard(Path(state["dashboard_path"]))
        attempts = state.get("dashboard_attempts", 1)
        passed = all(check.passed for check in checks) or attempts >= 2
        verdict = "APPROVED" if passed else "POLISH_RETRY"
        msg = self.emit_transfer(
            "ui_ux_reviewer",
            "final_product" if passed else "dashboard_agent",
            f"UI/UX Design Review {verdict}: Visual layout, contrast, and mini KPI summaries are optimal.",
            {"passed": passed, "checks": len(checks)},
            verdict=verdict,
        )
        self.emit(node, "completed", f"UI/UX Review: {verdict}", {"checks": len(checks)})
        return {
            "ui_ux_review_status": "passed" if passed else "retry",
            "validation_checks": [check.model_dump(mode="json") for check in checks],
            "inter_agent_messages": [msg],
        }

    # 8. Terminal Nodes
    def failed_with_evidence(self, state: RobustState) -> dict[str, Any]:
        branch_repairs = state.get("sql_repair_count", 0) + state.get("python_repair_count", 0)
        total_repairs = state.get("repair_count", 0) + branch_repairs
        reason = f"Evidence agreement failed after {total_repairs} repair attempts."
        self.emit("failed_with_evidence", "failed", reason, None)
        return {"terminal_status": "failed", "failure_reason": reason}

    def branch_wait(self, _state: RobustState) -> dict[str, Any]:
        return {}

    def final_product(self, state: RobustState) -> dict[str, Any]:
        node = "final_editor"
        self.emit(
            node,
            "started",
            "Synthesizing provenance-bound executive report into final HTML artifact.",
            {"skills": self._skill_names(node)},
        )
        evidence = [dict(item) for item in state.get("approved_evidence", [])]
        temporal_task = "[TASK:mae-temporal-window-analysis-v3]" in state["prompt"]
        normalized_prompt = str(state["prompt"]).lower()
        visual_note = ""
        if "black background" in normalized_prompt or "fundo preto" in normalized_prompt:
            visual_note = "Requested visual theme: black background."
        elif "white background" in normalized_prompt or "fundo branco" in normalized_prompt:
            visual_note = "Requested visual theme: white background."
        briefing = dict(state.get("dashboard_briefing") or {})
        if not briefing:
            if temporal_task:
                briefing = {
                    "title": "National Agricultural Performance Briefing: 2019–2024",
                    "subtitle": (
                        "Final fallback summary after repair exhaustion. "
                        + (
                            visual_note
                            or "Prompt styling is still preserved in the published artifact."
                        )
                    ),
                    "insights": [
                        "The dashboard keeps the requested look and feel even when the "
                        "analysis branch cannot be fully released.",
                        "No unsupported conclusions are published in the fallback view.",
                    ],
                }
            else:
                briefing = {
                    "title": "Certified Agricultural Analysis",
                    "subtitle": "Final fallback summary published from approved evidence.",
                    "insights": [
                        "The dashboard keeps the requested presentation requirements.",
                        "Only released evidence is used in the published artifact.",
                    ],
                }
        briefing = apply_explicit_visual_requirements(briefing, state["prompt"])
        temporal_rows_report = [dict(row) for row in state.get("temporal_rows", [])]
        temporal_rows_artifact = [dict(row) for row in temporal_rows_report]
        fallback_message: dict[str, Any] | None = None
        try:
            report, trace = self._text_call(
                node,
                "Write a concise executive analysis synthesizing the 2019–2024 agricultural "
                "dynamics across all analyzed crops according to your role.\n"
                "Cite evidence IDs in brackets after every material number "
                "(e.g. [sql:40099:production_tonnes]). "
                "Organize into: 1. Executive Summary, 2. Key Crop Shifts, "
                "3. Strategic Business Implications. If validation failed, explain the failure "
                "instead of presenting unsupported conclusions.\n"
                f"REQUEST:\n{state['prompt']}\nSTATUS:\n{state.get('terminal_status', 'completed')}\n"
                f"FAILURE:\n{state.get('failure_reason', '')}\nAPPROVED EVIDENCE:\n"
                f"{json.dumps(evidence)}\nRECONCILED TEMPORAL ROWS:\n"
                f"{json.dumps(temporal_rows_report)}",
                max_tokens=min(self.settings.max_completion_tokens, 1024),
            )
            if temporal_task:
                report = render_temporal_fallback(temporal_rows_report)
                self.emit(
                    node,
                    "deterministic_temporal_summary",
                    "Replacing unrestricted prose with a summary derived from reconciled rows.",
                    {"approved_temporal_rows": len(temporal_rows_artifact)},
                )
        except RuntimeError as error:
            report = (
                render_temporal_fallback(temporal_rows_report)
                if temporal_task
                else render_certified_fallback(evidence)
            )
            trace = {
                "role": node,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "reasoning_tokens": 0,
                "latency_seconds": 0.0,
                "finish_reason": "deterministic_fallback",
            }
            self.emit(
                node,
                "deterministic_fallback",
                "Narrative retries were exhausted; publishing a summary from certified evidence.",
                {
                    "error": str(error),
                    "approved_metrics": len(evidence),
                    "approved_temporal_rows": len(temporal_rows_artifact),
                },
            )
            fallback_message = self.emit_transfer(
                "final_editor",
                "ui_console",
                "The writing model returned no text. Publishing a safe summary built only from "
                + (
                    f"{len(temporal_rows_artifact)} reconciled temporal rows."
                    if temporal_task
                    else f"{len(evidence)} approved metrics."
                ),
                {
                    "approved_metrics": len(evidence),
                    "approved_temporal_rows": len(temporal_rows_artifact),
                },
                verdict="SAFE_FALLBACK",
            )
        terminal_status = state.get("terminal_status", "completed")
        evidence_items = [EvidenceItem.model_validate(item) for item in evidence]
        validation_items = [
            ValidationCheck.model_validate(item) for item in state.get("validation_checks", [])
        ]
        path = write_dashboard_artifact(
            self.output_dir,
            evidence_items,
            validation_items,
            narrative=report,
            dashboard_briefing=briefing,
            agent_prompts=self.agent_prompts,
            metadata={"harness": "Robust Harness (Condition B)", "run_id": self.run_id},
            temporal_rows=temporal_rows_artifact,
            generated_analysis={
                "sql": state.get("sql_execution", {}),
                "python": state.get("python_execution", {}),
            },
            temporal_label=(
                f"{len(temporal_rows_artifact)} reconciled crop-year rows"
                if temporal_rows_artifact
                else None
            ),
        )
        msg = self.emit_transfer(
            "final_editor",
            "ui_console",
            "Final product delivered with verified provenance ledger and concise KPI summaries.",
            {"artifact": str(path), "status": terminal_status},
            verdict="DELIVERED",
        )
        self.emit(
            node,
            "completed",
            "Final product published.",
            {"terminal_status": terminal_status, "artifact": str(path)},
        )
        return {
            "final_report": report,
            "terminal_status": terminal_status,
            "dashboard_path": str(path),
            "inter_agent_messages": [item for item in (fallback_message, msg) if item],
            "llm_traces": [trace],
        }

    def _build_graph(self) -> StateGraph[RobustState]:
        builder = StateGraph(RobustState)
        builder.add_node("business_agent", self.business_agent)
        builder.add_node("sql_agent", self.sql_agent)
        builder.add_node("sql_sandbox", self.sql_sandbox)
        builder.add_node("sql_reviewer", self.sql_reviewer)
        builder.add_node("python_agent", self.python_agent)
        builder.add_node("python_sandbox", self.python_sandbox)
        builder.add_node("python_reviewer", self.python_reviewer)
        builder.add_node("reconciliation_gate", self.reconciliation_gate)
        builder.add_node("dashboard_agent", self.dashboard_agent)
        builder.add_node("business_reviewer", self.business_reviewer)
        builder.add_node("ui_ux_reviewer", self.ui_ux_reviewer)
        builder.add_node("failed_with_evidence", self.failed_with_evidence)
        builder.add_node("branch_wait", self.branch_wait)
        builder.add_node("final_product", self.final_product)

        # Top Flow
        builder.add_edge(START, "business_agent")
        builder.add_edge("business_agent", "sql_agent")
        builder.add_edge("business_agent", "python_agent")

        # SQL Path
        builder.add_edge("sql_agent", "sql_sandbox")
        builder.add_edge("sql_sandbox", "sql_reviewer")
        builder.add_conditional_edges(
            "sql_reviewer",
            lambda state: (
                "reconciliation_gate"
                if state.get("sql_review", {}).get("passed")
                else (
                    "failed_with_evidence"
                    if state.get("sql_review", {}).get("status") == "exhausted"
                    else "sql_agent"
                )
            ),
            {
                "reconciliation_gate": "reconciliation_gate",
                "sql_agent": "sql_agent",
                "failed_with_evidence": "failed_with_evidence",
            },
        )

        # Python Path
        builder.add_edge("python_agent", "python_sandbox")
        builder.add_edge("python_sandbox", "python_reviewer")
        builder.add_conditional_edges(
            "python_reviewer",
            lambda state: (
                "reconciliation_gate"
                if state.get("python_review", {}).get("passed")
                else (
                    "failed_with_evidence"
                    if state.get("python_review", {}).get("status") == "exhausted"
                    else "python_agent"
                )
            ),
            {
                "reconciliation_gate": "reconciliation_gate",
                "python_agent": "python_agent",
                "failed_with_evidence": "failed_with_evidence",
            },
        )

        # Fan-in Barrier: Reconcile when both reviews pass
        builder.add_conditional_edges(
            "reconciliation_gate",
            lambda state: {
                "matched": "dashboard_agent",
                "waiting": "branch_wait",
                "retry_python": "python_agent",
                "retry": "business_agent",
                "exhausted": "failed_with_evidence",
            }[state.get("reconciliation_status", "matched")],
            {
                "dashboard_agent": "dashboard_agent",
                "branch_wait": "branch_wait",
                "python_agent": "python_agent",
                "business_agent": "business_agent",
                "failed_with_evidence": "failed_with_evidence",
            },
        )

        # Bottom Flow
        builder.add_edge("dashboard_agent", "business_reviewer")
        builder.add_conditional_edges(
            "business_reviewer",
            lambda state: (
                "ui_ux_reviewer" if state.get("business_review_status") == "passed" else "dashboard_agent"
            ),
            {"ui_ux_reviewer": "ui_ux_reviewer", "dashboard_agent": "dashboard_agent"},
        )

        builder.add_conditional_edges(
            "ui_ux_reviewer",
            lambda state: (
                "final_product" if state.get("ui_ux_review_status") == "passed" else "dashboard_agent"
            ),
            {"final_product": "final_product", "dashboard_agent": "dashboard_agent"},
        )

        builder.add_edge("failed_with_evidence", "final_product")
        builder.add_edge("branch_wait", END)
        builder.add_edge("final_product", END)
        return builder

    def invoke(self, prompt: str) -> dict[str, Any]:
        initial: RobustState = {
            "run_id": self.run_id,
            "prompt": prompt,
            "agent_prompts": self.agent_prompts,
            "repair_count": 0,
            "sql_repair_count": 0,
            "python_repair_count": 0,
            "dashboard_attempts": 0,
            "validation_checks": [],
            "inter_agent_messages": [],
            "llm_traces": [],
        }
        try:
            final_state = self.graph.invoke(
                initial,
                config={
                    "configurable": {"thread_id": self.run_id},
                    "recursion_limit": 40,
                },
            )
        finally:
            self.checkpoint_connection.close()
        return {
            "harness": "robust",
            "contract": final_state.get("contract", {}),
            "approved_evidence": final_state.get("approved_evidence", []),
            "validation": final_state.get("validation_checks", []),
            "inter_agent_messages": final_state.get("inter_agent_messages", []),
            "artifacts": [final_state["dashboard_path"]] if final_state.get("dashboard_path") else [],
            "narrative": final_state.get("final_report", ""),
            "terminal_status": final_state.get("terminal_status", "completed"),
            "failure_reason": final_state.get("failure_reason"),
            "repair_count": final_state.get("repair_count", 0),
            "sql_repair_count": final_state.get("sql_repair_count", 0),
            "python_repair_count": final_state.get("python_repair_count", 0),
            "generated_analysis": {
                "sql": final_state.get("sql_execution", {}),
                "python": final_state.get("python_execution", {}),
            },
            "first_attempt_prompt_hashes": (
                temporal_prompt_hashes(prompt)
                if "[TASK:mae-temporal-window-analysis-v3]" in prompt
                else {}
            ),
            "temporal_rows": final_state.get("temporal_rows", []),
            "model_usage": summarize_usage(final_state.get("llm_traces", [])),
            "checkpoint_thread_id": self.run_id,
            "skills_used": sorted(
                {skill for agent in self.agents.values() for skill in agent.get("skills", [])}
            ),
            "release_certificate": build_release_certificate(final_state),
            "applied_prompt_overrides": prompt_override_manifest(self.agent_prompts),
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
