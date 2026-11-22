"""Shared Pydantic v2 data contracts between Backend and Frontend."""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


ProbeLevel = Literal["L1", "L2", "L3", "PLAN"]
OperationType = Literal["mkdir", "move", "rename", "archive"]
DiffStatus = Literal[
    "unchanged",
    "added",
    "removed",
    "moved_from",
    "moved_to",
    "renamed_from",
    "renamed_to",
    "archived_to",
]


# ---------------------------------------------------------------------------
# Configuration & Workspace Status
# ---------------------------------------------------------------------------


class LLMConfigOverride(BaseModel):
    """Optional per-request BYOK LLM configuration override."""

    api_key: Optional[str] = None
    base_url: Optional[str] = None
    model_name: Optional[str] = None


class WorkspaceConfigUpdateRequest(BaseModel):
    """Payload for updating active sandbox directory and/or BYOK LLM config."""

    sandbox_root: Optional[str] = None
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    model_name: Optional[str] = None


class WorkspaceConfigResponse(BaseModel):
    """Current workspace & model configuration returned to UI (key masked)."""

    sandbox_root: str
    sandbox_exists: bool
    model_name: str
    base_url: str
    has_api_key: bool
    api_key_masked: str
    file_count: int = 0
    dir_count: int = 0
    total_size_bytes: int = 0
    recent_workspaces: list[str] = Field(default_factory=list)
    persisted: bool = False


class WorkspaceDialogResponse(BaseModel):
    """Result from native OS directory selection dialog."""

    selected_path: Optional[str] = None
    cancelled: bool = False
    valid: bool = False
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Directory Tree & Progressive Disclosure Probes (L1 / L2 / L3)
# ---------------------------------------------------------------------------


class FileTreeNode(BaseModel):
    """Recursive directory tree node supporting before/after diff rendering."""

    name: str
    path: str
    type: Literal["file", "dir"]
    size: int = 0
    extension: Optional[str] = None
    modified_at: Optional[str] = None
    diff_status: DiffStatus = "unchanged"
    operation_id: Optional[str] = None
    related_path: Optional[str] = None
    children: list["FileTreeNode"] = Field(default_factory=list)


FileTreeNode.model_rebuild()


class L1ScanResult(BaseModel):
    """L1 Progressive Disclosure: Lightweight directory tree & metadata."""

    root_path: str
    max_depth: int
    total_files: int
    total_dirs: int
    total_size_bytes: int
    truncated: bool = False
    skipped_items: list[str] = Field(default_factory=list)
    tree: FileTreeNode
    flat_entries: list[dict[str, Any]] = Field(default_factory=list)


class L2HeadResult(BaseModel):
    """L2 Progressive Disclosure: Lightweight head preview / PDF first page / DOCX outline / metadata."""

    path: str
    file_type: Literal["text", "pdf", "docx", "image", "archive", "binary"]
    mime_type: str
    size_bytes: int
    modified_at: str
    encoding: Optional[str] = None
    line_count_estimate: Optional[int] = None
    page_count: Optional[int] = None
    pdf_metadata: Optional[dict[str, str]] = None
    docx_metadata: Optional[dict[str, str]] = None
    preview: str
    truncated: bool = False


class L3DeepResult(BaseModel):
    """L3 Progressive Disclosure: Bounded deep content read for disambiguation."""

    path: str
    start_line: int
    end_line: int
    total_lines: int
    content: str
    truncated: bool = False


# ---------------------------------------------------------------------------
# Organization Plan & Diff Staging (Dry-Run Proposal)
# ---------------------------------------------------------------------------


class ProposedOperationInput(BaseModel):
    """Single operation proposed by the LLM before server-side enrichment."""

    op_type: OperationType = Field(
        description="Safe non-destructive operation type: mkdir, move, rename, or archive."
    )
    source: Optional[str] = Field(
        default=None,
        description="Relative POSIX path of existing file/dir inside SANDBOX_ROOT. Required for move/rename/archive; omit for mkdir.",
    )
    target: str = Field(
        description="Relative POSIX destination path inside SANDBOX_ROOT."
    )
    reason: str = Field(
        description="Concise explanation of why this organization action is proposed."
    )
    probe_evidence: Literal["L1", "L2", "L3"] = Field(
        default="L1",
        description="Highest progressive disclosure probe level used to classify this item.",
    )
    category: Optional[str] = Field(
        default=None,
        description="Optional semantic category label (e.g., Finance, Engineering, Media).",
    )


class FileOperation(BaseModel):
    """Validated, enriched operation inside a staged OrganizationPlan."""

    id: str
    op_type: OperationType
    source: Optional[str] = None
    target: str
    reason: str
    probe_evidence: Literal["L1", "L2", "L3"] = "L1"
    category: Optional[str] = None
    size_bytes: int = 0
    manual_adjusted: bool = False


class OrganizationPlan(BaseModel):
    """Staged reorganization proposal awaiting Human-in-the-Loop approval."""

    plan_id: str
    thread_id: str
    summary: str
    created_at: str
    operations: list[FileOperation] = Field(default_factory=list)
    before_tree: FileTreeNode
    after_tree: FileTreeNode
    warnings: list[str] = Field(default_factory=list)
    stats: dict[str, int] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Transactional Execution & Undo Audit Journal (.agent_history/)
# ---------------------------------------------------------------------------


class ExecutionStepLog(BaseModel):
    """Atomic execution record for a single operation (used for rollback & undo)."""

    operation_id: str
    op_type: OperationType
    source: Optional[str] = None
    target: str
    status: Literal["completed", "skipped", "failed", "reverted"]
    created_dirs: list[str] = Field(default_factory=list)
    file_hash: Optional[str] = None
    error: Optional[str] = None


class ExecutionBatchRecord(BaseModel):
    """Persisted batch manifest saved in `.agent_history/<batch_id>.json`."""

    batch_id: str
    plan_id: str
    thread_id: str
    summary: str
    status: Literal["executed", "reverted", "rolled_back_on_error", "cancelled"]
    created_at: str
    reverted_at: Optional[str] = None
    approved_count: int = 0
    skipped_count: int = 0
    steps: list[ExecutionStepLog] = Field(default_factory=list)
    error_message: Optional[str] = None


# ---------------------------------------------------------------------------
# SSE Streaming Events & HITL API Requests / Responses
# ---------------------------------------------------------------------------


class AgentRunRequest(BaseModel):
    """Start or continue a multi-turn agent organization session via SSE."""

    instruction: str = Field(..., min_length=1, max_length=4000)
    thread_id: Optional[str] = Field(
        default=None,
        description="Optional existing thread_id to continue multi-turn conversation memory; omit or null to start a new chat.",
    )
    llm_config: Optional[LLMConfigOverride] = None


class ConfirmPlanRequest(BaseModel):
    """Human-in-the-Loop confirmation or rejection of a suspended LangGraph interrupt."""

    thread_id: str
    plan_id: str
    approved: bool = True
    approved_operation_ids: list[str] = Field(default_factory=list)
    operations: Optional[list[FileOperation]] = Field(
        default=None,
        description="Optional user-edited operations list (after drag-and-drop / inline rename / virtual mkdir adjustments).",
    )
    user_comment: Optional[str] = None


class ConfirmPlanResponse(BaseModel):
    """Result after resuming LangGraph interrupt and running transactional executor."""

    thread_id: str
    status: Literal["executed", "cancelled", "rolled_back_on_error"]
    message: str
    batch: Optional[ExecutionBatchRecord] = None
    updated_tree: FileTreeNode


class UndoResponse(BaseModel):
    """Result after reverting a transaction batch."""

    batch_id: str
    status: Literal["reverted"]
    message: str
    batch: ExecutionBatchRecord
    updated_tree: FileTreeNode


class SeedDemoResponse(BaseModel):
    """Result of seeding sample messy files inside SANDBOX_ROOT."""

    created_files: list[str]
    skipped_files: list[str]
    updated_tree: FileTreeNode


TraceEventType = Literal[
    "run_started",
    "thinking",
    "tool_start",
    "tool_end",
    "security_blocked",
    "plan_interrupt",
    "assistant_message",
    "execution_complete",
    "error",
    "done",
]


class TraceEvent(BaseModel):
    """Structured SSE event streamed to frontend AgentTraceLog & DiffTreePreview."""

    event_id: str
    thread_id: str
    timestamp: str
    type: TraceEventType
    probe_level: Optional[ProbeLevel] = None
    tool_name: Optional[str] = None
    tool_args: Optional[dict[str, Any]] = None
    summary: str = ""
    detail: Optional[Any] = None
