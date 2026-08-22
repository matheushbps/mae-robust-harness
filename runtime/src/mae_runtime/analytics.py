from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

import duckdb

from .contracts import EvidenceItem, ValidationCheck
from .dataset import file_sha256

METRICS = {
    "planted_area_ha": ("ha", 3),
    "production_tonnes": ("tonnes", 4),
    "yield_kg_ha": ("kg/ha", 5),
    "production_value_thousand_brl": ("thousand BRL", 6),
}
NATIONAL_QUERY = """
SELECT crop_code, crop_name, year, planted_area_ha, production_tonnes,
       yield_kg_ha, production_value_thousand_brl
FROM national_crop_year
ORDER BY crop_code, year
""".strip()


def profile_dataset(dataset_path: Path) -> dict[str, Any]:
    connection = duckdb.connect(str(dataset_path), read_only=True)
    try:
        row = connection.execute(
            """
            SELECT count(*) AS rows,
                   count(DISTINCT municipality_code) AS municipalities,
                   count(DISTINCT crop_code) AS crops,
                   min(year) AS first_year,
                   max(year) AS last_year,
                   count(*) - count(DISTINCT (municipality_code, year, crop_code)) AS duplicate_keys,
                   avg((planted_area_ha IS NULL)::INTEGER) AS planted_null_rate,
                   avg((harvested_area_ha IS NULL)::INTEGER) AS harvested_null_rate,
                   avg((production_tonnes IS NULL)::INTEGER) AS production_null_rate,
                   avg((production_value_thousand_brl IS NULL)::INTEGER) AS value_null_rate,
                   (SELECT count(*) FROM source_chunks) AS source_chunks
            FROM crop_metrics
            """
        ).fetchone()
        schema = connection.execute("DESCRIBE crop_metrics").fetchall()
    finally:
        connection.close()
    manifest_path = dataset_path.with_suffix(".manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
    return {
        "rows": row[0],
        "municipalities": row[1],
        "crops": row[2],
        "first_year": row[3],
        "last_year": row[4],
        "duplicate_keys": row[5],
        "null_rates": {
            "planted_area": row[6],
            "harvested_area": row[7],
            "production": row[8],
            "production_value": row[9],
        },
        "schema": [{"column": item[0], "type": item[1]} for item in schema],
        "source_chunks": row[10],
        "manifest_present": bool(manifest),
        "manifest_mode": manifest.get("mode"),
        "dataset_sha256": file_sha256(dataset_path),
    }


def _evidence_from_aggregates(
    rows: list[tuple[Any, ...]], branch: str, method: str, dataset_sha256: str
) -> list[EvidenceItem]:
    grouped: dict[str, dict[int, tuple[Any, ...]]] = defaultdict(dict)
    crop_names: dict[str, str] = {}
    for row in rows:
        crop_code, crop_name, year = str(row[0]), str(row[1]), int(row[2])
        grouped[crop_code][year] = row
        crop_names[crop_code] = crop_name
    evidence: list[EvidenceItem] = []
    for crop_code, by_year in sorted(grouped.items()):
        years = sorted(by_year)
        if len(years) < 2:
            continue
        start_year, end_year = years[0], years[-1]
        for metric, (unit, index) in METRICS.items():
            start_raw, end_raw = by_year[start_year][index], by_year[end_year][index]
            start_value = float(start_raw) if start_raw is not None else None
            end_value = float(end_raw) if end_raw is not None else None
            change = None
            if start_value not in (None, 0.0) and end_value is not None:
                change = (end_value - start_value) / abs(start_value) * 100.0
            evidence.append(
                EvidenceItem(
                    evidence_id=f"{branch}:{crop_code}:{metric}",
                    match_key=f"{crop_code}:{metric}",
                    branch=branch,
                    crop_code=crop_code,
                    crop_name=crop_names[crop_code],
                    metric=metric,
                    start_year=start_year,
                    end_year=end_year,
                    start_value=start_value,
                    end_value=end_value,
                    change_percent=change,
                    unit=unit,
                    method=method,
                    provenance={
                        "source_table": 5457,
                        "grain": "national crop-year",
                        "dataset_sha256": dataset_sha256,
                        "method_sha256": hashlib.sha256(method.encode("utf-8")).hexdigest(),
                    },
                )
            )
    return evidence


def run_sql_analysis(dataset_path: Path) -> list[EvidenceItem]:
    connection = duckdb.connect(str(dataset_path), read_only=True)
    try:
        rows = connection.execute(NATIONAL_QUERY).fetchall()
    finally:
        connection.close()
    return _evidence_from_aggregates(rows, "sql", NATIONAL_QUERY, file_sha256(dataset_path))


def run_python_analysis(dataset_path: Path) -> list[EvidenceItem]:
    connection = duckdb.connect(str(dataset_path), read_only=True)
    try:
        raw_rows = connection.execute(
            """
            SELECT crop_code, crop_name, year, planted_area_ha, harvested_area_ha,
                   production_tonnes, production_value_thousand_brl
            FROM crop_metrics
            ORDER BY crop_code, year, municipality_code
            """
        ).fetchall()
    finally:
        connection.close()
    aggregates: dict[tuple[str, str, int], dict[str, float]] = defaultdict(
        lambda: {"planted_area": 0.0, "harvested_area": 0.0, "production": 0.0, "value": 0.0}
    )
    for crop_code, crop_name, year, planted_area, harvested_area, production, value in raw_rows:
        totals = aggregates[(str(crop_code), str(crop_name), int(year))]
        if planted_area is not None:
            totals["planted_area"] += float(planted_area)
        if harvested_area is not None:
            totals["harvested_area"] += float(harvested_area)
        if production is not None:
            totals["production"] += float(production)
        if value is not None:
            totals["value"] += float(value)
    rows: list[tuple[Any, ...]] = []
    for (crop_code, crop_name, year), totals in sorted(aggregates.items()):
        weighted_yield = (
            totals["production"] * 1000.0 / totals["harvested_area"] if totals["harvested_area"] else None
        )
        rows.append(
            (
                crop_code,
                crop_name,
                year,
                totals["planted_area"],
                totals["production"],
                weighted_yield,
                totals["value"],
            )
        )
    return _evidence_from_aggregates(
        rows,
        "python",
        "Python sums over municipal fact rows",
        file_sha256(dataset_path),
    )


def _relative_error(left: float | None, right: float | None) -> float:
    if left is None or right is None:
        return 0.0 if left is right else math.inf
    denominator = max(abs(left), abs(right), 1.0)
    return abs(left - right) / denominator


def reconcile_evidence(
    sql_evidence: list[EvidenceItem],
    python_evidence: list[EvidenceItem],
    tolerance: float,
) -> tuple[list[ValidationCheck], list[EvidenceItem]]:
    python_by_key = {item.match_key: item for item in python_evidence}
    checks: list[ValidationCheck] = []
    approved: list[EvidenceItem] = []
    for sql_item in sql_evidence:
        python_item = python_by_key.get(sql_item.match_key)
        start_error = _relative_error(sql_item.start_value, python_item.start_value if python_item else None)
        end_error = _relative_error(sql_item.end_value, python_item.end_value if python_item else None)
        same_contract = bool(
            python_item
            and sql_item.unit == python_item.unit
            and sql_item.start_year == python_item.start_year
            and sql_item.end_year == python_item.end_year
        )
        passed = same_contract and max(start_error, end_error) <= tolerance
        checks.append(
            ValidationCheck(
                check_id=f"agreement:{sql_item.match_key}",
                passed=passed,
                message=(
                    "SQL and Python agree within tolerance." if passed else "Independent results disagree."
                ),
                details={
                    "start_relative_error": start_error,
                    "end_relative_error": end_error,
                    "same_unit_and_period": same_contract,
                },
            )
        )
        if passed:
            approved.append(sql_item)
    return checks, approved


def execute_readonly_sql(dataset_path: Path, query: str, max_rows: int = 500) -> dict[str, Any]:
    normalized = query.strip().rstrip(";")
    lowered = re.sub(r"\s+", " ", normalized.lower())
    if not (lowered.startswith("select ") or lowered.startswith("with ")):
        raise ValueError("Only SELECT or WITH queries are allowed.")
    forbidden = re.compile(
        r"\b(insert|update|delete|drop|alter|create|attach|detach|copy|export|import|install|load|pragma)\b"
    )
    if forbidden.search(lowered) or ";" in normalized:
        raise ValueError("The query contains a forbidden operation.")
    connection = duckdb.connect(str(dataset_path), read_only=True)
    try:
        cursor = connection.execute(f"SELECT * FROM ({normalized}) AS bounded_query LIMIT {max_rows + 1}")
        columns = [description[0] for description in cursor.description]
        rows = cursor.fetchall()
    finally:
        connection.close()
    return {
        "columns": columns,
        "rows": [list(row) for row in rows[:max_rows]],
        "truncated": len(rows) > max_rows,
    }


def write_dashboard_artifact(
    output_dir: Path,
    evidence: list[EvidenceItem],
    validation: list[ValidationCheck],
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / "dashboard.json"
    payload = {
        "title": "Brazilian Municipal Crop Intelligence",
        "source": "IBGE SIDRA PAM table 5457",
        "evidence": [item.model_dump(mode="json") for item in evidence],
        "validation": [item.model_dump(mode="json") for item in validation],
        "charts": [
            {"id": "change-by-crop", "type": "bar", "metric": "change_percent"},
            {"id": "start-end-comparison", "type": "slope", "metric": "end_value"},
        ],
    }
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def validate_dashboard(path: Path) -> list[ValidationCheck]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    checks = [
        ValidationCheck(
            check_id="visual:has_title",
            passed=bool(payload.get("title")),
            message="Dashboard has a title.",
        ),
        ValidationCheck(
            check_id="visual:has_evidence",
            passed=bool(payload.get("evidence")),
            message="Dashboard contains approved evidence.",
        ),
        ValidationCheck(
            check_id="visual:has_source",
            passed=bool(payload.get("source")),
            message="Dashboard has an explicit source note.",
        ),
        ValidationCheck(
            check_id="visual:chart_contracts",
            passed=all(chart.get("type") and chart.get("metric") for chart in payload.get("charts", [])),
            message="Every chart has a type and metric.",
        ),
    ]
    return checks
