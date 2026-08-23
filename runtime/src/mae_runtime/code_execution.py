from __future__ import annotations

import ast
import hashlib
import json
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import duckdb
from pydantic import BaseModel, Field

from .dataset import file_sha256
from .temporal_contract import BranchDiagnostic


class CodeExecutionResult(BaseModel):
    status: str
    rows: list[dict[str, Any]] = Field(default_factory=list)
    columns: list[str] = Field(default_factory=list)
    diagnostics: list[BranchDiagnostic] = Field(default_factory=list)
    latency_seconds: float = 0.0
    code_sha256: str
    dataset_sha256: str


def _result(
    *,
    status: str,
    code: str,
    dataset_path: Path,
    started: float,
    rows: list[dict[str, Any]] | None = None,
    columns: list[str] | None = None,
    diagnostics: list[BranchDiagnostic] | None = None,
) -> CodeExecutionResult:
    return CodeExecutionResult(
        status=status,
        rows=rows or [],
        columns=columns or [],
        diagnostics=diagnostics or [],
        latency_seconds=round(time.perf_counter() - started, 4),
        code_sha256=hashlib.sha256(code.encode("utf-8")).hexdigest(),
        dataset_sha256=file_sha256(dataset_path),
    )

def execute_generated_sql(
    dataset_path: Path, code: str, max_rows: int = 100
) -> CodeExecutionResult:
    started = time.perf_counter()
    normalized = code.strip().rstrip(";")
    lowered = re.sub(r"\s+", " ", normalized.lower())
    forbidden = re.compile(
        r"\b(insert|update|delete|drop|alter|create|attach|detach|copy|export|import|"
        r"install|load|pragma|call)\b"
    )
    if (
        not (lowered.startswith("select ") or lowered.startswith("with "))
        or forbidden.search(lowered)
        or ";" in normalized
    ):
        return _result(
            status="rejected",
            code=code,
            dataset_path=dataset_path,
            started=started,
            diagnostics=[
                BranchDiagnostic(
                    code="unsafe_sql",
                    message="Only one read-only SELECT or WITH statement is allowed.",
                )
            ],
        )

    connection = duckdb.connect(str(dataset_path), read_only=True)
    try:
        cursor = connection.execute(
            f"SELECT * FROM ({normalized}) AS bounded_generated_query LIMIT {max_rows + 1}"
        )
        columns = [item[0] for item in cursor.description]
        values = cursor.fetchall()
    except Exception as error:  # noqa: BLE001 - model SQL errors become repair diagnostics.
        return _result(
            status="rejected",
            code=code,
            dataset_path=dataset_path,
            started=started,
            diagnostics=[
                BranchDiagnostic(
                    code="sql_execution_error",
                    message="DuckDB rejected the generated query.",
                    details={"error": str(error)},
                )
            ],
        )
    finally:
        connection.close()

    if len(values) > max_rows:
        return _result(
            status="rejected",
            code=code,
            dataset_path=dataset_path,
            started=started,
            diagnostics=[
                BranchDiagnostic(
                    code="row_limit_exceeded",
                    message="The generated query exceeded the result-row limit.",
                    details={"limit": max_rows},
                )
            ],
        )
    rows = [dict(zip(columns, row, strict=True)) for row in values]
    return _result(
        status="completed",
        code=code,
        dataset_path=dataset_path,
        started=started,
        rows=rows,
        columns=columns,
    )


_ALLOWED_ATTRIBUTES = {"append", "get", "items", "keys", "setdefault", "sort", "values"}
_BLOCKED_NAMES = {
    "__import__",
    "breakpoint",
    "compile",
    "delattr",
    "dir",
    "eval",
    "exec",
    "exit",
    "getattr",
    "globals",
    "help",
    "input",
    "locals",
    "memoryview",
    "object",
    "open",
    "quit",
    "setattr",
    "super",
    "type",
    "vars",
}


def _python_safety_diagnostic(code: str) -> BranchDiagnostic | None:
    try:
        tree = ast.parse(code)
    except SyntaxError as error:
        return BranchDiagnostic(
            code="python_syntax_error",
            message="Generated Python is not syntactically valid.",
            details={"error": str(error)},
        )

    analyze_functions = [
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "analyze"
    ]
    if len(analyze_functions) != 1:
        return BranchDiagnostic(
            code="invalid_python_contract",
            message="Generated Python must define exactly one analyze(rows) function.",
        )

    forbidden_nodes = (
        ast.AsyncFunctionDef,
        ast.ClassDef,
        ast.Delete,
        ast.Global,
        ast.Import,
        ast.ImportFrom,
        ast.Lambda,
        ast.Nonlocal,
        ast.With,
    )
    for node in ast.walk(tree):
        if isinstance(node, forbidden_nodes):
            return BranchDiagnostic(
                code="unsafe_python",
                message=f"Generated Python contains forbidden syntax: {type(node).__name__}.",
            )
        if isinstance(node, ast.Name) and (
            node.id in _BLOCKED_NAMES or node.id.startswith("__")
        ):
            return BranchDiagnostic(
                code="unsafe_python",
                message=f"Generated Python references forbidden name: {node.id}.",
            )
        if isinstance(node, ast.Attribute) and (
            node.attr.startswith("__") or node.attr not in _ALLOWED_ATTRIBUTES
        ):
            return BranchDiagnostic(
                code="unsafe_python",
                message=f"Generated Python references forbidden attribute: {node.attr}.",
            )
    return None


_PYTHON_RUNNER = r"""
import builtins
import json
import math
import resource
import sys

payload = json.loads(sys.stdin.read())
if sys.platform != "darwin":
    memory_bytes = int(payload["memory_mb"]) * 1024 * 1024
    _, address_hard_limit = resource.getrlimit(resource.RLIMIT_AS)
    resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, address_hard_limit))
cpu_seconds = max(1, int(math.ceil(float(payload["timeout_seconds"]))))
resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
allowed_names = (
    "abs", "bool", "dict", "enumerate", "float", "int", "len", "list",
    "max", "min", "range", "round", "set", "sorted", "str", "sum", "tuple", "zip"
)
safe_builtins = {name: getattr(builtins, name) for name in allowed_names}
scope = {"__builtins__": safe_builtins}
exec(compile(payload["code"], "<generated-analysis>", "exec"), scope)
result = scope["analyze"](payload["rows"])
sys.stdout.write(json.dumps(result, allow_nan=False, separators=(",", ":")))
"""


def _load_python_rows(dataset_path: Path) -> list[dict[str, Any]]:
    connection = duckdb.connect(str(dataset_path), read_only=True)
    try:
        cursor = connection.execute(
            """
            SELECT municipality_code, crop_code, crop_name, year, planted_area_ha,
                   harvested_area_ha, production_tonnes, production_value_thousand_brl
            FROM crop_metrics
            ORDER BY crop_code, year, municipality_code
            """
        )
        columns = [item[0] for item in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
    finally:
        connection.close()


def execute_generated_python(
    dataset_path: Path,
    code: str,
    timeout_seconds: int = 10,
    memory_mb: int = 512,
    max_rows: int = 100,
) -> CodeExecutionResult:
    started = time.perf_counter()
    safety_error = _python_safety_diagnostic(code)
    if safety_error:
        return _result(
            status="rejected",
            code=code,
            dataset_path=dataset_path,
            started=started,
            diagnostics=[safety_error],
        )

    payload = json.dumps(
        {
            "code": code,
            "rows": _load_python_rows(dataset_path),
            "timeout_seconds": timeout_seconds,
            "memory_mb": memory_mb,
        },
        separators=(",", ":"),
    )
    try:
        with tempfile.TemporaryDirectory(prefix="mae-python-sandbox-") as workdir:
            completed = subprocess.run(
                [sys.executable, "-I", "-S", "-c", _PYTHON_RUNNER],
                input=payload,
                text=True,
                capture_output=True,
                cwd=workdir,
                env={},
                timeout=timeout_seconds + 1,
                check=False,
            )
    except subprocess.TimeoutExpired:
        return _result(
            status="rejected",
            code=code,
            dataset_path=dataset_path,
            started=started,
            diagnostics=[
                BranchDiagnostic(
                    code="python_timeout",
                    message="Generated Python exceeded its wall-clock limit.",
                )
            ],
        )

    if completed.returncode in (-signal.SIGKILL, -signal.SIGXCPU):
        return _result(
            status="rejected",
            code=code,
            dataset_path=dataset_path,
            started=started,
            diagnostics=[
                BranchDiagnostic(
                    code="python_timeout",
                    message="Generated Python exceeded its CPU or wall-clock limit.",
                )
            ],
        )
    if completed.returncode != 0:
        message = completed.stderr.strip()[-1000:] or "Restricted Python process failed."
        return _result(
            status="rejected",
            code=code,
            dataset_path=dataset_path,
            started=started,
            diagnostics=[
                BranchDiagnostic(
                    code="python_execution_error",
                    message="Restricted Python execution failed.",
                    details={"error": message},
                )
            ],
        )
    if len(completed.stdout.encode("utf-8")) > 5_000_000:
        return _result(
            status="rejected",
            code=code,
            dataset_path=dataset_path,
            started=started,
            diagnostics=[
                BranchDiagnostic(
                    code="python_output_limit",
                    message="Generated Python exceeded its output-size limit.",
                )
            ],
        )

    try:
        values = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        return _result(
            status="rejected",
            code=code,
            dataset_path=dataset_path,
            started=started,
            diagnostics=[
                BranchDiagnostic(
                    code="malformed_python_result",
                    message="Generated Python did not return JSON.",
                    details={"error": str(error)},
                )
            ],
        )
    if (
        not isinstance(values, list)
        or len(values) > max_rows
        or any(not isinstance(row, dict) for row in values)
    ):
        return _result(
            status="rejected",
            code=code,
            dataset_path=dataset_path,
            started=started,
            diagnostics=[
                BranchDiagnostic(
                    code="malformed_python_result",
                    message="analyze(rows) must return a bounded list of objects.",
                    details={"limit": max_rows},
                )
            ],
        )
    columns = list(values[0]) if values else []
    if any(list(row) != columns for row in values):
        return _result(
            status="rejected",
            code=code,
            dataset_path=dataset_path,
            started=started,
            diagnostics=[
                BranchDiagnostic(
                    code="malformed_python_result",
                    message="Every Python result row must have the same ordered columns.",
                )
            ],
        )
    return _result(
        status="completed",
        code=code,
        dataset_path=dataset_path,
        started=started,
        rows=values,
        columns=columns,
    )
