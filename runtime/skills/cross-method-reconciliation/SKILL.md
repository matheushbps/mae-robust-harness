---
name: cross-method-reconciliation
description: Reconcile independently computed SQL and Python evidence, approve only matching facts, and route bounded repairs on disagreement.
---

# Cross Method Reconciliation

Use this skill after both analytical branches complete.

- Match evidence by crop and metric, never by list position.
- Compare start and end values with the configured relative tolerance.
- Require the same unit and period before numeric comparison.
- Approve the SQL evidence item only when an independent Python item exists and both endpoints agree.
- Emit one validation record per comparison with relative errors and a clear outcome.
- On disagreement, request repair of the failed analytical branches; do not rewrite values in the reconciler.
- Stop after the configured repair limit and produce a failure report with the available evidence ledger.

Only approved evidence may reach dashboards or executive prose.
