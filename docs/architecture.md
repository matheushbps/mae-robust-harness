# Robust Harness Architecture

## Runtime boundary

Codex authors and freezes the repository. During an experiment, the FastAPI service loads fixed agent definitions and skill packets, LangGraph controls execution, and Qwen supplies only local inference. Runtime agents cannot create agents or redesign the graph.

## Graph

```text
START → business contract → dataset profile
                              ├→ SQL analysis ───┐
                              └→ Python analysis ┤
                                                ↓
                                    evidence reconciliation
                                      ├ pass → dashboard → visual QA → final editor → END
                                      ├ retry → targeted repair → both analysis branches
                                      └ exhausted → failed with evidence → final editor → END
```

The reconciliation edge is a fan-in barrier over both analytical nodes. `RobustState` is typed, append-only trace fields use reducers, and a SQLite checkpointer persists super-step state under the run ID.

## Five skill packets

| Skill | Runtime consumers | Control added |
| --- | --- | --- |
| Dataset profiling | Data Profiler | Grain, coverage, null, and schema gates |
| Agricultural metric analysis | Business, SQL, Python, Final Editor | Domain units and aggregation rules |
| SQL evidence extraction | SQL Analyst | Read-only query and provenance contract |
| Cross-method reconciliation | Reconciler, Final Editor | Independent agreement and bounded failure |
| Dashboard visual QA | Dashboard Engineer, Visual Reviewer | Evidence-to-chart and rendering checks |

The loader reads only the skills assigned to the active role. Deterministic nodes also verify that their declared skill packet exists before running.

## Model adapter policy

The Qwen model defaults to reasoning on. Structured nodes use LM Studio's native chat API with reasoning off so the bounded generation contains JSON. The final narrative uses reasoning on. Token and latency metadata are recorded without storing private chain-of-thought.
