---
name: sql-evidence-extraction
description: Extract bounded, auditable evidence from the PAM DuckDB snapshot using read-only SQL and provenance-bearing result contracts.
---

# Sql Evidence Extraction

Use this skill for the SQL branch only.

- Allow a single `SELECT` or `WITH` statement against approved tables and views.
- Reject mutation, DDL, extension loading, file access, `ATTACH`, `COPY`, `PRAGMA`, and multi-statement SQL.
- Bound ad hoc result sets to 500 rows.
- Preserve the executed query, source table, grain, period, metric, and unit in every evidence item.
- Use the frozen `national_crop_year` view for the benchmark comparison.
- Report empty or incomplete results as a failed branch; do not repair them with fabricated values.

Return structured evidence only. Narrative interpretation belongs downstream.
