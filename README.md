# Robust Agricultural Agent Harness

This repository is the graph-engineered variant of the MAE harness experiment. Its frontend submits the frozen business prompt and selected inference provider to a local runtime that executes the Codex-authored, frozen agent graph. Provider credentials remain server-side.

## Local interface

```bash
# Set these in your shell or local secret manager; no environment files are committed.
export AGENT_RUNTIME_URL=http://127.0.0.1:8788
export MODEL_BASE_URL=http://127.0.0.1:1234/v1
export MODEL_ID=qwen/qwen3.6-35b-a3b
npm install
npm run dev
```

The agent runtime must implement `POST /runs` at `AGENT_RUNTIME_URL`. The request contains `harness`, `prompt`, and `provider`; the `X-Harness-Variant` header is set to `robust`.

## Robustness contract

The final runtime will enforce typed state, scoped tools, bounded retries, Python and SQL execution gates, evidence reconciliation, provenance, visual validation, and immutable run manifests.
