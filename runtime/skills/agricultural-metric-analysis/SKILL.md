---
name: agricultural-metric-analysis
description: Define and analyze the frozen PAM agricultural metrics while preserving units, aggregation rules, periods, and business meaning.
---

# Agricultural Metric Analysis

Use this skill when translating the frozen request into metrics or interpreting computed evidence.

- Keep planted area in hectares, production in tonnes, yield in kilograms per harvested hectare, and production value in thousand BRL.
- Sum planted area, harvested area, production, and production value across municipalities.
- Recompute aggregate yield as `sum(production_tonnes) * 1000 / sum(harvested_area_ha)`; never sum or simply average municipal yields.
- Compare the earliest and latest available years only after confirming comparable coverage.
- Treat nominal production value as nominal; do not claim real growth without an inflation adjustment supplied by the experiment.
- Distinguish absolute scale from percentage change and surface null or zero denominators.

Return metric selections and risks, or evidence with period, unit, method, and provenance. Do not invent causal explanations.
