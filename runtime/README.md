# Robust Runtime

This FastAPI service is the validated benchmark condition. A frozen LangGraph state machine coordinates eight specialist roles, five runtime skills, independent SQL and Python evidence, reconciliation, bounded repair, checkpoints, artifact checks, and a provenance-bound final report.

## Start

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp .env.example .env
.venv/bin/python -m mae_runtime.dataset --fixture --output ../data/agriculture.duckdb
.venv/bin/uvicorn mae_runtime.app:app --host 127.0.0.1 --port 8788
```

Use `--estimate` to inspect the approved dataset scope without downloading it. The full 42-chunk download requires the explicit `--full` flag.

## Inspect the graph

```bash
.venv/bin/python -m mae_runtime.graph --describe
```

The SQL and Python branches use an explicit fan-in barrier before evidence reconciliation. Graph state is checkpointed to ignored SQLite storage using the run ID as the LangGraph thread ID.

Structured Qwen calls use LM Studio's native endpoint with reasoning disabled; narrative generation keeps reasoning enabled. This avoids spending the entire output allowance before JSON appears while preserving reasoning for synthesis.

## Verify

```bash
.venv/bin/ruff check src tests
.venv/bin/pytest -q
```
