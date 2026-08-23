from __future__ import annotations

import hashlib
import json
from typing import Any

from .temporal_contract import REQUIRED_COLUMNS

FROZEN_TEMPORAL_CONTRACT = {
    "grain": "one row per crop_code and year",
    "years": [2019, 2020, 2021, 2022, 2023, 2024],
    "crop_count": 7,
    "row_count": 42,
    "columns": list(REQUIRED_COLUMNS),
    "weighted_yield": "sum(production_tonnes) * 1000 / sum(harvested_area_ha)",
    "yoy": "(current / previous - 1) * 100; null for 2019 or zero/null previous",
    "rank": "dense rank within year by descending production_tonnes",
    "trailing_yield": "average of current and at most two preceding annual weighted yields",
    "yield_vs_trailing": "(current weighted yield / trailing yield - 1) * 100",
}


def temporal_generation_prompt(
    branch: str, request: str, contract: dict[str, Any]
) -> str:
    del contract
    columns = ", ".join(REQUIRED_COLUMNS)
    shared = (
        f"FROZEN REQUEST:\n{request}\nCONTRACT:\n{json.dumps(FROZEN_TEMPORAL_CONTRACT)}\n"
        "The first attempt receives no checker diagnostics or oracle values."
    )
    if branch == "sql":
        return (
            "Generate the complete DuckDB SQL implementation. Return keys code and assumptions. "
            "code must be one read-only SELECT/WITH query over crop_metrics and return exactly: "
            f"{columns}. Use staged CTEs plus LAG, DENSE_RANK, and AVG ... ROWS BETWEEN 2 "
            "PRECEDING AND CURRENT ROW.\nTABLE crop_metrics(municipality_code, "
            "municipality_name, state_code, year, crop_code, crop_name, planted_area_ha, "
            "harvested_area_ha, production_tonnes, yield_kg_ha, "
            f"production_value_thousand_brl).\n{shared}"
        )
    if branch == "python":
        return (
            "Generate an independent pure-Python implementation. Return keys code and assumptions. "
            f"code must define analyze(rows) and return dictionaries with exactly: {columns}. "
            "Imports, lambda expressions, files, network, SQL results, and the national_crop_year "
            "view are unavailable. Use explicit loops instead of callback keys. Input rows contain "
            "municipality_code, crop_code, crop_name, year, planted_area_ha, harvested_area_ha, "
            f"production_tonnes, and production_value_thousand_brl.\n{shared}"
        )
    raise ValueError(f"Unsupported temporal branch: {branch}")


def temporal_prompt_hashes(request: str) -> dict[str, str]:
    return {
        branch: hashlib.sha256(
            temporal_generation_prompt(branch, request, {}).encode("utf-8")
        ).hexdigest()
        for branch in ("sql", "python")
    }
