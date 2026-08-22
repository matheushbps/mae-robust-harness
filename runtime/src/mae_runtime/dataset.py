from __future__ import annotations

import argparse
import hashlib
import json
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import duckdb
import httpx

TABLE_ID = 5457
YEARS = tuple(range(2019, 2025))
CROPS = {
    "40099": "Upland cotton (seed)",
    "40102": "Paddy rice",
    "40106": "Sugarcane",
    "40112": "Beans (grain)",
    "40122": "Corn (grain)",
    "40124": "Soybeans (grain)",
    "40127": "Wheat (grain)",
}
VARIABLES = {
    "8331": "planted_area_ha",
    "216": "harvested_area_ha",
    "214": "production_tonnes",
    "112": "yield_kg_ha",
    "215": "production_value_thousand_brl",
}
SIDRA_BASE_URL = "https://apisidra.ibge.gov.br/values"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def estimate_dataset() -> dict[str, Any]:
    municipalities = 5_570
    combinations = len(YEARS) * len(CROPS)
    long_rows = municipalities * combinations * len(VARIABLES)
    wide_rows = municipalities * combinations
    return {
        "municipalities": municipalities,
        "crop_year_chunks": combinations,
        "maximum_long_rows": long_rows,
        "maximum_wide_rows": wide_rows,
        "estimated_uncompressed_json_mb": 378,
        "estimated_compressed_source_mb": [10, 18],
        "estimated_duckdb_mb": [18, 24],
        "storage_policy": "Transform each chunk immediately; do not retain raw JSON.",
    }


def parse_sidra_value(raw: Any) -> float | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if text in {"", "...", "-", "X"}:
        return None
    try:
        return float(text.replace(",", "."))
    except ValueError:
        return None


def normalize_sidra_payload(payload: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not payload or "D1C" not in payload[0] or "D4C" not in payload[0]:
        raise ValueError("Unexpected SIDRA payload schema.")
    grouped: dict[tuple[str, int, str], dict[str, Any]] = {}
    for item in payload[1:]:
        municipality_code = str(item.get("D1C", ""))
        municipality_label = str(item.get("D1N", ""))
        crop_code = str(item.get("D4C", ""))
        variable_code = str(item.get("D2C", ""))
        if not municipality_code or crop_code not in CROPS or variable_code not in VARIABLES:
            continue
        year = int(item["D3C"])
        municipality_name, _, state_code = municipality_label.rpartition(" - ")
        key = (municipality_code, year, crop_code)
        row = grouped.setdefault(
            key,
            {
                "municipality_code": municipality_code,
                "municipality_name": municipality_name or municipality_label,
                "state_code": state_code or None,
                "year": year,
                "crop_code": crop_code,
                "crop_name": CROPS[crop_code],
                **dict.fromkeys(VARIABLES.values()),
            },
        )
        row[VARIABLES[variable_code]] = parse_sidra_value(item.get("V"))
    return [row for row in grouped.values() if any(row[column] is not None for column in VARIABLES.values())]


def initialize_database(connection: duckdb.DuckDBPyConnection) -> None:
    connection.execute(
        """
        CREATE TABLE crop_metrics (
            municipality_code VARCHAR NOT NULL,
            municipality_name VARCHAR NOT NULL,
            state_code VARCHAR,
            year INTEGER NOT NULL,
            crop_code VARCHAR NOT NULL,
            crop_name VARCHAR NOT NULL,
            planted_area_ha DOUBLE,
            harvested_area_ha DOUBLE,
            production_tonnes DOUBLE,
            yield_kg_ha DOUBLE,
            production_value_thousand_brl DOUBLE,
            source_table INTEGER NOT NULL DEFAULT 5457,
            PRIMARY KEY (municipality_code, year, crop_code)
        );
        CREATE TABLE source_chunks (
            year INTEGER NOT NULL,
            crop_code VARCHAR NOT NULL,
            source_url VARCHAR NOT NULL,
            sha256 VARCHAR NOT NULL,
            response_bytes BIGINT NOT NULL,
            api_rows BIGINT NOT NULL,
            normalized_rows BIGINT NOT NULL,
            retrieved_at TIMESTAMP NOT NULL DEFAULT current_timestamp,
            PRIMARY KEY (year, crop_code)
        );
        """
    )


def finalize_database(connection: duckdb.DuckDBPyConnection) -> None:
    connection.execute(
        """
        CREATE VIEW national_crop_year AS
        SELECT
            crop_code,
            crop_name,
            year,
            sum(planted_area_ha) AS planted_area_ha,
            sum(production_tonnes) AS production_tonnes,
            CASE
                WHEN sum(harvested_area_ha) > 0
                THEN sum(production_tonnes) * 1000.0 / sum(harvested_area_ha)
            END AS yield_kg_ha,
            sum(production_value_thousand_brl) AS production_value_thousand_brl
        FROM crop_metrics
        GROUP BY crop_code, crop_name, year;
        CREATE INDEX crop_metrics_year_crop ON crop_metrics(year, crop_code);
        CREATE INDEX crop_metrics_municipality ON crop_metrics(municipality_code);
        """
    )


def insert_rows(connection: duckdb.DuckDBPyConnection, rows: Iterable[dict[str, Any]]) -> int:
    values = [
        (
            row["municipality_code"],
            row["municipality_name"],
            row["state_code"],
            row["year"],
            row["crop_code"],
            row["crop_name"],
            row["planted_area_ha"],
            row["harvested_area_ha"],
            row["production_tonnes"],
            row["yield_kg_ha"],
            row["production_value_thousand_brl"],
        )
        for row in rows
    ]
    connection.executemany(
        """
        INSERT OR REPLACE INTO crop_metrics VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 5457)
        """,
        values,
    )
    return len(values)


def build_fixture_database(output_path: Path) -> dict[str, Any]:
    fixture_rows: list[dict[str, Any]] = []
    base = {
        "1100015": ("Alta Floresta D'Oeste", "RO", 1.0),
        "5103403": ("Cuiabá", "MT", 2.0),
        "4106902": ("Curitiba", "PR", 3.0),
    }
    for municipality_code, (name, state, scale) in base.items():
        for year, year_scale in ((2019, 1.0), (2024, 1.25)):
            for crop_code, crop_scale in (("40124", 1.0), ("40122", 0.72)):
                area = 1000.0 * scale * crop_scale * year_scale
                production = area * (3.1 + 0.08 * scale)
                fixture_rows.append(
                    {
                        "municipality_code": municipality_code,
                        "municipality_name": name,
                        "state_code": state,
                        "year": year,
                        "crop_code": crop_code,
                        "crop_name": CROPS[crop_code],
                        "planted_area_ha": area,
                        "harvested_area_ha": area,
                        "production_tonnes": production,
                        "yield_kg_ha": production * 1000.0 / area,
                        "production_value_thousand_brl": production * (1.7 + 0.1 * crop_scale),
                    }
                )
    return build_database_from_rows(output_path, fixture_rows, mode="fixture")


def build_database_from_rows(output_path: Path, rows: list[dict[str, Any]], mode: str) -> dict[str, Any]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()
    connection = duckdb.connect(str(output_path))
    try:
        initialize_database(connection)
        inserted = insert_rows(connection, rows)
        finalize_database(connection)
        connection.execute("CHECKPOINT")
    finally:
        connection.close()
    manifest = {
        "mode": mode,
        "table": TABLE_ID,
        "rows": inserted,
        "database_bytes": output_path.stat().st_size,
        "database_sha256": file_sha256(output_path),
    }
    output_path.with_suffix(".manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def _fetch_chunk(client: httpx.Client, year: int, crop_code: str) -> tuple[str, bytes]:
    variables = ",".join(VARIABLES)
    url = f"{SIDRA_BASE_URL}/t/{TABLE_ID}/n6/all/v/{variables}/p/{year}/c782/{crop_code}"
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = client.get(url)
            response.raise_for_status()
            return url, response.content
        except (httpx.HTTPError, OSError) as error:
            last_error = error
            if attempt < 2:
                time.sleep(1.0 + attempt)
    raise RuntimeError(f"SIDRA chunk failed after three attempts: {url}") from last_error


def build_full_database(output_path: Path) -> dict[str, Any]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()
    connection = duckdb.connect(str(output_path))
    chunks: list[dict[str, Any]] = []
    total_rows = 0
    try:
        initialize_database(connection)
        with httpx.Client(timeout=120.0, follow_redirects=True) as client:
            for year in YEARS:
                for crop_code in CROPS:
                    url, content = _fetch_chunk(client, year, crop_code)
                    payload = json.loads(content)
                    rows = normalize_sidra_payload(payload)
                    inserted = insert_rows(connection, rows)
                    digest = hashlib.sha256(content).hexdigest()
                    connection.execute(
                        "INSERT INTO source_chunks VALUES (?, ?, ?, ?, ?, ?, ?, current_timestamp)",
                        [year, crop_code, url, digest, len(content), len(payload) - 1, inserted],
                    )
                    total_rows += inserted
                    chunks.append(
                        {
                            "year": year,
                            "crop_code": crop_code,
                            "sha256": digest,
                            "response_bytes": len(content),
                            "api_rows": len(payload) - 1,
                            "normalized_rows": inserted,
                        }
                    )
        finalize_database(connection)
        connection.execute("CHECKPOINT")
    finally:
        connection.close()
    manifest = {
        "mode": "full",
        "table": TABLE_ID,
        "years": YEARS,
        "crops": CROPS,
        "variables": VARIABLES,
        "rows": total_rows,
        "database_bytes": output_path.stat().st_size,
        "database_sha256": file_sha256(output_path),
        "chunks": chunks,
    }
    output_path.with_suffix(".manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the MAE IBGE PAM DuckDB dataset.")
    parser.add_argument("--output", type=Path, default=Path("../data/agriculture.duckdb"))
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--estimate", action="store_true", help="Print size estimates without downloading data."
    )
    mode.add_argument("--fixture", action="store_true", help="Build a tiny deterministic test database.")
    mode.add_argument("--full", action="store_true", help="Download all 42 approved SIDRA chunks.")
    arguments = parser.parse_args()
    if arguments.estimate:
        print(json.dumps(estimate_dataset(), indent=2))
    elif arguments.fixture:
        print(json.dumps(build_fixture_database(arguments.output.resolve()), indent=2))
    else:
        print(json.dumps(build_full_database(arguments.output.resolve()), indent=2))


if __name__ == "__main__":
    main()
