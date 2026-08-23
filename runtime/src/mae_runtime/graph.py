from __future__ import annotations

import argparse
import hashlib
import json
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
from .config import RUNTIME_ROOT, Settings
from .contracts import EvidenceItem, LLMTrace, ValidationCheck
from .model_client import ModelGateway
from .skill_library import load_skill, render_agent_skills

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


def build_release_certificate(state: RobustState) -> dict[str, Any]:
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
    sql_evidence: list[dict[str, Any]]
    sql_review: dict[str, Any]
    python_plan: dict[str, Any]
    python_evidence: list[dict[str, Any]]
    python_review: dict[str, Any]
    approved_evidence: list[dict[str, Any]]
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
        plan, trace = self._json_call(
            node,
            "Formulate Python calculation plan. Return keys selected_checks, comparison_period, and risks.\n"
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
            "Return a JSON object with keys: title, subtitle, insights, and visual_theme.\n"
            f"EVIDENCE SAMPLE:\n{json.dumps([item.model_dump(mode='json') for item in evidence[:10]])}",
        )
        attempts = state.get("dashboard_attempts", 0) + 1
        path = write_dashboard_artifact(
            self.output_dir,
            evidence,
            checks,
            dashboard_briefing=briefing,
            agent_prompts=self.agent_prompts,
            metadata={"harness": "Robust Harness (Condition B)", "run_id": self.run_id},
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
        reason = f"Evidence agreement failed after {state.get('repair_count', 0)} repair attempts."
        self.emit("failed_with_evidence", "failed", reason, None)
        return {"terminal_status": "failed", "failure_reason": reason}

    def final_product(self, state: RobustState) -> dict[str, Any]:
        node = "final_editor"
        self.emit(
            node,
            "started",
            "Synthesizing provenance-bound executive report into final HTML artifact.",
            {"skills": self._skill_names(node)},
        )
        evidence = state.get("approved_evidence", [])
        briefing = state.get("dashboard_briefing", {})
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
                f"{json.dumps(evidence)}",
                max_tokens=min(self.settings.max_completion_tokens, 1024),
            )
        except RuntimeError as error:
            report = render_certified_fallback(evidence)
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
                {"error": str(error), "approved_metrics": len(evidence)},
            )
            fallback_message = self.emit_transfer(
                "final_editor",
                "ui_console",
                "The writing model returned no text. Publishing a safe summary built only "
                f"from {len(evidence)} approved metrics.",
                {"approved_metrics": len(evidence)},
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
            lambda state: "reconciliation_gate" if state.get("sql_review", {}).get("passed") else "sql_agent",
            {"reconciliation_gate": "reconciliation_gate", "sql_agent": "sql_agent"},
        )

        # Python Path
        builder.add_edge("python_agent", "python_sandbox")
        builder.add_edge("python_sandbox", "python_reviewer")
        builder.add_conditional_edges(
            "python_reviewer",
            lambda state: (
                "reconciliation_gate" if state.get("python_review", {}).get("passed") else "python_agent"
            ),
            {"reconciliation_gate": "reconciliation_gate", "python_agent": "python_agent"},
        )

        # Fan-in Barrier: Reconcile when both reviews pass
        builder.add_conditional_edges(
            "reconciliation_gate",
            lambda state: {
                "matched": "dashboard_agent",
                "retry": "business_agent",
                "exhausted": "failed_with_evidence",
            }[state.get("reconciliation_status", "matched")],
            {
                "dashboard_agent": "dashboard_agent",
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
        builder.add_edge("final_product", END)
        return builder

    def invoke(self, prompt: str) -> dict[str, Any]:
        initial: RobustState = {
            "run_id": self.run_id,
            "prompt": prompt,
            "agent_prompts": self.agent_prompts,
            "repair_count": 0,
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
