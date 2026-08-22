---
name: dashboard-visual-qa
description: Build and review dashboard artifacts from approved evidence, checking labels, units, provenance, accessibility, and misleading encodings.
---

# Dashboard Visual Qa

Use this skill when building or reviewing the benchmark dashboard artifact.

- Read approved evidence only; reject unvalidated numbers.
- Preserve metric name, unit, period, crop label, and evidence ID in chart contracts.
- Require a descriptive title and explicit source note.
- Reject clipped labels, missing units, misleading axis truncation, unreadable contrast, or color-only status encoding.
- Verify that every plotted value maps to an approved evidence item.
- Emit deterministic checks before any optional multimodal review.

Return an artifact path and a validation ledger. Do not silently repair analytical evidence.
