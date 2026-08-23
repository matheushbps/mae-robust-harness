# Robust Harness Repository Guide

## Purpose

This repository is the validated condition in the MAE controlled experiment. Its job is to convert the same prompt, dataset, model, and hardware used by the simple condition into a more reliable, observable, and auditable agricultural analysis through explicit harness engineering.

Codex or another coding agent authors and freezes the agent definitions, tools, state, and graph. Qwen is only the runtime inference provider inside those fixed roles. Runtime agents must never create or redesign other agents.

## Non-negotiable experiment contract

- Keep all public artifacts, code, prompts, traces, and UI copy in English.
- Keep the business prompt byte-identical to the simple condition.
- Pin `qwen/qwen3.6-35b-a3b`, its decoding settings, and the 65,280-token loaded context in the run manifest.
- Use the same IBGE SIDRA PAM 5457 snapshot, filters, schema, and checksums as the simple condition.
- Change only harness behavior when comparing conditions.
- Never commit private endpoints, credentials, generated databases, checkpoints, or run artifacts.

## Harness subsystems

### Instructions

- This file is the repository map, not an encyclopedia.
- Agent role, system prompt, tool, input, and output contracts live in `runtime/config/agents.json`.
- Reusable procedures live in `runtime/skills/` and are versioned.
- Architecture and decisions live in `docs/`.

### Tools

- Business Analyst: schema catalog only.
- Data Profiler: read-only DuckDB metadata and deterministic profiling.
- SQL Analyst: validated `SELECT`/`WITH` queries only.
- Python Analyst: bounded analytics functions; no arbitrary shell or network.
- Evidence Reconciler: evidence store and numeric tolerance checks only.
- Dashboard Engineer: approved evidence and artifact writer only.
- Visual Reviewer: rendered artifacts and visual checklist only.
- Final Editor: approved claims and limitations only.

Apply least privilege. All writes stay under `outputs/`; all data access is read-only during a run.

### Environment

- Frontend: React 19, TypeScript, vinext, Node 22+.
- Runtime: Python, FastAPI, LangGraph, DuckDB, HTTPX, Pydantic.
- Simple runtime uses port 8787; robust runtime uses port 8788.
- The local Qwen server uses an OpenAI-compatible API configured through the shell or a local secret manager. Environment files are never versioned.

### State

- `RobustState` is the authoritative typed graph state.
- LangGraph checkpoints state at super-step boundaries using local SQLite.
- Evidence is append-only and every material claim references an evidence ID.
- Update `PROGRESS.md` before ending an incomplete development session.
- Do not place large transcripts in graph state; store artifact paths and structured summaries.

### Feedback

- Every node has an explicit pass condition.
- SQL and Python independently reproduce key totals.
- Reconciliation uses declared numeric tolerances.
- Repair routes receive the failed invariant and have a maximum of two attempts.
- Exhausted retries terminate with evidence; they never silently pass.
- Separate generation from validation.

## Graph contract

```text
START → business_contract → data_profile
                           ↙           ↘
                    sql_analysis   python_analysis
                           ↘           ↙
                    evidence_reconciliation
                       ↙ pass  retry ↘
             dashboard_build      targeted_repair
                    ↓                   ↘ bounded loop
               visual_review
                    ↓
               final_editor → END
```

Do not replace the graph with a single prompt, an unbounded ReAct loop, or opaque agent delegation.

## Repository map

- `app/`: experiment console and server-side proxy.
- `runtime/`: FastAPI service, LangGraph workflow, Qwen client, tools, validators, and tests.
- `runtime/config/`: frozen experiment and agent contracts.
- `runtime/skills/`: five reusable English skill modules.
- `docs/`: architecture, failure attribution, and decisions.
- `data/`: generated local data only; full datasets are ignored by Git.
- `outputs/`: ignored run artifacts.

## First run

```bash
npm install
cd runtime && python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
export AGENT_RUNTIME_URL=http://127.0.0.1:8788
export MODEL_BASE_URL=http://127.0.0.1:1234/v1
export MODEL_ID=qwen/qwen3.6-35b-a3b
```

## Verification

```bash
npm run lint
npm test
cd runtime && .venv/bin/ruff check src tests
cd runtime && .venv/bin/pytest -q
cd runtime && .venv/bin/python -m mae_runtime.graph --describe
```

## Definition of done

- The requested behavior is represented in typed state, nodes, edges, and explicit failure routes.
- Agent tool permissions remain least-privilege.
- Required node, route, validator, API, and integration tests pass.
- Frontend lint and production build pass.
- The prompt, dataset, model, and shared API contract still match the simple condition.
- Run artifacts contain provenance, validator outcomes, latency, tokens, retries, and terminal status.
- `PROGRESS.md` records remaining work or states that the repository is clean.

When a run fails, attribute it to task specification, context, environment, verification, state, model inference, or data quality before changing the model.

Reference framework: [Why capable agents still fail](https://walkinglabs.github.io/learn-harness-engineering/pt-BR/lectures/lecture-01-why-capable-agents-still-fail/) and [What a harness actually is](https://walkinglabs.github.io/learn-harness-engineering/pt-BR/lectures/lecture-02-what-a-harness-actually-is/).
