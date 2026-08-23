from pathlib import Path

import pytest
from fastapi import HTTPException

from mae_runtime.security import RuntimeGuard, resolve_artifact


def test_artifact_resolution_rejects_traversal(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        resolve_artifact(tmp_path, "..", "checkpoints.sqlite")
    with pytest.raises(ValueError):
        resolve_artifact(tmp_path, "a" * 16, "../../checkpoints.sqlite")


def test_runtime_guard_requires_token_and_limits_concurrency() -> None:
    guard = RuntimeGuard(
        token="secret", require_auth=True, max_concurrent=1, max_per_window=5, window_seconds=60
    )
    with pytest.raises(HTTPException):
        guard.authenticate("Bearer wrong")
    guard.authenticate("Bearer secret")
    guard.reserve()
    with pytest.raises(HTTPException):
        guard.reserve()
    guard.release()


def test_runtime_guard_allows_loopback_without_token() -> None:
    guard = RuntimeGuard(token=None, require_auth=False, max_concurrent=1, max_per_window=1, window_seconds=60)
    guard.authenticate(None)
