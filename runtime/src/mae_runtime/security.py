from __future__ import annotations

import hmac
import re
import threading
import time
from collections import deque
from pathlib import Path

RUN_ID_PATTERN = re.compile(r"^[a-f0-9]{16}$")
ALLOWED_ARTIFACTS = frozenset({"dashboard.html", "dashboard.json"})


def resolve_artifact(root: Path, run_id: str, filename: str) -> Path:
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError("Invalid run id.")
    if filename not in ALLOWED_ARTIFACTS:
        raise ValueError("Invalid artifact name.")
    root = root.resolve()
    path = (root / run_id / filename).resolve()
    if root not in path.parents:
        raise ValueError("Artifact path escapes the runtime root.")
    return path


class RuntimeGuard:
    def __init__(
        self,
        *,
        token: str | None,
        require_auth: bool,
        max_concurrent: int,
        max_per_window: int,
        window_seconds: int,
    ) -> None:
        self.token = token
        self.require_auth = require_auth
        self.max_concurrent = max(1, max_concurrent)
        self.max_per_window = max(1, max_per_window)
        self.window_seconds = max(1, window_seconds)
        self._active = 0
        self._timestamps: deque[float] = deque()
        self._lock = threading.Lock()

    def authenticate(self, authorization: str | None) -> None:
        if not self.require_auth:
            return
        expected = f"Bearer {self.token}" if self.token else ""
        if not authorization or not hmac.compare_digest(authorization, expected):
            from fastapi import HTTPException, status

            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required.")

    def reserve(self) -> None:
        now = time.monotonic()
        with self._lock:
            while self._timestamps and now - self._timestamps[0] >= self.window_seconds:
                self._timestamps.popleft()
            if self._active >= self.max_concurrent:
                self._raise_limit("Too many runs are already in progress.")
            if len(self._timestamps) >= self.max_per_window:
                self._raise_limit("Run rate limit exceeded; try again later.")
            self._active += 1
            self._timestamps.append(now)

    def release(self) -> None:
        with self._lock:
            self._active = max(0, self._active - 1)

    @staticmethod
    def _raise_limit(detail: str) -> None:
        from fastapi import HTTPException, status

        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=detail)
