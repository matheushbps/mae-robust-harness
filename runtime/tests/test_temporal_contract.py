from __future__ import annotations

from copy import deepcopy

from mae_runtime.temporal_contract import REQUIRED_COLUMNS, validate_temporal_rows


def valid_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for crop_index in range(7):
        productions = [float((7 - crop_index) * 100 + year_index * 10) for year_index in range(6)]
        yields = [float(1000 + crop_index * 100 + year_index * 20) for year_index in range(6)]
        for year_index, year in enumerate(range(2019, 2025)):
            window = yields[max(0, year_index - 2) : year_index + 1]
            moving = sum(window) / len(window)
            previous = productions[year_index - 1] if year_index else None
            rows.append(
                {
                    "crop_code": f"C{crop_index + 1}",
                    "crop_name": f"Crop {crop_index + 1}",
                    "year": year,
                    "production_tonnes": productions[year_index],
                    "weighted_yield_kg_ha": yields[year_index],
                    "yoy_production_pct": (
                        (productions[year_index] / previous - 1) * 100 if previous else None
                    ),
                    "production_rank": crop_index + 1,
                    "trailing_3y_yield_kg_ha": moving,
                    "yield_vs_trailing_pct": (yields[year_index] / moving - 1) * 100,
                }
            )
    return rows


def diagnostic_codes(rows: list[dict[str, object]]) -> set[str]:
    return {item.code for item in validate_temporal_rows(rows, "a" * 64)}


def test_accepts_exact_temporal_contract() -> None:
    rows = valid_rows()
    assert tuple(rows[0]) == REQUIRED_COLUMNS
    assert validate_temporal_rows(rows, "a" * 64) == []


def test_rejects_wrong_schema_and_duplicate_grain() -> None:
    rows = valid_rows()
    rows[0]["unexpected"] = 1
    rows[1] = deepcopy(rows[0])
    assert {"wrong_columns", "duplicate_crop_year"} <= diagnostic_codes(rows)


def test_rejects_temporal_math_and_rank_errors() -> None:
    rows = valid_rows()
    rows[0]["yoy_production_pct"] = 0.0
    rows[1]["trailing_3y_yield_kg_ha"] = 999.0
    rows[2]["production_rank"] = 7
    assert {"invalid_2019_yoy", "invalid_trailing_window", "invalid_rank"} <= diagnostic_codes(rows)
