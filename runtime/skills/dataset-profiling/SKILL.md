---
name: dataset-profiling
description: Profile the fixed IBGE PAM DuckDB snapshot before analysis and block work when grain, coverage, or schema invariants fail.
---

# Dataset Profiling

Use this skill before any analytical branch.

- Read metadata through the read-only DuckDB connection; do not modify data or fetch new data.
- Confirm the grain is one row per `municipality_code`, `year`, and `crop_code`.
- Record row count, municipality and crop counts, year bounds, null rates, duplicate keys, schema, and source manifest availability.
- Treat IBGE suppression and unavailable markers as nulls, never as zero.
- Fail closed on an empty table, duplicate grain, unexpected columns or types, years outside the frozen scope, or missing source identity.

Return a profile object plus explicit pass/fail checks. Do not produce business conclusions.
