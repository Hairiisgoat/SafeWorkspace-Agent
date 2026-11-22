"""Exactly 4 atomic tools bound to the LLM — ZERO direct filesystem write privileges."""

from __future__ import annotations

import json
from typing import Any, Optional

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from app.config import Settings
from app.sandbox.executor import stage_organization_plan
from app.sandbox.guard import SandboxGuard
from app.sandbox.readers import inspect_file_head, read_file_deep, scan_directory_tree
from app.schemas import OrganizationPlan, ProbeLevel, ProposedOperationInput


TOOL_PROBE_LEVELS: dict[str, ProbeLevel] = {
    "scan_directory_tree": "L1",
    "inspect_file_head": "L2",
    "read_file_deep": "L3",
    "propose_organization_plan": "PLAN",
}


# ---------------------------------------------------------------------------
# Strict Pydantic v2 Tool Input Schemas
# ---------------------------------------------------------------------------


class ScanDirectoryTreeInput(BaseModel):
    subpath: str = Field(
        default=".",
        description="Relative directory path inside SANDBOX_ROOT to scan. Use '.' for workspace root.",
    )
    max_depth: int = Field(
        default=4,
        ge=1,
        le=8,
        description="Maximum recursion depth (1-8).",
    )


class InspectFileHeadInput(BaseModel):
    file_path: str = Field(
        ...,
        description="Relative path of a single ambiguous file inside SANDBOX_ROOT to probe (L2).",
    )
    max_lines: int = Field(
        default=35,
        ge=5,
        le=100,
        description="Maximum head lines to read for text files.",
    )


class ReadFileDeepInput(BaseModel):
    file_path: str = Field(
        ...,
        description="Relative path of a file inside SANDBOX_ROOT for deep disambiguation (L3). Use ONLY when L1+L2 is insufficient.",
    )
    start_line: int = Field(
        default=1,
        ge=1,
        description="1-based starting line index.",
    )
    max_lines: int = Field(
        default=180,
        ge=10,
        le=400,
        description="Maximum lines to read in this deep slice.",
    )


class ProposeOrganizationPlanInput(BaseModel):
    summary: str = Field(
        ...,
        min_length=4,
        max_length=600,
        description="Concise summary of the organization strategy and folder taxonomy.",
    )
    operations: list[ProposedOperationInput] = Field(
        ...,
        min_length=1,
        description="Ordered list of safe non-destructive operations (mkdir, move, rename, archive) to stage for human approval.",
    )


def create_agent_tools(
    guard: SandboxGuard,
    thread_id: str,
    cfg: Settings,
    staged_holder: dict[str, Optional[OrganizationPlan]],
) -> list[BaseTool]:
    """Build the 4 atomic sandboxed tools bound to the LLM.

    Security Invariant:
    NONE of these 4 tools ever mutate files or directories on disk.
    `propose_organization_plan` only performs dry-run validation & virtual diff staging
    before LangGraph suspends for Human-in-the-Loop approval.
    """

    def _tool_scan_directory_tree(subpath: str = ".", max_depth: int = 4) -> str:
        result = scan_directory_tree(
            guard,
            subpath=subpath,
            max_depth=min(max_depth, cfg.l1_max_depth + 2),
            max_entries=cfg.l1_max_entries,
        )
        # Compact token-efficient representation for LLM context engineering
        payload: dict[str, Any] = {
            "probe_level": "L1",
            "root_path": result.root_path,
            "total_files": result.total_files,
            "total_dirs": result.total_dirs,
            "total_size_bytes": result.total_size_bytes,
            "truncated": result.truncated,
            "skipped_items": result.skipped_items,
            "entries": result.flat_entries,
        }
        return json.dumps(payload, ensure_ascii=False)

    def _tool_inspect_file_head(file_path: str, max_lines: int = 35) -> str:
        result = inspect_file_head(
            guard,
            file_path=file_path,
            max_lines=max_lines,
            max_chars=cfg.l2_max_chars,
        )
        payload = {"probe_level": "L2", **result.model_dump(exclude_none=True)}
        return json.dumps(payload, ensure_ascii=False)

    def _tool_read_file_deep(
        file_path: str, start_line: int = 1, max_lines: int = 180
    ) -> str:
        result = read_file_deep(
            guard,
            file_path=file_path,
            start_line=start_line,
            max_lines=max_lines,
            max_chars=cfg.l3_max_chars,
        )
        payload = {"probe_level": "L3", **result.model_dump()}
        return json.dumps(payload, ensure_ascii=False)

    def _tool_propose_organization_plan(
        summary: str,
        operations: list[dict[str, Any]] | list[ProposedOperationInput],
    ) -> str:
        parsed_ops = [
            op if isinstance(op, ProposedOperationInput) else ProposedOperationInput.model_validate(op)
            for op in operations
        ]
        plan = stage_organization_plan(
            guard=guard,
            thread_id=thread_id,
            summary=summary,
            proposed_ops=parsed_ops,
        )
        staged_holder["plan"] = plan
        return json.dumps(
            {
                "status": "staged_for_human_approval",
                "plan_id": plan.plan_id,
                "summary": plan.summary,
                "stats": plan.stats,
                "warnings": plan.warnings,
                "message": (
                    "Organization plan validated and staged (ZERO disk writes executed). "
                    "Execution is now suspended awaiting Human-in-the-Loop approval."
                ),
            },
            ensure_ascii=False,
        )

    return [
        StructuredTool.from_function(
            func=_tool_scan_directory_tree,
            name="scan_directory_tree",
            description=(
                "[L1 Progressive Disclosure] Scan directory structure and lightweight file metadata "
                "(names, extensions, sizes, modification times) inside SANDBOX_ROOT without reading contents. "
                "ALWAYS call this first."
            ),
            args_schema=ScanDirectoryTreeInput,
        ),
        StructuredTool.from_function(
            func=_tool_inspect_file_head,
            name="inspect_file_head",
            description=(
                "[L2 Progressive Disclosure] Read lightweight head preview (first lines of text/code/CSV/MD, "
                "opening paragraphs & tables of Word .docx documents, or page 1 + metadata of PDF) "
                "for a single ambiguous file whose name alone does not reveal its purpose."
            ),
            args_schema=InspectFileHeadInput,
        ),
        StructuredTool.from_function(
            func=_tool_read_file_deep,
            name="read_file_deep",
            description=(
                "[L3 Progressive Disclosure] Read a deeper line range of a file. "
                "ONLY call this when L1 filename/metadata AND L2 head preview are genuinely insufficient to classify the file."
            ),
            args_schema=ReadFileDeepInput,
        ),
        StructuredTool.from_function(
            func=_tool_propose_organization_plan,
            name="propose_organization_plan",
            description=(
                "[Staging / Dry-Run ONLY] Validate and stage a non-destructive file organization plan "
                "(mkdir, move, rename, archive) for Human-in-the-Loop diff preview and approval. "
                "Never performs direct disk writes; never supports deletion."
            ),
            args_schema=ProposeOrganizationPlanInput,
        ),
    ]
