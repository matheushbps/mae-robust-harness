from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel

RUNTIME_ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _path_from_env(name: str, default: Path) -> Path:
    raw = os.getenv(name)
    if not raw:
        return default.resolve()
    path = Path(raw).expanduser()
    return (path if path.is_absolute() else (RUNTIME_ROOT / path)).resolve()


class Settings(BaseModel):
    harness_variant: str = "robust"
    runtime_host: str
    runtime_port: int
    model_base_url: str
    model_id: str
    model_api_key: str | None
    model_timeout_seconds: float
    max_completion_tokens: int
    temperature: float
    dataset_path: Path
    artifacts_dir: Path
    checkpoint_path: Path
    numeric_tolerance: float
    max_repair_attempts: int
    runtime_token: str | None = None
    require_runtime_auth: bool = False
    max_concurrent_runs: int = 2
    max_runs_per_window: int = 10
    run_window_seconds: int = 60
    max_stored_runs: int = 200


def get_settings() -> Settings:
    _load_env_file(RUNTIME_ROOT / ".env")
    _load_env_file(REPOSITORY_ROOT / ".env.local")
    runtime_host = os.getenv("RUNTIME_HOST", "127.0.0.1")
    runtime_token = os.getenv("MAE_RUNTIME_TOKEN")
    is_loopback = runtime_host in {"127.0.0.1", "localhost", "::1"}
    require_runtime_auth = bool(runtime_token) or not is_loopback
    if require_runtime_auth and not runtime_token:
        raise RuntimeError("MAE_RUNTIME_TOKEN is required when RUNTIME_HOST is not loopback.")
    return Settings(
        runtime_host=runtime_host,
        runtime_port=int(os.getenv("RUNTIME_PORT", "8788")),
        model_base_url=os.getenv("MODEL_BASE_URL", "http://127.0.0.1:1234/v1").rstrip("/"),
        model_id=os.getenv("MODEL_ID", "qwen/qwen3.6-35b-a3b"),
        model_api_key=os.getenv("MODEL_API_KEY"),
        model_timeout_seconds=float(os.getenv("MODEL_TIMEOUT_SECONDS", "180")),
        max_completion_tokens=int(os.getenv("MAX_COMPLETION_TOKENS", "8192")),
        temperature=float(os.getenv("MODEL_TEMPERATURE", "0")),
        dataset_path=_path_from_env("DATASET_PATH", REPOSITORY_ROOT / "data/agriculture.duckdb"),
        artifacts_dir=_path_from_env("ARTIFACTS_DIR", REPOSITORY_ROOT / "outputs/runs"),
        checkpoint_path=_path_from_env("CHECKPOINT_PATH", REPOSITORY_ROOT / "outputs/checkpoints.sqlite"),
        numeric_tolerance=float(os.getenv("NUMERIC_TOLERANCE", "0.000001")),
        max_repair_attempts=int(os.getenv("MAX_REPAIR_ATTEMPTS", "4")),
        runtime_token=runtime_token,
        require_runtime_auth=require_runtime_auth,
        max_concurrent_runs=int(os.getenv("MAX_CONCURRENT_RUNS", "2")),
        max_runs_per_window=int(os.getenv("MAX_RUNS_PER_WINDOW", "10")),
        run_window_seconds=int(os.getenv("RUN_WINDOW_SECONDS", "60")),
        max_stored_runs=int(os.getenv("MAX_STORED_RUNS", "200")),
    )
