from __future__ import annotations

import asyncio
from typing import Any

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Response, status
from fastapi.responses import HTMLResponse

from .config import get_settings
from .contracts import RunRequest
from .graph import RobustHarness
from .model_client import QwenClient
from .run_store import RunStore
from .security import RuntimeGuard, resolve_artifact

settings = get_settings()
store = RunStore(settings.artifacts_dir)
model = QwenClient(settings)
harness = RobustHarness(model, settings)
background_tasks: set[asyncio.Task[Any]] = set()
guard = RuntimeGuard(
    token=settings.runtime_token,
    require_auth=settings.require_runtime_auth,
    max_concurrent=settings.max_concurrent_runs,
    max_per_window=settings.max_runs_per_window,
    window_seconds=settings.run_window_seconds,
)

app = FastAPI(
    title="MAE Robust Harness Runtime",
    version="0.1.0",
    description="Validated LangGraph agent runtime for the controlled agricultural benchmark.",
)


def _execute_run(run_id: str, prompt: str, agent_prompts: dict[str, str] | None = None) -> None:
    store.update(run_id, status="running")

    def emit(
        node: str,
        event_type: str,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        store.append_event(run_id, node, event_type, message, data)

    try:
        result = harness.run(run_id, prompt, emit, agent_prompts=agent_prompts)
        terminal_failed = result.get("terminal_status") == "failed"
        store.update(
            run_id,
            status="failed" if terminal_failed else "completed",
            result=result,
            error=result.get("failure_reason") if terminal_failed else None,
        )
    except Exception as error:  # noqa: BLE001 - terminal failures are persisted for evaluation.
        emit("runtime", "failed", "Run terminated with an exception.", {"error_type": type(error).__name__})
        store.update(run_id, status="failed", error="The run failed inside the runtime.")


async def _execute_background(run_id: str, prompt: str, agent_prompts: dict[str, str] | None = None) -> None:
    try:
        await asyncio.to_thread(_execute_run, run_id, prompt, agent_prompts)
    finally:
        guard.release()


def authorize(authorization: str | None = Header(default=None)) -> None:
    guard.authenticate(authorization)


@app.get("/agents", dependencies=[Depends(authorize)])
def get_agents() -> list[dict[str, Any]]:
    return list(harness.agents.values())


@app.get("/health", dependencies=[Depends(authorize)])
def health() -> dict[str, Any]:
    try:
        model_status = model.health()
    except Exception:  # noqa: BLE001 - health must not expose upstream details.
        model_status = {"connected": False, "model": settings.model_id, "available": False}
    return {
        "status": "ready" if settings.dataset_path.exists() and model_status.get("available") else "degraded",
        "harness": settings.harness_variant,
        "model": {"model": settings.model_id, "available": bool(model_status.get("available"))},
        "dataset": {"ready": settings.dataset_path.exists()},
        "limits": {"max_concurrent_runs": settings.max_concurrent_runs},
    }


@app.post("/runs", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(authorize)])
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
    if store.count() >= settings.max_stored_runs:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Run retention limit reached.",
        )
    guard.reserve()
    try:
        record = store.create(request.harness, request.prompt, settings.model_id)
    except Exception:
        guard.release()
        raise
    store.append_event(record.run_id, "runtime", "queued", "Run accepted by the robust runtime.")
    task = asyncio.create_task(_execute_background(record.run_id, request.prompt, request.agent_prompts))
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)
    return {
        "run_id": record.run_id,
        "status": "queued",
        "message": "The robust LangGraph run was accepted.",
    }


@app.get("/runs/{run_id}", dependencies=[Depends(authorize)])
def get_run(run_id: str) -> dict[str, Any]:
    try:
        return store.get(run_id).model_dump(mode="json")
    except KeyError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found.") from error


@app.get("/runs/{run_id}/events", dependencies=[Depends(authorize)])
def get_run_events(run_id: str, after: int = 0) -> dict[str, Any]:
    try:
        record = store.get(run_id)
    except KeyError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found.") from error
    events = [event.model_dump(mode="json") for event in record.events if event.sequence > after]
    return {"run_id": run_id, "status": record.status, "events": events}


@app.get("/runs/{run_id}/dashboard.html", response_class=HTMLResponse, dependencies=[Depends(authorize)])
def get_run_dashboard_html(run_id: str) -> HTMLResponse:
    try:
        artifact_path = resolve_artifact(settings.artifacts_dir, run_id, "dashboard.html")
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid artifact path.",
        ) from error
    if not artifact_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Dashboard HTML artifact not found for run {run_id}.",
        )
    return HTMLResponse(content=artifact_path.read_text(encoding="utf-8"))


@app.get("/runs/{run_id}/artifacts/{filename}", dependencies=[Depends(authorize)])
def get_run_artifact(run_id: str, filename: str) -> Response:
    try:
        artifact_path = resolve_artifact(settings.artifacts_dir, run_id, filename)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid artifact path.",
        ) from error
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
