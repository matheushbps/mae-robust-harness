from __future__ import annotations

import math
from collections import defaultdict
from typing import Any

from pydantic import BaseModel, ConfigDict

REQUIRED_COLUMNS = (
    "crop_code",
    "crop_name",
    "year",
    "production_tonnes",
    "weighted_yield_kg_ha",
    "yoy_production_pct",
    "production_rank",
    "trailing_3y_yield_kg_ha",
    "yield_vs_trailing_pct",
)


class TemporalRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    crop_code: str
    crop_name: str
    year: int
    production_tonnes: float
    weighted_yield_kg_ha: float | None
    yoy_production_pct: float | None
    production_rank: int
    trailing_3y_yield_kg_ha: float | None
    yield_vs_trailing_pct: float | None


class BranchDiagnostic(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = {}


def _close(left: float | None, right: float | None) -> bool:
    if left is None or right is None:
        return left is right
    return math.isclose(float(left), float(right), rel_tol=1e-9, abs_tol=1e-9)


def validate_temporal_rows(
    rows: list[dict[str, Any]], dataset_sha256: str
) -> list[BranchDiagnostic]:
    diagnostics: list[BranchDiagnostic] = []

    if any(tuple(row) != REQUIRED_COLUMNS for row in rows):
        diagnostics.append(
            BranchDiagnostic(
                code="wrong_columns",
                message="Every result row must have the exact temporal-analysis columns in order.",
            )
        )
    if len(rows) != 42:
        diagnostics.append(
            BranchDiagnostic(
                code="wrong_row_count",
                message="The analysis must return exactly 42 crop-year rows.",
                details={"actual": len(rows), "expected": 42},
            )
        )
    if len(dataset_sha256) != 64:
        diagnostics.append(
            BranchDiagnostic(code="missing_provenance", message="Dataset SHA256 is missing or invalid.")
        )

    keys = [(str(row.get("crop_code")), row.get("year")) for row in rows]
    if len(keys) != len(set(keys)):
        diagnostics.append(
            BranchDiagnostic(
                code="duplicate_crop_year",
                message="Every crop-year grain must occur exactly once.",
            )
        )

    crops = {str(row.get("crop_code")) for row in rows}
    years = {row.get("year") for row in rows}
    if len(crops) != 7:
        diagnostics.append(
            BranchDiagnostic(
                code="missing_crop",
                message="The result must cover all seven crops.",
                details={"actual": len(crops), "expected": 7},
            )
        )
    expected_years = set(range(2019, 2025))
    if years != expected_years:
        diagnostics.append(
            BranchDiagnostic(
                code="missing_year",
                message="Every year from 2019 through 2024 must be present.",
                details={"actual": sorted(year for year in years if isinstance(year, int))},
            )
        )

    parsed: list[TemporalRow] = []
    for index, row in enumerate(rows):
        try:
            item = TemporalRow.model_validate(row)
        except Exception as error:  # noqa: BLE001 - diagnostics preserve all invalid rows.
            diagnostics.append(
                BranchDiagnostic(
                    code="invalid_row",
                    message="A result row violates the typed contract.",
                    details={"index": index, "error": str(error)},
                )
            )
            continue
        numeric_values = (
            item.production_tonnes,
            item.weighted_yield_kg_ha,
            item.yoy_production_pct,
            item.trailing_3y_yield_kg_ha,
            item.yield_vs_trailing_pct,
        )
        if any(value is not None and not math.isfinite(value) for value in numeric_values):
            diagnostics.append(
                BranchDiagnostic(
                    code="non_finite_value",
                    message="Numeric results must be finite.",
                    details={"crop_code": item.crop_code, "year": item.year},
                )
            )
        parsed.append(item)

    by_crop: dict[str, list[TemporalRow]] = defaultdict(list)
    by_year: dict[int, list[TemporalRow]] = defaultdict(list)
    for item in parsed:
        by_crop[item.crop_code].append(item)
        by_year[item.year].append(item)

    for crop_code, crop_rows in by_crop.items():
        ordered = sorted(crop_rows, key=lambda item: item.year)
        yields: list[float | None] = []
        for index, item in enumerate(ordered):
            if item.year == 2019 and item.yoy_production_pct is not None:
                diagnostics.append(
                    BranchDiagnostic(
                        code="invalid_2019_yoy",
                        message="2019 year-over-year production must be null.",
                        details={"crop_code": crop_code},
                    )
                )
            if index:
                previous = ordered[index - 1].production_tonnes
                expected_yoy = (
                    (item.production_tonnes / previous - 1.0) * 100.0 if previous else None
                )
                if not _close(item.yoy_production_pct, expected_yoy):
                    diagnostics.append(
                        BranchDiagnostic(
                            code="invalid_yoy",
                            message="Year-over-year production does not match the prior crop-year.",
                            details={"crop_code": crop_code, "year": item.year},
                        )
                    )
            yields.append(item.weighted_yield_kg_ha)
            window = [value for value in yields[max(0, index - 2) : index + 1] if value is not None]
            expected_trailing = sum(window) / len(window) if window else None
            if not _close(item.trailing_3y_yield_kg_ha, expected_trailing):
                diagnostics.append(
                    BranchDiagnostic(
                        code="invalid_trailing_window",
                        message="Trailing yield must average the current and two prior annual yields.",
                        details={"crop_code": crop_code, "year": item.year},
                    )
                )
            expected_deviation = (
                (item.weighted_yield_kg_ha / expected_trailing - 1.0) * 100.0
                if item.weighted_yield_kg_ha is not None and expected_trailing
                else None
            )
            if not _close(item.yield_vs_trailing_pct, expected_deviation):
                diagnostics.append(
                    BranchDiagnostic(
                        code="invalid_yield_deviation",
                        message="Yield deviation does not match the trailing average.",
                        details={"crop_code": crop_code, "year": item.year},
                    )
                )

    for year, year_rows in by_year.items():
        distinct = sorted({item.production_tonnes for item in year_rows}, reverse=True)
        rank_by_value = {value: index + 1 for index, value in enumerate(distinct)}
        for item in year_rows:
            if item.production_rank != rank_by_value[item.production_tonnes]:
                diagnostics.append(
                    BranchDiagnostic(
                        code="invalid_rank",
                        message="Production rank is inconsistent with descending annual production.",
                        details={"crop_code": item.crop_code, "year": year},
                    )
                )

    unique: dict[tuple[str, str], BranchDiagnostic] = {}
    for item in diagnostics:
        key = (item.code, str(item.details))
        unique[key] = item
    return list(unique.values())
