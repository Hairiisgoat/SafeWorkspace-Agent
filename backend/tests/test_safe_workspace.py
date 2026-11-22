"""Comprehensive test suite for SandboxGuard, L1-L3 readers, Transactional Executor, LangGraph HITL & FastAPI."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, AIMessageChunk

from app.agent.graph import SafeWorkspaceAgentRunner
from app.config import RuntimeConfigStore, Settings, runtime_config, settings, validate_safe_sandbox_root
from app.main import app
from app.sandbox.executor import SandboxExecutor, stage_organization_plan
from app.sandbox.guard import SandboxGuard, SandboxSecurityError
from app.sandbox.readers import inspect_file_head, read_file_deep, scan_directory_tree, seed_demo_workspace
from app.schemas import ProposedOperationInput


@pytest.fixture(autouse=True)
def _isolate_local_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent API tests from overwriting a user's real backend/.local_config.json."""
    monkeypatch.setattr(runtime_config, "_local_config_path", tmp_path / ".test_local_config.json")


# ---------------------------------------------------------------------------
# 1. Strict SandboxGuard Security Tests
# ---------------------------------------------------------------------------


def test_sandbox_guard_blocks_traversal_and_protected_paths(tmp_path: Path) -> None:
    sandbox = tmp_path / "workspace"
    sandbox.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("TOP SECRET", encoding="utf-8")

    guard = SandboxGuard(sandbox)

    attack_paths = [
        "../secret.txt",
        "..\\secret.txt",
        "subdir/../../secret.txt",
        "/etc/passwd",
        "C:\\Windows\\System32\\drivers\\etc\\hosts",
        "\\\\server\\share\\file.txt",
        "//server/share/file.txt",
        "bad\x00file.txt",
        ".agent_history/batch_001.json",
        "nested/.agent_history/x.json",
        ".git/config",
        ".",
        "",
    ]

    for attack in attack_paths:
        with pytest.raises(SandboxSecurityError):
            guard.resolve_safe_path(attack, allow_root=False)

    # Reserved Windows names & ADS blocked on target validation
    for bad_target in ["CON.txt", "NUL", "docs/file.txt:ads", "docs/bad<name>.txt"]:
        with pytest.raises(SandboxSecurityError):
            guard.resolve_safe_path(bad_target, allow_root=False, validate_target_names=True)

    # Safe paths allowed
    safe = guard.resolve_safe_path("docs/sub/../report.md", allow_root=False, validate_target_names=True)
    assert guard.to_relative_posix(safe) == "docs/report.md"


def test_refuses_os_root_as_sandbox() -> None:
    root_dir = Path("/").resolve()
    with pytest.raises(ValueError):
        validate_safe_sandbox_root(root_dir)


# ---------------------------------------------------------------------------
# 2. L1 / L2 / L3 Progressive Disclosure Tests
# ---------------------------------------------------------------------------


def test_progressive_disclosure_l1_l2_l3(tmp_path: Path) -> None:
    sandbox = tmp_path / "ws"
    sandbox.mkdir()
    guard = SandboxGuard(sandbox)

    # Create internal protected dirs that L1 must skip
    (sandbox / ".agent_history").mkdir()
    (sandbox / ".agent_history" / "batch_x.json").write_text("{}", encoding="utf-8")
    (sandbox / ".git").mkdir()
    (sandbox / ".git" / "HEAD").write_text("ref: refs/heads/main", encoding="utf-8")

    # Create multi-line text file & binary file
    long_lines = "\n".join(f"Line {i:03d}: Project Phoenix telemetry data" for i in range(1, 121))
    (sandbox / "notes.txt").write_text(long_lines, encoding="utf-8")
    (sandbox / "blob.bin").write_bytes(b"\x00\x01\x02\x89PNG\x00\xff\xfe" * 20)

    # L1 scan
    l1 = scan_directory_tree(guard, ".", max_depth=4)
    assert l1.total_files == 2
    assert l1.total_dirs == 0
    names = {e["path"] for e in l1.flat_entries}
    assert names == {"notes.txt", "blob.bin"}

    # L2 head probe (text)
    l2_text = inspect_file_head(guard, "notes.txt", max_lines=10, max_chars=800)
    assert l2_text.file_type == "text"
    assert l2_text.truncated is True
    assert "Line 001:" in l2_text.preview
    assert "Line 010:" in l2_text.preview
    assert "Line 050:" not in l2_text.preview

    # L2 head probe (binary)
    l2_bin = inspect_file_head(guard, "blob.bin")
    assert l2_bin.file_type == "binary"
    assert "Binary file" in l2_bin.preview

    # L3 deep read
    l3 = read_file_deep(guard, "notes.txt", start_line=50, max_lines=20)
    assert l3.start_line == 50
    assert l3.end_line == 69
    assert l3.total_lines == 120
    assert "Line 050:" in l3.content
    assert "Line 069:" in l3.content


# ---------------------------------------------------------------------------
# 3. Staging, Partial Execution, Mid-Batch Auto-Rollback & One-Click Undo
# ---------------------------------------------------------------------------


def test_transactional_execute_partial_and_undo(tmp_path: Path) -> None:
    sandbox = tmp_path / "ws_exec"
    sandbox.mkdir()
    guard = SandboxGuard(sandbox)
    seed_demo_workspace(guard)

    plan = stage_organization_plan(
        guard=guard,
        thread_id="t_unit",
        summary="Organize invoices and notes",
        proposed_ops=[
            ProposedOperationInput(
                op_type="mkdir",
                target="Finance",
                reason="Create finance directory",
                probe_evidence="L2",
            ),
            ProposedOperationInput(
                op_type="move",
                source="scan_8821.txt",
                target="Finance/2026-10_CloudScale_Invoice.txt",
                reason="Tax invoice discovered via L2 head probe",
                probe_evidence="L2",
                category="Finance",
            ),
            ProposedOperationInput(
                op_type="archive",
                source="old_2024_onboarding_checklist.md",
                target="Archive/old_2024_onboarding_checklist.md",
                reason="Deprecated 2024 onboarding doc",
                probe_evidence="L2",
                category="Archive",
            ),
        ],
    )

    assert len(plan.operations) == 3
    assert plan.stats["affected_files"] == 2
    # Zero writes during staging!
    assert (sandbox / "scan_8821.txt").exists()
    assert not (sandbox / "Finance").exists()

    # Execute ONLY first 2 operations (leave archive unchecked!)
    executor = SandboxExecutor(guard)
    approved_ids = [plan.operations[0].id, plan.operations[1].id]
    batch = executor.execute_plan(plan, approved_ids)

    assert batch.status == "executed"
    assert batch.approved_count == 2
    assert batch.skipped_count == 1
    assert not (sandbox / "scan_8821.txt").exists()
    assert (sandbox / "Finance" / "2026-10_CloudScale_Invoice.txt").exists()
    # Unchecked archive op must NOT have executed
    assert (sandbox / "old_2024_onboarding_checklist.md").exists()
    assert not (sandbox / "Archive").exists()

    # Manifest saved in .agent_history/
    batches = executor.list_batches()
    assert len(batches) == 1
    assert batches[0].batch_id == batch.batch_id

    # One-click Undo
    reverted = executor.undo_batch(batch.batch_id)
    assert reverted.status == "reverted"
    assert (sandbox / "scan_8821.txt").exists()
    assert not (sandbox / "Finance" / "2026-10_CloudScale_Invoice.txt").exists()
    assert not (sandbox / "Finance").exists()  # Empty auto-created folder cleaned up!

    # Double undo rejected
    with pytest.raises(ValueError):
        executor.undo_batch(batch.batch_id)


def test_mid_batch_failure_triggers_automatic_rollback(tmp_path: Path) -> None:
    sandbox = tmp_path / "ws_rb"
    sandbox.mkdir()
    (sandbox / "a.txt").write_text("file A", encoding="utf-8")
    (sandbox / "b.txt").write_text("file B", encoding="utf-8")

    guard = SandboxGuard(sandbox)
    plan = stage_organization_plan(
        guard=guard,
        thread_id="t_rb",
        summary="Move two files",
        proposed_ops=[
            ProposedOperationInput(op_type="move", source="a.txt", target="Folder/a.txt", reason="Move A"),
            ProposedOperationInput(op_type="move", source="b.txt", target="Folder/b.txt", reason="Move B"),
        ],
    )

    # Simulate external conflict appearing right before execution at Folder/b.txt
    (sandbox / "Folder").mkdir()
    (sandbox / "Folder" / "b.txt").write_text("EXTERNAL CONFLICT", encoding="utf-8")

    executor = SandboxExecutor(guard)
    batch = executor.execute_plan(plan, [op.id for op in plan.operations])

    assert batch.status == "rolled_back_on_error"
    # Step 1 (a.txt -> Folder/a.txt) MUST have been rolled back!
    assert (sandbox / "a.txt").exists()
    assert not (sandbox / "Folder" / "a.txt").exists()
    # Existing external file untouched
    assert (sandbox / "Folder" / "b.txt").read_text(encoding="utf-8") == "EXTERNAL CONFLICT"


# ---------------------------------------------------------------------------
# 4. End-to-End LangGraph State Machine + Interrupt + Resume + Security Block
# ---------------------------------------------------------------------------


class _ScriptedMockLLM:
    """Deterministic scripted LLM simulating L1 -> security trap + L2 -> L3 -> propose plan."""

    def __init__(self) -> None:
        self.turn = 0

    def bind_tools(self, tools: list[Any]) -> "_ScriptedMockLLM":
        return self

    async def astream(self, messages: list[Any]):
        self.turn += 1
        if self.turn == 1:
            yield AIMessageChunk(
                content="首先执行 L1 目录树扫描以了解工作区结构。",
                tool_calls=[
                    {
                        "name": "scan_directory_tree",
                        "args": {"subpath": ".", "max_depth": 4},
                        "id": "call_l1",
                    }
                ],
            )
        elif self.turn == 2:
            # Includes an intentional traversal attempt to verify SandboxGuard + SSE security_blocked!
            yield AIMessageChunk(
                content="对模糊文件执行 L2 探针检查。",
                tool_calls=[
                    {
                        "name": "inspect_file_head",
                        "args": {"file_path": "../../etc/passwd"},
                        "id": "call_sec_trap",
                    },
                    {
                        "name": "inspect_file_head",
                        "args": {"file_path": "scan_8821.txt", "max_lines": 25},
                        "id": "call_l2",
                    },
                ],
            )
        elif self.turn == 3:
            yield AIMessageChunk(
                content="对会议记录文件进行 L3 深度确认。",
                tool_calls=[
                    {
                        "name": "read_file_deep",
                        "args": {"file_path": "IMG_2026_001.txt", "start_line": 1, "max_lines": 50},
                        "id": "call_l3",
                    }
                ],
            )
        else:
            yield AIMessageChunk(
                content="已收集足够证据，正在生成 Dry-Run 整理方案供人工审批。",
                tool_calls=[
                    {
                        "name": "propose_organization_plan",
                        "args": {
                            "summary": "按财务发票与项目会议文档建立规范目录",
                            "operations": [
                                {
                                    "op_type": "mkdir",
                                    "target": "Finance",
                                    "reason": "创建财务票据目录",
                                    "probe_evidence": "L2",
                                    "category": "Finance",
                                },
                                {
                                    "op_type": "move",
                                    "source": "scan_8821.txt",
                                    "target": "Finance/2026-10_CloudScale_Invoice.txt",
                                    "reason": "L2 探针识别为 CloudScale 10 月税务发票",
                                    "probe_evidence": "L2",
                                    "category": "Finance",
                                },
                                {
                                    "op_type": "move",
                                    "source": "IMG_2026_001.txt",
                                    "target": "Meetings/Phoenix_Q4_Architecture_Notes.txt",
                                    "reason": "L3 确认内容为 Project Phoenix Q4 架构会议纪要",
                                    "probe_evidence": "L3",
                                    "category": "Meetings",
                                },
                            ],
                        },
                        "id": "call_plan",
                    }
                ],
            )


def test_langgraph_progressive_disclosure_interrupt_and_resume(tmp_path: Path) -> None:
    sandbox = tmp_path / "ws_graph"
    sandbox.mkdir()
    guard = SandboxGuard(sandbox)
    seed_demo_workspace(guard)

    runner = SafeWorkspaceAgentRunner(settings)
    mock_llm = _ScriptedMockLLM()

    async def _run() -> list[dict[str, Any]]:
        collected: list[dict[str, Any]] = []
        async for evt in runner.stream_run(
            instruction="请帮我整理当前目录",
            thread_id="thread_e2e_001",
            sandbox_root=str(sandbox),
            llm_override=mock_llm,
        ):
            collected.append(evt)
        return collected

    events = asyncio.run(_run())
    event_types = [e["type"] for e in events]

    assert "run_started" in event_types
    assert "security_blocked" in event_types
    assert "plan_interrupt" in event_types
    assert event_types[-1] == "done"

    # Verify L1, L2, L3, PLAN probe levels all emitted
    probe_levels = {e.get("probe_level") for e in events if e.get("probe_level")}
    assert {"L1", "L2", "L3", "PLAN"}.issubset(probe_levels)

    # Extract staged plan from plan_interrupt event
    interrupt_evt = next(e for e in events if e["type"] == "plan_interrupt")
    plan = interrupt_evt["detail"]
    assert plan is not None
    assert len(plan["operations"]) == 3

    # CRITICAL INVARIANT: ZERO disk writes occurred while suspended at interrupt!
    assert (sandbox / "scan_8821.txt").exists()
    assert (sandbox / "IMG_2026_001.txt").exists()
    assert not (sandbox / "Finance").exists()
    assert not (sandbox / "Meetings").exists()

    # Resume with Human-in-the-Loop approval for ALL 3 ops
    status, msg, batch, root_str = asyncio.run(
        runner.confirm_plan(
            thread_id="thread_e2e_001",
            plan_id=plan["plan_id"],
            approved=True,
            approved_operation_ids=[op["id"] for op in plan["operations"]],
        )
    )
    assert status == "executed"
    assert batch is not None
    assert batch.approved_count == 3
    assert (sandbox / "Finance" / "2026-10_CloudScale_Invoice.txt").exists()
    assert (sandbox / "Meetings" / "Phoenix_Q4_Architecture_Notes.txt").exists()

    # Verify Undo restores everything
    executor = SandboxExecutor(guard)
    executor.undo_batch(batch.batch_id)
    assert (sandbox / "scan_8821.txt").exists()
    assert (sandbox / "IMG_2026_001.txt").exists()
    assert not (sandbox / "Finance").exists()
    assert not (sandbox / "Meetings").exists()


# ---------------------------------------------------------------------------
# 5. FastAPI HTTP Contract Smoke Tests
# ---------------------------------------------------------------------------


def test_fastapi_endpoints_config_tree_seed_history(tmp_path: Path) -> None:
    sandbox = tmp_path / "api_sandbox"
    sandbox.mkdir()
    client = TestClient(app)

    # Update config to point at isolated temp sandbox + test BYOK masking
    res = client.put(
        "/api/config",
        json={
            "sandbox_root": str(sandbox),
            "api_key": "demo-key-1234567890abcdef",
            "model_name": "gpt-4o-mini",
        },
    )
    assert res.status_code == 200
    cfg = res.json()
    assert cfg["has_api_key"] is True
    assert cfg["api_key_masked"] == "demo...cdef"
    assert "1234567890" not in json.dumps(cfg)

    # Seed demo files
    seed_res = client.post("/api/workspace/seed-demo")
    assert seed_res.status_code == 200
    assert len(seed_res.json()["created_files"]) >= 8

    # Live tree endpoint
    tree_res = client.get("/api/workspace/tree")
    assert tree_res.status_code == 200
    assert tree_res.json()["type"] == "dir"
    assert len(tree_res.json()["children"]) >= 8

    # History endpoint initially empty
    hist_res = client.get("/api/history")
    assert hist_res.status_code == 200
    assert hist_res.json() == []


def test_l2_pdf_probe_and_cancel_interrupt_flow(tmp_path: Path) -> None:
    from pypdf import PdfWriter

    sandbox = tmp_path / "ws_pdf_cancel"
    sandbox.mkdir()
    guard = SandboxGuard(sandbox)

    # Create a real 2-page PDF with metadata
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_blank_page(width=200, height=200)
    writer.add_metadata({"/Title": "Q4 Security Audit", "/Author": "SafeWorkspace"})
    pdf_path = sandbox / "audit_doc.pdf"
    with pdf_path.open("wb") as fh:
        writer.write(fh)

    l2_pdf = inspect_file_head(guard, "audit_doc.pdf")
    assert l2_pdf.file_type == "pdf"
    assert l2_pdf.page_count == 2
    assert l2_pdf.pdf_metadata is not None
    assert l2_pdf.pdf_metadata.get("title") == "Q4 Security Audit"

    # Test LangGraph interrupt -> human rejection (approved=False) leaves disk completely untouched
    seed_demo_workspace(guard)
    runner = SafeWorkspaceAgentRunner(settings)
    mock_llm = _ScriptedMockLLM()

    async def _run_and_cancel() -> tuple[str, list[dict[str, Any]]]:
        evts: list[dict[str, Any]] = []
        async for evt in runner.stream_run(
            instruction="整理目录",
            thread_id="thread_cancel_001",
            sandbox_root=str(sandbox),
            llm_override=mock_llm,
        ):
            evts.append(evt)
        plan_evt = next(e for e in evts if e["type"] == "plan_interrupt")
        status, _msg, batch, _root = await runner.confirm_plan(
            thread_id="thread_cancel_001",
            plan_id=plan_evt["detail"]["plan_id"],
            approved=False,
            approved_operation_ids=[],
            user_comment="先不整理",
        )
        assert batch is None
        return status, evts

    cancel_status, _ = asyncio.run(_run_and_cancel())
    assert cancel_status == "cancelled"
    assert (sandbox / "scan_8821.txt").exists()
    assert not (sandbox / "Finance").exists()


def test_case_only_rename_and_chained_move_undo(tmp_path: Path) -> None:
    sandbox = tmp_path / "ws_edge"
    sandbox.mkdir()
    (sandbox / "Report.txt").write_text("case rename", encoding="utf-8")
    (sandbox / "a.txt").write_text("content A", encoding="utf-8")
    (sandbox / "c.txt").write_text("content C", encoding="utf-8")

    guard = SandboxGuard(sandbox)
    plan = stage_organization_plan(
        guard=guard,
        thread_id="t_edge",
        summary="Test case-only rename and chained moves",
        proposed_ops=[
            ProposedOperationInput(
                op_type="rename",
                source="Report.txt",
                target="report.txt",
                reason="Normalize filename casing",
            ),
            ProposedOperationInput(
                op_type="move",
                source="a.txt",
                target="b.txt",
                reason="Move A to B",
            ),
            ProposedOperationInput(
                op_type="move",
                source="c.txt",
                target="a.txt",
                reason="Move C into vacated A",
            ),
        ],
    )

    executor = SandboxExecutor(guard)
    batch = executor.execute_plan(plan, [op.id for op in plan.operations])
    assert batch.status == "executed"
    assert (sandbox / "b.txt").read_text(encoding="utf-8") == "content A"
    assert (sandbox / "a.txt").read_text(encoding="utf-8") == "content C"
    assert not (sandbox / "c.txt").exists()

    reverted = executor.undo_batch(batch.batch_id)
    assert reverted.status == "reverted"
    assert (sandbox / "a.txt").read_text(encoding="utf-8") == "content A"
    assert (sandbox / "c.txt").read_text(encoding="utf-8") == "content C"
    assert not (sandbox / "b.txt").exists()


def test_fastapi_sse_stream_confirm_and_undo_e2e(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify full HTTP wire flow: POST /api/agent/run/stream -> /api/agent/confirm -> /api/history/{id}/undo."""
    from app.agent import graph as graph_mod

    sandbox = tmp_path / "ws_http_e2e"
    sandbox.mkdir()
    client = TestClient(app)

    assert client.put("/api/config", json={"sandbox_root": str(sandbox)}).status_code == 200
    assert client.post("/api/workspace/seed-demo").status_code == 200

    mock_llm = _ScriptedMockLLM()
    monkeypatch.setattr(graph_mod, "_build_llm", lambda _cfg: mock_llm)

    sse_events: list[dict[str, Any]] = []
    with client.stream(
        "POST",
        "/api/agent/run/stream",
        json={"instruction": "按财务发票和会议纪要整理目录", "thread_id": "http_e2e_thread"},
    ) as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")
        for raw_line in resp.iter_lines():
            if raw_line.startswith("data:"):
                sse_events.append(json.loads(raw_line[5:].strip()))

    event_types = [e["type"] for e in sse_events]
    assert "plan_interrupt" in event_types
    assert "security_blocked" in event_types
    assert event_types[-1] == "done"

    plan = next(e["detail"] for e in sse_events if e["type"] == "plan_interrupt")
    op_ids = [op["id"] for op in plan["operations"]]

    # Confirm via HTTP
    confirm_res = client.post(
        "/api/agent/confirm",
        json={
            "thread_id": "http_e2e_thread",
            "plan_id": plan["plan_id"],
            "approved": True,
            "approved_operation_ids": op_ids,
        },
    )
    assert confirm_res.status_code == 200
    confirm_body = confirm_res.json()
    assert confirm_body["status"] == "executed"
    batch_id = confirm_body["batch"]["batch_id"]
    assert (sandbox / "Finance" / "2026-10_CloudScale_Invoice.txt").exists()

    # Undo via HTTP
    undo_res = client.post(f"/api/history/{batch_id}/undo")
    assert undo_res.status_code == 200
    assert undo_res.json()["status"] == "reverted"
    assert (sandbox / "scan_8821.txt").exists()
    assert not (sandbox / "Finance").exists()


# ---------------------------------------------------------------------------
# 6. Local Config Persistence, Dynamic Workspace Guard & Dialog, DOCX L2/L3
# ---------------------------------------------------------------------------


def test_local_config_persistence_priority_and_recent_workspaces(tmp_path: Path) -> None:
    cfg_file = tmp_path / ".local_config.json"
    workspaces = []
    for i in range(6):
        d = tmp_path / f"ws_{i}"
        d.mkdir()
        workspaces.append(d)

    env_settings = Settings(
        OPENAI_API_KEY="test-from-env-only",
        OPENAI_BASE_URL="https://api.openai.com/v1",
        MODEL_NAME="gpt-4o-mini",
        SANDBOX_ROOT=str(workspaces[0]),
    )

    store1 = RuntimeConfigStore(env_settings, local_config_path=cfg_file)
    assert store1.api_key == "test-from-env-only"

    # Save user BYOK & switch through 6 workspaces
    store1.update(
        api_key="test-persisted-key-9999",
        base_url="https://api.deepseek.com/v1",
        model_name="deepseek-chat",
        sandbox_root=str(workspaces[1]),
    )
    for ws in workspaces[2:]:
        store1.update(sandbox_root=str(ws))

    assert cfg_file.exists()
    # Most recent 5 only, newest first
    assert len(store1.recent_workspaces) == 5
    assert Path(store1.recent_workspaces[0]) == workspaces[5].resolve()
    assert str(workspaces[0].resolve()) not in store1.recent_workspaces

    # Simulate fresh backend restart: .local_config.json MUST take precedence over .env
    store2 = RuntimeConfigStore(env_settings, local_config_path=cfg_file)
    assert store2.api_key == "test-persisted-key-9999"
    assert store2.base_url == "https://api.deepseek.com/v1"
    assert store2.model_name == "deepseek-chat"
    assert store2.sandbox_root == workspaces[5].resolve()
    assert len(store2.recent_workspaces) == 5


def test_sandbox_guard_blocks_drive_system_and_home_roots(tmp_path: Path) -> None:
    import os

    # 1. Filesystem / drive root blocked
    with pytest.raises(SandboxSecurityError):
        SandboxGuard.validate_sandbox_root(Path("/").resolve(), must_exist=False)

    # 2. Current user home root & users container blocked
    home = Path.home().resolve()
    with pytest.raises(SandboxSecurityError):
        SandboxGuard.validate_sandbox_root(home, must_exist=False)
    if home.parent != home.parent.parent:
        with pytest.raises(SandboxSecurityError):
            SandboxGuard.validate_sandbox_root(home.parent, must_exist=False)

    # 3. OS system directories blocked
    if os.name == "nt":
        for forbidden in ("C:\\", "D:\\", "C:\\Windows", "C:\\Windows\\System32", "C:\\Program Files"):
            with pytest.raises(SandboxSecurityError):
                SandboxGuard.validate_sandbox_root(forbidden, must_exist=False)
    else:
        for forbidden in ("/", "/etc", "/usr/bin"):
            with pytest.raises(SandboxSecurityError):
                SandboxGuard.validate_sandbox_root(forbidden, must_exist=False)

    # 4. Valid personal subdirectory allowed
    ok_dir = tmp_path / "course_reports"
    ok_dir.mkdir()
    assert SandboxGuard.validate_sandbox_root(ok_dir, must_exist=True) == ok_dir.resolve()


def test_workspace_select_dialog_endpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app import main as main_mod

    valid_dir = tmp_path / "picked_project"
    valid_dir.mkdir()
    client = TestClient(app)

    # Case 1: Cancelled dialog
    monkeypatch.setattr(main_mod, "_run_tk_folder_dialog", lambda _init: "")
    res_cancel = client.post("/api/workspace/select-dialog")
    assert res_cancel.status_code == 200
    assert res_cancel.json()["cancelled"] is True
    assert res_cancel.json()["valid"] is False

    # Case 2: Forbidden user home root blocked cleanly
    monkeypatch.setattr(main_mod, "_run_tk_folder_dialog", lambda _init: str(Path.home().resolve()))
    res_blocked = client.post("/api/workspace/select-dialog")
    assert res_blocked.status_code == 200
    body_blocked = res_blocked.json()
    assert body_blocked["cancelled"] is False
    assert body_blocked["valid"] is False
    assert body_blocked["error"] is not None

    # Case 3: Valid directory accepted
    monkeypatch.setattr(main_mod, "_run_tk_folder_dialog", lambda _init: str(valid_dir))
    res_ok = client.post("/api/workspace/select-dialog")
    assert res_ok.status_code == 200
    body_ok = res_ok.json()
    assert body_ok["cancelled"] is False
    assert body_ok["valid"] is True
    assert Path(body_ok["selected_path"]) == valid_dir.resolve()


def test_docx_l2_and_l3_progressive_disclosure(tmp_path: Path) -> None:
    from docx import Document

    sandbox = tmp_path / "ws_docx"
    sandbox.mkdir()
    guard = SandboxGuard(sandbox)

    doc = Document()
    doc.core_properties.title = "机器学习监督学习实验报告"
    doc.core_properties.author = "Alice Chen"
    doc.add_heading("实验一：二手车价格预测模型", level=1)
    doc.add_paragraph("本实验使用随机森林与梯度提升回归模型预测二手车市场价格。")

    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "课程名称"
    table.cell(0, 1).text = "机器学习实战"
    table.cell(1, 0).text = "学号"
    table.cell(1, 1).text = "20260001"

    for i in range(1, 41):
        doc.add_paragraph(f"实验记录段落 {i:02d}：特征工程与交叉验证 RMSE 评估结果分析。")

    docx_path = sandbox / "lab_report_01.docx"
    doc.save(str(docx_path))

    # L2 head probe
    l2 = inspect_file_head(guard, "lab_report_01.docx", max_lines=10, max_chars=800)
    assert l2.file_type == "docx"
    assert l2.truncated is True
    assert l2.docx_metadata is not None
    assert l2.docx_metadata.get("title") == "机器学习监督学习实验报告"
    assert l2.docx_metadata.get("author") == "Alice Chen"
    assert "二手车价格预测模型" in l2.preview
    assert "随机森林" in l2.preview

    # L3 deep slice probe
    l3 = read_file_deep(guard, "lab_report_01.docx", start_line=15, max_lines=15)
    assert l3.start_line == 15
    assert l3.end_line == 29
    assert l3.total_lines >= 42
    assert "实验记录段落" in l3.content


def test_parent_dir_move_and_child_file_ordering_and_undo(tmp_path: Path) -> None:
    """Regression test for parent-dir move + child-file rename conflict & valid chained sequences."""
    sandbox = tmp_path / "ws_dir_child"
    (sandbox / "docx_uml_work" / "final_render").mkdir(parents=True)
    (sandbox / "docx_uml_work" / "final_render" / "final.pdf").write_bytes(b"%PDF-1.4 fake")
    (sandbox / "docx_uml_work" / "final_render" / "page1.png").write_bytes(b"png")

    guard = SandboxGuard(sandbox)

    # 1. Conflicting order: move parent dir, then reference child under OLD path -> MUST reject during staging
    with pytest.raises(ValueError, match="Operation order conflict"):
        stage_organization_plan(
            guard=guard,
            thread_id="t_conflict",
            summary="Conflicting parent+child ops",
            proposed_ops=[
                ProposedOperationInput(
                    op_type="move",
                    source="docx_uml_work/final_render",
                    target="文档/渲染预览",
                    reason="Move folder",
                ),
                ProposedOperationInput(
                    op_type="rename",
                    source="docx_uml_work/final_render/final.pdf",
                    target="文档/渲染预览/终版.pdf",
                    reason="Rename using stale old path",
                ),
            ],
        )

    # 2. Valid chained post-move path: move parent dir -> rename using NEW path -> execute & undo cleanly
    executor = SandboxExecutor(guard)
    plan_chained = stage_organization_plan(
        guard=guard,
        thread_id="t_chained",
        summary="Move folder then rename child at new path",
        proposed_ops=[
            ProposedOperationInput(
                op_type="move",
                source="docx_uml_work/final_render",
                target="文档/渲染预览",
                reason="Move folder",
            ),
            ProposedOperationInput(
                op_type="rename",
                source="文档/渲染预览/final.pdf",
                target="文档/渲染预览/终版.pdf",
                reason="Rename child at new path",
            ),
        ],
    )
    batch1 = executor.execute_plan(plan_chained, [o.id for o in plan_chained.operations])
    assert batch1.status == "executed"
    assert (sandbox / "文档" / "渲染预览" / "终版.pdf").exists()
    assert not (sandbox / "docx_uml_work" / "final_render").exists()

    executor.undo_batch(batch1.batch_id)
    assert (sandbox / "docx_uml_work" / "final_render" / "final.pdf").exists()
    assert not (sandbox / "文档").exists()

    # 3. Valid child-first order: rename child first -> then move parent dir -> execute & undo cleanly
    plan_child_first = stage_organization_plan(
        guard=guard,
        thread_id="t_child_first",
        summary="Rename child first then move folder",
        proposed_ops=[
            ProposedOperationInput(
                op_type="rename",
                source="docx_uml_work/final_render/final.pdf",
                target="docx_uml_work/final_render/终版.pdf",
                reason="Rename child first",
            ),
            ProposedOperationInput(
                op_type="move",
                source="docx_uml_work/final_render",
                target="文档/渲染预览",
                reason="Move folder after child rename",
            ),
        ],
    )
    batch2 = executor.execute_plan(plan_child_first, [o.id for o in plan_child_first.operations])
    assert batch2.status == "executed"
    assert (sandbox / "文档" / "渲染预览" / "终版.pdf").exists()

    executor.undo_batch(batch2.batch_id)
    assert (sandbox / "docx_uml_work" / "final_render" / "final.pdf").exists()
    assert not (sandbox / "文档").exists()


def test_workspace_execute_accepts_manual_adjusted_ops_and_blocks_illegal_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify POST /api/workspace/execute applies user drag/rename/mkdir edits and blocks traversal."""
    from app.agent import graph as graph_mod

    sandbox = tmp_path / "ws_manual_edit"
    sandbox.mkdir()
    client = TestClient(app)

    assert client.put("/api/config", json={"sandbox_root": str(sandbox)}).status_code == 200
    assert client.post("/api/workspace/seed-demo").status_code == 200

    # Run 1: user manually drags invoice into a custom virtual folder & renames it
    mock_llm_1 = _ScriptedMockLLM()
    monkeypatch.setattr(graph_mod, "_build_llm", lambda _cfg: mock_llm_1)
    events: list[dict[str, Any]] = []
    with client.stream(
        "POST",
        "/api/agent/run/stream",
        json={"instruction": "整理文件", "thread_id": "manual_edit_thread_1"},
    ) as resp:
        assert resp.status_code == 200
        for line in resp.iter_lines():
            if line.startswith("data:"):
                events.append(json.loads(line[5:].strip()))

    plan = next(e["detail"] for e in events if e["type"] == "plan_interrupt")
    adjusted_ops = [
        {
            "id": "op_manual_001",
            "op_type": "mkdir",
            "source": None,
            "target": "自定义目录",
            "reason": "用户手动新建目标目录",
            "probe_evidence": "L1",
            "size_bytes": 0,
            "manual_adjusted": True,
        },
        {
            "id": plan["operations"][1]["id"],
            "op_type": "move",
            "source": "scan_8821.txt",
            "target": "自定义目录/手动重命名发票.txt",
            "reason": "用户拖拽微调并双击重命名",
            "probe_evidence": "L2",
            "size_bytes": 100,
            "manual_adjusted": True,
        },
    ]

    exec_res = client.post(
        "/api/workspace/execute",
        json={
            "thread_id": "manual_edit_thread_1",
            "plan_id": plan["plan_id"],
            "approved": True,
            "approved_operation_ids": ["op_manual_001", plan["operations"][1]["id"]],
            "operations": adjusted_ops,
        },
    )
    assert exec_res.status_code == 200
    body = exec_res.json()
    assert body["status"] == "executed"
    assert (sandbox / "自定义目录" / "手动重命名发票.txt").exists()
    assert not (sandbox / "Finance").exists()

    # Undo works seamlessly on manually adjusted batches too
    undo_res = client.post(f"/api/history/{body['batch']['batch_id']}/undo")
    assert undo_res.status_code == 200
    assert (sandbox / "scan_8821.txt").exists()
    assert not (sandbox / "自定义目录").exists()

    # Run 2: tampered traversal path in user-submitted operations MUST be blocked (403)
    mock_llm_2 = _ScriptedMockLLM()
    monkeypatch.setattr(graph_mod, "_build_llm", lambda _cfg: mock_llm_2)
    events2: list[dict[str, Any]] = []
    with client.stream(
        "POST",
        "/api/agent/run/stream",
        json={"instruction": "整理文件", "thread_id": "manual_edit_thread_2"},
    ) as resp:
        for line in resp.iter_lines():
            if line.startswith("data:"):
                events2.append(json.loads(line[5:].strip()))
    plan2 = next(e["detail"] for e in events2 if e["type"] == "plan_interrupt")

    bad_res = client.post(
        "/api/workspace/execute",
        json={
            "thread_id": "manual_edit_thread_2",
            "plan_id": plan2["plan_id"],
            "approved": True,
            "approved_operation_ids": ["op_bad"],
            "operations": [
                {
                    "id": "op_bad",
                    "op_type": "move",
                    "source": "scan_8821.txt",
                    "target": "../../escape.txt",
                    "reason": "malicious path",
                    "probe_evidence": "L1",
                    "size_bytes": 100,
                    "manual_adjusted": True,
                }
            ],
        },
    )
    assert bad_res.status_code == 403
    assert (sandbox / "scan_8821.txt").exists()


# ---------------------------------------------------------------------------
# 7. Single-Workspace Multi-Turn Conversation Memory & Context Compaction
# ---------------------------------------------------------------------------


def test_multi_turn_conversation_memory_and_context_compaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify multi-turn thread_id continuity via MemorySaver + 6-turn Context Compaction."""
    from langchain_core.messages import HumanMessage, ToolMessage
    from app.agent import graph as graph_mod
    from app.agent.graph import compact_messages_for_llm

    # --- Part A: Context Compaction keeps last 6 turns & folds older oversized ToolMessages ---
    synthetic_history: list[Any] = []
    for t in range(1, 9):  # 8 turns total
        synthetic_history.append(HumanMessage(content=f"用户第 {t} 轮指令"))
        synthetic_history.append(
            AIMessage(
                content="",
                tool_calls=[{"name": "scan_directory_tree", "args": {"subpath": "."}, "id": f"tc_{t}"}],
            )
        )
        synthetic_history.append(
            ToolMessage(
                content=f"TURN_{t}_RAW_PAYLOAD_" + ("X" * 800),
                tool_call_id=f"tc_{t}",
                name="scan_directory_tree",
            )
        )
        if t < 8:
            synthetic_history.append(AIMessage(content=f"助手第 {t} 轮回复"))

    compacted = compact_messages_for_llm(synthetic_history, max_recent_turns=6, tool_fold_threshold=320)
    human_contents = [m.content for m in compacted if isinstance(m, HumanMessage)]
    assert len(human_contents) == 6
    assert human_contents[0] == "用户第 3 轮指令"
    assert human_contents[-1] == "用户第 8 轮指令"

    tool_msgs = [m for m in compacted if isinstance(m, ToolMessage)]
    assert len(tool_msgs) == 6
    # Turns 3..7 ToolMessages folded while preserving tool_call_id
    for tm in tool_msgs[:-1]:
        assert "历史探针结果已折叠" in str(tm.content)
        assert tm.tool_call_id.startswith("tc_")
    # Current turn (Turn 8) ToolMessage preserved verbatim!
    assert "TURN_8_RAW_PAYLOAD_" in str(tool_msgs[-1].content)

    # --- Part B: End-to-End HTTP /api/agent/run/stream multi-turn continuity ---
    sandbox1 = tmp_path / "ws_multi_1"
    sandbox2 = tmp_path / "ws_multi_2"
    sandbox1.mkdir()
    sandbox2.mkdir()
    client = TestClient(app)

    assert client.put("/api/config", json={"sandbox_root": str(sandbox1)}).status_code == 200
    assert client.post("/api/workspace/seed-demo").status_code == 200

    observed_turn2_messages: list[Any] = []

    class _MultiTurnMockLLM:
        def __init__(self) -> None:
            self.calls = 0

        def bind_tools(self, tools: list[Any]) -> "_MultiTurnMockLLM":
            return self

        async def astream(self, messages: list[Any]):
            self.calls += 1
            if self.calls == 1:
                # Turn 1 step 1: invoke L1 scan (produces bulky JSON > 320 chars)
                yield AIMessageChunk(
                    content="先执行 L1 扫描了解目录结构。",
                    tool_calls=[
                        {"name": "scan_directory_tree", "args": {"subpath": ".", "max_depth": 4}, "id": "call_m1"}
                    ],
                )
            elif self.calls == 2:
                # Turn 1 step 2: reply directly without staging plan yet (asking user preference)
                yield AIMessageChunk(
                    content="我发现了财务发票与会议纪要，请问你希望按部门分类还是按年份归档？"
                )
            else:
                # Turn 2+: capture compacted messages passed to LLM to assert memory + folding!
                observed_turn2_messages.extend(messages)
                yield AIMessageChunk(
                    content="好的，已记住你上一轮上下文并按年份归档规则处理。"
                )

    mock_llm = _MultiTurnMockLLM()
    monkeypatch.setattr(graph_mod, "_build_llm", lambda _cfg: mock_llm)

    # Turn 1: omit thread_id -> backend generates new thread_id and sends it in run_started
    turn1_events: list[dict[str, Any]] = []
    with client.stream(
        "POST",
        "/api/agent/run/stream",
        json={"instruction": "先帮我看看这个目录里有什么"},
    ) as resp1:
        assert resp1.status_code == 200
        for line in resp1.iter_lines():
            if line.startswith("data:"):
                turn1_events.append(json.loads(line[5:].strip()))

    start1 = next(e for e in turn1_events if e["type"] == "run_started")
    thread_id = start1["thread_id"]
    assert thread_id.startswith("thread_")
    assert start1["detail"]["is_continuation"] is False
    assert start1["detail"]["turn_index"] == 1

    # Turn 2: pass the exact same thread_id -> backend reuses MemorySaver checkpoint!
    turn2_events: list[dict[str, Any]] = []
    with client.stream(
        "POST",
        "/api/agent/run/stream",
        json={"instruction": "按年份归档吧", "thread_id": thread_id},
    ) as resp2:
        assert resp2.status_code == 200
        for line in resp2.iter_lines():
            if line.startswith("data:"):
                turn2_events.append(json.loads(line[5:].strip()))

    start2 = next(e for e in turn2_events if e["type"] == "run_started")
    assert start2["thread_id"] == thread_id
    assert start2["detail"]["is_continuation"] is True
    assert start2["detail"]["turn_index"] == 2

    # Verify LLM in Turn 2 received BOTH Turn 1 & Turn 2 HumanMessages + folded Turn 1 L1 ToolMessage!
    t2_humans = [m.content for m in observed_turn2_messages if isinstance(m, HumanMessage)]
    assert t2_humans == ["先帮我看看这个目录里有什么", "按年份归档吧"]
    t2_tools = [m for m in observed_turn2_messages if isinstance(m, ToolMessage)]
    assert len(t2_tools) == 1
    assert "历史探针结果已折叠" in str(t2_tools[0].content)

    # Turn 3: Switch workspace root -> passing old thread_id auto-isolates into a new thread_id!
    assert client.put("/api/config", json={"sandbox_root": str(sandbox2)}).status_code == 200
    turn3_events: list[dict[str, Any]] = []
    with client.stream(
        "POST",
        "/api/agent/run/stream",
        json={"instruction": "看看新目录", "thread_id": thread_id},
    ) as resp3:
        assert resp3.status_code == 200
        for line in resp3.iter_lines():
            if line.startswith("data:"):
                turn3_events.append(json.loads(line[5:].strip()))

    start3 = next(e for e in turn3_events if e["type"] == "run_started")
    assert start3["thread_id"] != thread_id
    assert start3["detail"]["is_continuation"] is False






