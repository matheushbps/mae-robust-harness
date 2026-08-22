from __future__ import annotations

import asyncio
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Response, status
from fastapi.responses import HTMLResponse

from .config import get_settings
from .contracts import RunRequest
from .dataset import file_sha256
from .graph import RobustHarness
from .model_client import QwenClient
from .run_store import RunStore

settings = get_settings()
store = RunStore(settings.artifacts_dir)
model = QwenClient(settings)
harness = RobustHarness(model, settings)
background_tasks: set[asyncio.Task[Any]] = set()

app = FastAPI(
    title="MAE Robust Harness Runtime",
    version="0.1.0",
    description="Validated LangGraph agent runtime for the controlled agricultural benchmark.",
)


def _execute_run(run_id: str, prompt: str) -> None:
    store.update(run_id, status="running")

    def emit(
        node: str,
        event_type: str,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        store.append_event(run_id, node, event_type, message, data)

    try:
        result = harness.run(run_id, prompt, emit)
        terminal_failed = result.get("terminal_status") == "failed"
        store.update(
            run_id,
            status="failed" if terminal_failed else "completed",
            result=result,
            error=result.get("failure_reason") if terminal_failed else None,
        )
    except Exception as error:  # noqa: BLE001 - terminal failures are persisted for evaluation.
        emit("runtime", "failed", "Run terminated with an exception.", {"error": str(error)})
        store.update(run_id, status="failed", error=str(error))


async def _execute_background(run_id: str, prompt: str) -> None:
    await asyncio.to_thread(_execute_run, run_id, prompt)


@app.get("/health")
def health() -> dict[str, Any]:
    try:
        model_status = model.health()
    except Exception as error:  # noqa: BLE001 - health reports upstream failures without crashing.
        model_status = {"connected": False, "model": settings.model_id, "error": str(error)}
    return {
        "status": "ready" if settings.dataset_path.exists() and model_status.get("available") else "degraded",
        "harness": settings.harness_variant,
        "model": model_status,
        "dataset": {
            "ready": settings.dataset_path.exists(),
            "path": str(settings.dataset_path),
            "sha256": file_sha256(settings.dataset_path) if settings.dataset_path.exists() else None,
        },
        "checkpoint": {"backend": "sqlite", "path": str(settings.checkpoint_path)},
        "graph": {"max_repair_attempts": settings.max_repair_attempts},
    }


@app.post("/runs", status_code=status.HTTP_202_ACCEPTED)
async def create_run(request: RunRequest) -> dict[str, Any]:
    if request.harness != settings.harness_variant:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"This runtime only accepts the {settings.harness_variant} harness.",
        )
    if not settings.dataset_path.exists():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The DuckDB dataset has not been built yet.",
        )
    record = store.create(request.harness, request.prompt, settings.model_id)
    store.append_event(record.run_id, "runtime", "queued", "Run accepted by the robust runtime.")
    task = asyncio.create_task(_execute_background(record.run_id, request.prompt))
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)
    return {
        "run_id": record.run_id,
        "status": "queued",
        "message": "The robust LangGraph run was accepted.",
    }


@app.get("/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    try:
        return store.get(run_id).model_dump(mode="json")
    except KeyError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found.") from error


@app.get("/runs/{run_id}/events")
def get_run_events(run_id: str, after: int = 0) -> dict[str, Any]:
    try:
        record = store.get(run_id)
    except KeyError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found.") from error
    events = [event.model_dump(mode="json") for event in record.events if event.sequence > after]
    return {"run_id": run_id, "status": record.status, "events": events}


@app.get("/runs/{run_id}/dashboard.html", response_class=HTMLResponse)
def get_run_dashboard_html(run_id: str) -> HTMLResponse:
    artifact_path = settings.artifacts_dir / run_id / "dashboard.html"
    if not artifact_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Dashboard HTML artifact not found for run {run_id}.",
        )
    return HTMLResponse(content=artifact_path.read_text(encoding="utf-8"))


@app.get("/runs/{run_id}/artifacts/{filename}")
def get_run_artifact(run_id: str, filename: str) -> Response:
    if ".." in filename or filename.startswith("/") or "\\" in filename:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid filename.")
    artifact_path = settings.artifacts_dir / run_id / filename
    if not artifact_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Artifact {filename} not found for run {run_id}.",
        )
    if filename.endswith(".html"):
        return HTMLResponse(content=artifact_path.read_text(encoding="utf-8"))
    if filename.endswith(".json"):
        return Response(content=artifact_path.read_text(encoding="utf-8"), media_type="application/json")
    return Response(content=artifact_path.read_bytes())


def main() -> None:
    uvicorn.run(app, host=settings.runtime_host, port=settings.runtime_port)


if __name__ == "__main__":
    main()
