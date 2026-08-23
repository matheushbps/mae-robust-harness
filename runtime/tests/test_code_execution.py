from __future__ import annotations

from pathlib import Path

import pytest

from mae_runtime.code_execution import execute_generated_python, execute_generated_sql
from mae_runtime.dataset import build_fixture_database


@pytest.fixture
def dataset_path(tmp_path: Path) -> Path:
    path = tmp_path / "fixture.duckdb"
    build_fixture_database(path)
    return path


def test_executes_readonly_sql_with_window_function(dataset_path: Path) -> None:
    result = execute_generated_sql(
        dataset_path,
        """
        SELECT crop_code, year,
               lag(production_tonnes) OVER (PARTITION BY crop_code ORDER BY year) AS prior
        FROM national_crop_year
        ORDER BY crop_code, year
        """,
        max_rows=10,
    )
    assert result.status == "completed"
    assert result.columns == ["crop_code", "year", "prior"]
    assert len(result.rows) == 4
    assert result.rows[0]["prior"] is None
    assert len(result.code_sha256) == 64
    assert len(result.dataset_sha256) == 64


@pytest.mark.parametrize(
    ("code", "diagnostic"),
    [
        ("DELETE FROM crop_metrics", "unsafe_sql"),
        ("SELECT 1; SELECT 2", "unsafe_sql"),
        ("INSTALL httpfs", "unsafe_sql"),
        ("SELECT * FROM read_csv_auto('/etc/hosts', header=false)", "unsafe_sql"),
    ],
)
def test_rejects_unsafe_sql(dataset_path: Path, code: str, diagnostic: str) -> None:
    result = execute_generated_sql(dataset_path, code)
    assert result.status == "rejected"
    assert result.diagnostics[0].code == diagnostic


def test_rejects_sql_result_above_limit(dataset_path: Path) -> None:
    result = execute_generated_sql(
        dataset_path,
        "SELECT * FROM crop_metrics CROSS JOIN range(20)",
        max_rows=10,
    )
    assert result.status == "rejected"
    assert result.diagnostics[0].code == "row_limit_exceeded"


def test_executes_restricted_python_analysis(dataset_path: Path) -> None:
    result = execute_generated_python(
        dataset_path,
        """
def analyze(rows):
    totals = {}
    for row in rows:
        key = row["crop_code"]
        totals[key] = totals.get(key, 0.0) + (row["production_tonnes"] or 0.0)
    return [{"crop_code": key, "total": totals[key]} for key in sorted(totals)]
""",
    )
    assert result.status == "completed"
    assert result.columns == ["crop_code", "total"]
    assert len(result.rows) == 2
    assert result.rows[0]["total"] > 0


@pytest.mark.parametrize(
    ("code", "diagnostic"),
    [
        ("import os\ndef analyze(rows): return []", "unsafe_python"),
        ("def analyze(rows): return open('/etc/passwd').read()", "unsafe_python"),
        ("def analyze(rows): return rows[0].__class__", "unsafe_python"),
    ],
)
def test_rejects_unsafe_python(dataset_path: Path, code: str, diagnostic: str) -> None:
    result = execute_generated_python(dataset_path, code)
    assert result.status == "rejected"
    assert result.diagnostics[0].code == diagnostic


def test_terminates_python_timeout(dataset_path: Path) -> None:
    result = execute_generated_python(
        dataset_path,
        "def analyze(rows):\n    while True:\n        pass",
        timeout_seconds=1,
    )
    assert result.status == "rejected"
    assert result.diagnostics[0].code == "python_timeout"
