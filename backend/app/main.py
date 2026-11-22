"""FastAPI entry point: CORS, BYOK config, SSE streaming agent, HITL confirm, and Undo APIs."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from sse_starlette.sse import EventSourceResponse

from app.agent.graph import agent_runner
from app.config import mask_api_key, runtime_config, settings
from app.sandbox.executor import SandboxExecutor
from app.sandbox.guard import SandboxGuard, SandboxSecurityError
from app.sandbox.readers import scan_directory_tree, seed_demo_workspace
from app.schemas import (
    AgentRunRequest,
    ConfirmPlanRequest,
    ConfirmPlanResponse,
    ExecutionBatchRecord,
    FileTreeNode,
    SeedDemoResponse,
    UndoResponse,
    WorkspaceConfigResponse,
    WorkspaceConfigUpdateRequest,
    WorkspaceDialogResponse,
)


app = FastAPI(
    title="SafeWorkspace Agent API",
    description=(
        "Local sandbox file organization agent featuring L1-L3 Progressive Disclosure, "
        "Strict Sandbox Guard, LangGraph Human-in-the-Loop Interrupts, and Transactional Undo."
    ),
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _current_guard() -> SandboxGuard:
    try:
        return SandboxGuard(runtime_config.sandbox_root)
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _build_config_response() -> WorkspaceConfigResponse:
    root = runtime_config.sandbox_root
    exists = root.exists() and root.is_dir()
    file_count = 0
    dir_count = 0
    total_size = 0

    if exists:
        try:
            guard = SandboxGuard(root)
            scan = scan_directory_tree(guard, ".", max_depth=6, max_entries=1500)
            file_count = scan.total_files
            dir_count = scan.total_dirs
            total_size = scan.total_size_bytes
        except Exception:
            pass

    key = runtime_config.api_key
    return WorkspaceConfigResponse(
        sandbox_root=str(root),
        sandbox_exists=exists,
        model_name=runtime_config.model_name,
        base_url=runtime_config.base_url,
        has_api_key=bool(key.strip()),
        api_key_masked=mask_api_key(key),
        file_count=file_count,
        dir_count=dir_count,
        total_size_bytes=total_size,
        recent_workspaces=runtime_config.recent_workspaces,
        persisted=runtime_config.has_persisted_config,
    )


@app.get("/api/health")
async def health_check() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "safe-workspace-agent",
        "sandbox_root": str(runtime_config.sandbox_root),
    }


@app.get("/api/config", response_model=WorkspaceConfigResponse)
async def get_config() -> WorkspaceConfigResponse:
    return _build_config_response()


@app.put("/api/config", response_model=WorkspaceConfigResponse)
async def update_config(payload: WorkspaceConfigUpdateRequest) -> WorkspaceConfigResponse:
    try:
        runtime_config.update(
            sandbox_root=payload.sandbox_root,
            api_key=payload.api_key,
            base_url=payload.base_url,
            model_name=payload.model_name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _build_config_response()


_TK_DIALOG_SCRIPT = """
import json
import sys
import tkinter as tk
from tkinter import filedialog

initial_dir = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else None
root = tk.Tk()
root.withdraw()
try:
    root.attributes("-topmost", True)
except Exception:
    pass
root.update()
selected = filedialog.askdirectory(
    parent=root,
    title="选择 SafeWorkspace 沙盒工作区目录",
    initialdir=initial_dir,
    mustexist=True,
)
root.destroy()
print(json.dumps({"selected_path": selected or ""}, ensure_ascii=False))
"""


def _run_tk_folder_dialog(initial_dir: str) -> str:
    """Launch tkinter folder picker inside an isolated subprocess to avoid blocking or GUI-thread issues."""
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    completed = subprocess.run(
        [sys.executable, "-c", _TK_DIALOG_SCRIPT, initial_dir],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        env=env,
        check=False,
    )
    if completed.returncode != 0:
        err_msg = (completed.stderr or "").strip() or f"Dialog process exited with code {completed.returncode}"
        raise RuntimeError(err_msg)

    lines = [line.strip() for line in (completed.stdout or "").splitlines() if line.strip()]
    if not lines:
        return ""
    payload = json.loads(lines[-1])
    return str(payload.get("selected_path") or "").strip()


@app.post("/api/workspace/select-dialog", response_model=WorkspaceDialogResponse)
async def select_workspace_dialog() -> WorkspaceDialogResponse:
    """Open native OS folder picker in an isolated subprocess and validate against SandboxGuard."""
    initial_dir = str(runtime_config.sandbox_root) if runtime_config.sandbox_root.exists() else ""
    try:
        selected_raw = await asyncio.to_thread(_run_tk_folder_dialog, initial_dir)
    except subprocess.TimeoutExpired:
        return WorkspaceDialogResponse(
            selected_path=None,
            cancelled=True,
            valid=False,
            error="文件夹选择窗口超时未响应，请重试。",
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"无法打开系统文件夹选择对话框（请确认当前运行环境支持图形界面与 tkinter）：{exc}",
        ) from exc

    if not selected_raw:
        return WorkspaceDialogResponse(
            selected_path=None,
            cancelled=True,
            valid=False,
            error=None,
        )

    try:
        validated = SandboxGuard.validate_sandbox_root(selected_raw, must_exist=True)
    except (SandboxSecurityError, FileNotFoundError, NotADirectoryError) as exc:
        return WorkspaceDialogResponse(
            selected_path=selected_raw,
            cancelled=False,
            valid=False,
            error=str(exc),
        )

    return WorkspaceDialogResponse(
        selected_path=str(validated),
        cancelled=False,
        valid=True,
        error=None,
    )


@app.get("/api/workspace/tree", response_model=FileTreeNode)
async def get_workspace_tree(max_depth: int = 6) -> FileTreeNode:
    guard = _current_guard()
    scan = scan_directory_tree(guard, ".", max_depth=max_depth, max_entries=1500)
    return scan.tree


@app.post("/api/workspace/seed-demo", response_model=SeedDemoResponse)
async def seed_demo_files() -> SeedDemoResponse:
    """Safely populate realistic messy sample files inside SANDBOX_ROOT for quick testing."""
    guard = _current_guard()
    created, skipped = seed_demo_workspace(guard)
    scan = scan_directory_tree(guard, ".", max_depth=6, max_entries=1500)
    return SeedDemoResponse(
        created_files=created,
        skipped_files=skipped,
        updated_tree=scan.tree,
    )


@app.post("/api/agent/run/stream")
async def stream_agent_run(req: AgentRunRequest, request: Request) -> EventSourceResponse:
    """Stream L1/L2/L3 progressive disclosure trace events & LangGraph interrupt plans via SSE."""
    # Ensure sandbox root is valid before opening stream
    guard = _current_guard()
    override = req.llm_config

    async def event_generator():
        async for event in agent_runner.stream_run(
            instruction=req.instruction,
            thread_id=req.thread_id,
            sandbox_root=str(guard.root),
            api_key_override=override.api_key if override else None,
            base_url_override=override.base_url if override else None,
            model_name_override=override.model_name if override else None,
        ):
            if await request.is_disconnected():
                break
            yield {
                "event": event["type"],
                "id": event["event_id"],
                "data": json.dumps(event, ensure_ascii=False),
            }

    return EventSourceResponse(event_generator(), ping=15)


@app.post("/api/agent/confirm", response_model=ConfirmPlanResponse)
@app.post("/api/workspace/execute", response_model=ConfirmPlanResponse)
async def confirm_agent_plan(req: ConfirmPlanRequest) -> ConfirmPlanResponse:
    """Human-in-the-Loop checkpoint resume: execute approved (and optionally user-edited) operations or cancel cleanly."""
    try:
        status, message, batch, sandbox_root = await agent_runner.confirm_plan(
            thread_id=req.thread_id,
            plan_id=req.plan_id,
            approved=req.approved,
            approved_operation_ids=req.approved_operation_ids,
            operations=[op.model_dump() for op in req.operations] if req.operations is not None else None,
            user_comment=req.user_comment,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SandboxSecurityError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (ValueError, FileNotFoundError, PermissionError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    guard = SandboxGuard(sandbox_root)
    updated_tree = scan_directory_tree(guard, ".", max_depth=6, max_entries=1500).tree

    return ConfirmPlanResponse(
        thread_id=req.thread_id,
        status=status,  # type: ignore[arg-type]
        message=message,
        batch=batch,
        updated_tree=updated_tree,
    )


@app.get("/api/history", response_model=list[ExecutionBatchRecord])
async def list_execution_history() -> list[ExecutionBatchRecord]:
    guard = _current_guard()
    executor = SandboxExecutor(guard)
    return executor.list_batches()


@app.post("/api/history/{batch_id}/undo", response_model=UndoResponse)
async def undo_execution_batch(batch_id: str) -> UndoResponse:
    guard = _current_guard()
    executor = SandboxExecutor(guard)
    try:
        reverted = executor.undo_batch(batch_id)
    except SandboxSecurityError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValueError, FileExistsError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    updated_tree = scan_directory_tree(guard, ".", max_depth=6, max_entries=1500).tree
    return UndoResponse(
        batch_id=batch_id,
        status="reverted",
        message=f"已成功撤回批次 `{batch_id}`，本地沙盒已恢复至执行前状态。",
        batch=reverted,
        updated_tree=updated_tree,
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=True,
    )
