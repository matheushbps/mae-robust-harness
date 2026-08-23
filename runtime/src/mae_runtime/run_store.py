from __future__ import annotations

import hashlib
import json
import threading
import uuid
from pathlib import Path
from typing import Any

from .contracts import RunEvent, RunRecord, utc_now


class RunStore:
    def __init__(self, artifacts_dir: Path) -> None:
        self._artifacts_dir = artifacts_dir
        self._runs: dict[str, RunRecord] = {}
        self._lock = threading.RLock()

    def create(self, harness: str, prompt: str, model_id: str) -> RunRecord:
        with self._lock:
            run_id = uuid.uuid4().hex[:16]
            record = RunRecord(
                run_id=run_id,
                harness=harness,
                prompt_hash=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                model_id=model_id,
            )
            self._runs[run_id] = record
            self._persist(record)
            return record.model_copy(deep=True)

    def get(self, run_id: str) -> RunRecord:
        with self._lock:
            if run_id not in self._runs:
                raise KeyError(run_id)
            return self._runs[run_id].model_copy(deep=True)

    def count(self) -> int:
        with self._lock:
            return len(self._runs)

    def update(self, run_id: str, **changes: Any) -> RunRecord:
        with self._lock:
            record = self._runs[run_id]
            for key, value in changes.items():
                setattr(record, key, value)
            record.updated_at = utc_now()
            self._persist(record)
            return record.model_copy(deep=True)

    def append_event(
        self,
        run_id: str,
        node: str,
        event_type: str,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> RunEvent:
        with self._lock:
            record = self._runs[run_id]
            event = RunEvent(
                sequence=len(record.events) + 1,
                node=node,
                event_type=event_type,
                message=message,
                data=data or {},
            )
            record.events.append(event)
            record.updated_at = utc_now()
            self._persist(record)
            return event.model_copy(deep=True)

    def _persist(self, record: RunRecord) -> None:
        run_dir = self._artifacts_dir / record.run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        target = run_dir / "run.json"
        temporary = run_dir / "run.json.tmp"
        temporary.write_text(
            json.dumps(record.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(target)
