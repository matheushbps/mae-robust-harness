# Failure Attribution

Classify a failed run before changing the model or graph.

| Class | Example signal | Owner |
| --- | --- | --- |
| Specification | Required output is ambiguous | Business contract |
| Context | Role lacks a needed schema or skill | Agent configuration |
| Environment | Dataset or model endpoint is unavailable | Runtime health gate |
| Verification | Validator misses or rejects the wrong invariant | Validation code |
| State | Parallel results are lost or joined early | LangGraph state and edges |
| Model inference | Empty, invalid, or unsupported output | Model adapter and bounded retry |
| Data quality | Duplicate grain, suppression, or missing coverage | Dataset profile |

The first live Qwen smoke run produced a model-inference failure: reasoning consumed the bounded completion without visible JSON. The robust condition detected and retried it; the adapter was then corrected to use an explicitly supported reasoning-off mode for structured calls. This failure remains useful benchmark evidence because the symptoms, retry behavior, and remedy are inspectable.
