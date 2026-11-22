"""LangGraph state machine with L1-L3 Progressive Disclosure prompt & native Interrupt HITL."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, AsyncGenerator, Literal, Optional, TypedDict

from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import Command, interrupt

from app.config import Settings, runtime_config, settings
from app.sandbox.executor import SandboxExecutor
from app.sandbox.guard import SandboxGuard, SandboxSecurityError
from app.schemas import ExecutionBatchRecord, OrganizationPlan, TraceEvent
from .tools import TOOL_PROBE_LEVELS, create_agent_tools


PROGRESSIVE_DISCLOSURE_SYSTEM_PROMPT = """You are **SafeWorkspace Agent**, an expert local workspace organization architect operating inside a **Strict Sandbox** with **Zero Direct Write Privileges**.

## Core Operating Principles (MUST FOLLOW)

### 1. Progressive Disclosure Context Engineering (L1 → L2 → L3)
Minimize token cost and read noise by escalating disclosure levels strictly on demand:
- **L1 (`scan_directory_tree`)**: ALWAYS start by scanning the workspace directory tree and metadata.
- **L2 (`inspect_file_head`)**: ONLY probe files whose names/paths are ambiguous, generic, or opaque (e.g., `IMG_001.txt`, `scan_8821.txt`, `untitled_notes.txt`, `doc_final_v2.md`). Do NOT inspect files whose category is already unmistakable from L1 metadata unless needed to resolve naming or grouping ambiguity.
- **L3 (`read_file_deep`)**: ONLY escalate to deep inspection when L1 + L2 preview is genuinely insufficient to decide classification or naming. Never bulk-read the entire workspace.

### 2. Strict Sandbox & Non-Destructive Safety
- Every path MUST be a clean relative POSIX path inside `SANDBOX_ROOT` (e.g., `Finance/2026-10_CloudScale_Invoice.txt`).
- NEVER attempt to access parent paths (`../`), absolute OS paths, `.agent_history`, or `.git`.
- NEVER propose deleting files — deprecated/legacy items should be organized using `op_type="archive"` into an `Archive/` folder.
- You have NO direct disk-write tools. Once you have gathered sufficient evidence, call `propose_organization_plan` to stage a validated dry-run plan for Human-in-the-Loop Diff approval.

### 3. High-Craft Taxonomy & Traceable Evidence
- Design a clean, intuitive, flat-enough directory taxonomy (typically 2-5 top-level folders, e.g., `Docs/`, `Finance/`, `Engineering/`, `Notes/`, `Archive/`), matching the user's language and intent.
- Prefer meaningful, consistent filenames when renaming opaque files (preserve original file extensions!).
- **Strict Operation Ordering**: NEVER move a parent directory and then reference files inside its OLD path in later operations. If you need to reorganize or rename files inside a directory, either (a) move individual files directly to their final target paths, or (b) rename/move child files BEFORE moving the parent directory.
- For EVERY operation in `propose_organization_plan`, accurately tag `probe_evidence` (`"L1"`, `"L2"`, or `"L3"`) reflecting the highest probe level actually used for that item, and write a concise, specific `reason` in the user's language.
- If the workspace is already clean or the user only asks an informational question, reply clearly without staging an unnecessary plan.
- If `propose_organization_plan` returns a validation error, fix the conflicting paths and call it again cleanly.
"""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


MAX_RECENT_TURNS = 6
HISTORICAL_TOOL_FOLD_THRESHOLD = 320


def compact_messages_for_llm(
    history: list[BaseMessage],
    *,
    max_recent_turns: int = MAX_RECENT_TURNS,
    tool_fold_threshold: int = HISTORICAL_TOOL_FOLD_THRESHOLD,
) -> list[BaseMessage]:
    """Compact multi-turn message history without mutating checkpoint state or breaking tool_call_id pairs.

    Rules:
    - Groups history into logical turns anchored on each `HumanMessage`.
    - Keeps only the most recent `max_recent_turns` (default 6) turns.
    - Preserves all messages of the current active turn verbatim.
    - In earlier retained turns, folds oversized `ToolMessage` payloads (e.g. L1/L2/L3 raw probe dumps)
      into a compact placeholder while keeping `tool_call_id` and `name` intact for OpenAI API validity.
    """
    if not history:
        return []

    turns: list[list[BaseMessage]] = []
    current_turn: list[BaseMessage] = []

    for msg in history:
        if isinstance(msg, HumanMessage):
            if current_turn:
                turns.append(current_turn)
            current_turn = [msg]
        else:
            current_turn.append(msg)

    if current_turn:
        turns.append(current_turn)

    retained_turns = turns[-max_recent_turns:] if max_recent_turns > 0 else turns
    compacted: list[BaseMessage] = []
    last_turn_idx = len(retained_turns) - 1

    for turn_idx, turn_msgs in enumerate(retained_turns):
        is_current_turn = turn_idx == last_turn_idx
        for msg in turn_msgs:
            if not is_current_turn and isinstance(msg, ToolMessage):
                raw_content = msg.content if isinstance(msg.content, str) else json.dumps(msg.content, ensure_ascii=False)
                if len(raw_content) > tool_fold_threshold:
                    tool_label = msg.name or "probe"
                    folded_text = (
                        f"[历史探针结果已折叠 ({tool_label}，原 {len(raw_content)} 字符)，如需最新状态请重新扫描]"
                    )
                    compacted.append(
                        ToolMessage(
                            content=folded_text,
                            tool_call_id=msg.tool_call_id,
                            name=msg.name,
                        )
                    )
                    continue
            compacted.append(msg)

    return compacted


def _make_event(
    thread_id: str,
    event_type: str,
    summary: str = "",
    *,
    probe_level: Optional[str] = None,
    tool_name: Optional[str] = None,
    tool_args: Optional[dict[str, Any]] = None,
    detail: Optional[Any] = None,
) -> dict[str, Any]:
    return TraceEvent(
        event_id=f"evt_{uuid.uuid4().hex[:10]}",
        thread_id=thread_id,
        timestamp=_utc_now_iso(),
        type=event_type,  # type: ignore[arg-type]
        probe_level=probe_level,  # type: ignore[arg-type]
        tool_name=tool_name,
        tool_args=tool_args,
        summary=summary,
        detail=detail,
    ).model_dump()


class AgentState(TypedDict):
    """Persistent LangGraph state stored in checkpointer across HITL interrupt/resume."""

    messages: Annotated[list[BaseMessage], add_messages]
    thread_id: str
    sandbox_root: str
    step_count: int
    pending_plan: Optional[dict[str, Any]]
    human_decision: Optional[dict[str, Any]]
    execution_result: Optional[dict[str, Any]]
    status: Literal["running", "awaiting_approval", "executed", "cancelled", "completed", "error"]


def _summarize_tool_output(tool_name: str, raw_output: str) -> tuple[str, Any]:
    """Produce a concise UI-friendly summary and compact detail payload for SSE trace logs."""
    try:
        parsed = json.loads(raw_output)
    except Exception:
        return raw_output[:160], {"raw": raw_output[:500]}

    if tool_name == "scan_directory_tree":
        files = parsed.get("total_files", 0)
        dirs = parsed.get("total_dirs", 0)
        root = parsed.get("root_path", ".")
        summary = f"L1 扫描完成：共发现 {files} 个文件、{dirs} 个目录 (root={root})"
        return summary, {
            "total_files": files,
            "total_dirs": dirs,
            "truncated": parsed.get("truncated", False),
            "skipped_items": parsed.get("skipped_items", []),
        }

    if tool_name == "inspect_file_head":
        path = parsed.get("path", "")
        ftype = parsed.get("file_type", "file")
        preview = (parsed.get("preview") or "")[:360]
        extra = ""
        if ftype == "pdf" and parsed.get("page_count"):
            extra = f" · PDF {parsed['page_count']} 页"
        elif ftype == "docx" and parsed.get("line_count_estimate") is not None:
            extra = f" · Word {parsed['line_count_estimate']} 段/行"
        elif parsed.get("line_count_estimate"):
            extra = f" · 约 {parsed['line_count_estimate']} 行"
        summary = f"L2 轻量探针读取 `{path}` ({ftype}{extra})"
        return summary, {
            "path": path,
            "file_type": ftype,
            "size_bytes": parsed.get("size_bytes", 0),
            "preview": preview,
            "truncated": parsed.get("truncated", False),
        }

    if tool_name == "read_file_deep":
        path = parsed.get("path", "")
        s_line = parsed.get("start_line", 1)
        e_line = parsed.get("end_line", 0)
        total = parsed.get("total_lines", 0)
        summary = f"L3 深度读取 `{path}` (第 {s_line}-{e_line} / {total} 行)"
        return summary, {
            "path": path,
            "start_line": s_line,
            "end_line": e_line,
            "total_lines": total,
            "preview": (parsed.get("content") or "")[:400],
        }

    if tool_name == "propose_organization_plan":
        stats = parsed.get("stats", {})
        total_ops = stats.get("total_operations", 0)
        summary = f"已生成 Dry-Run 整理提案（共 {total_ops} 项安全变更），准备挂起人工审批"
        return summary, {
            "plan_id": parsed.get("plan_id"),
            "stats": stats,
            "warnings": parsed.get("warnings", []),
        }

    return f"Tool {tool_name} completed", parsed


def _is_local_openai_endpoint(base_url: str) -> bool:
    lowered = (base_url or "").lower()
    return any(host in lowered for host in ("localhost", "127.0.0.1", "0.0.0.0", "host.docker.internal"))


def _build_llm(configurable: dict[str, Any]) -> Any:
    """Instantiate ChatOpenAI (or test mock override) from runtime configurable params."""
    if "llm_override" in configurable and configurable["llm_override"] is not None:
        return configurable["llm_override"]

    api_key = (configurable.get("api_key") or "").strip()
    base_url = (configurable.get("base_url") or settings.openai_base_url).strip()
    model_name = (configurable.get("model_name") or settings.model_name).strip()

    if not api_key:
        if _is_local_openai_endpoint(base_url):
            api_key = "local-no-key-required"
        else:
            raise ValueError(
                "未配置有效的 API Key。请点击顶部「模型与沙盒配置」输入您的 OpenAI 兼容 API Key (BYOK)，"
                "或在项目根目录 `.env` 中配置 `OPENAI_API_KEY`。"
            )

    return ChatOpenAI(
        model=model_name,
        api_key=api_key,
        base_url=base_url,
        temperature=0.1,
        streaming=True,
    )


class SafeWorkspaceAgentRunner:
    """Encapsulates the compiled LangGraph workflow, checkpointer, and SSE streaming runner."""

    def __init__(self, app_settings: Settings = settings) -> None:
        self.app_settings = app_settings
        self.checkpointer = MemorySaver()
        self.graph = self._compile_graph()

    def _compile_graph(self):
        builder = StateGraph(AgentState)

        async def agent_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
            configurable = config.get("configurable", {})
            thread_id = state["thread_id"]
            guard = SandboxGuard(state["sandbox_root"])
            staged_holder: dict[str, Optional[OrganizationPlan]] = {"plan": None}
            tools = create_agent_tools(guard, thread_id, self.app_settings, staged_holder)

            llm = _build_llm(configurable)
            llm_with_tools = llm.bind_tools(tools)

            writer = get_stream_writer()
            compacted_history = compact_messages_for_llm(
                list(state["messages"]),
                max_recent_turns=MAX_RECENT_TURNS,
                tool_fold_threshold=HISTORICAL_TOOL_FOLD_THRESHOLD,
            )
            messages: list[BaseMessage] = [
                SystemMessage(content=PROGRESSIVE_DISCLOSURE_SYSTEM_PROMPT),
                *compacted_history,
            ]

            full_chunk: Optional[AIMessageChunk] = None
            text_buffer = ""
            reasoning_buffer = ""

            # Stream token deltas if supported, gracefully handling mock/non-streaming LLMs
            if hasattr(llm_with_tools, "astream"):
                async for chunk in llm_with_tools.astream(messages):
                    if isinstance(chunk, AIMessageChunk):
                        full_chunk = chunk if full_chunk is None else full_chunk + chunk
                        delta_text = chunk.content if isinstance(chunk.content, str) else ""
                        reasoning_delta = str(
                            chunk.additional_kwargs.get("reasoning_content") or ""
                        )
                        if reasoning_delta:
                            reasoning_buffer += reasoning_delta
                            if len(reasoning_buffer) >= 48 or "\n" in reasoning_buffer:
                                writer(
                                    _make_event(
                                        thread_id,
                                        "thinking",
                                        summary=reasoning_buffer,
                                        detail={"kind": "reasoning_delta"},
                                    )
                                )
                                reasoning_buffer = ""
                        if delta_text:
                            text_buffer += delta_text
                            if len(text_buffer) >= 48 or "\n" in text_buffer:
                                writer(
                                    _make_event(
                                        thread_id,
                                        "thinking",
                                        summary=text_buffer,
                                        detail={"kind": "content_delta"},
                                    )
                                )
                                text_buffer = ""
                    elif isinstance(chunk, AIMessage):
                        full_chunk = AIMessageChunk(
                            content=chunk.content,
                            tool_calls=chunk.tool_calls,
                            additional_kwargs=chunk.additional_kwargs,
                        )
                        if isinstance(chunk.content, str) and chunk.content.strip():
                            text_buffer += chunk.content
            else:
                ai_msg = await llm_with_tools.ainvoke(messages)
                full_chunk = AIMessageChunk(
                    content=ai_msg.content,
                    tool_calls=ai_msg.tool_calls,
                    additional_kwargs=ai_msg.additional_kwargs,
                )
                if isinstance(ai_msg.content, str) and ai_msg.content.strip():
                    text_buffer = ai_msg.content

            if reasoning_buffer:
                writer(
                    _make_event(
                        thread_id,
                        "thinking",
                        summary=reasoning_buffer,
                        detail={"kind": "reasoning_delta"},
                    )
                )
            if text_buffer:
                writer(
                    _make_event(
                        thread_id,
                        "thinking",
                        summary=text_buffer,
                        detail={"kind": "content_delta"},
                    )
                )

            if full_chunk is None:
                final_ai = AIMessage(content="未能从模型获取响应，请检查模型配置后重试。")
            else:
                final_ai = AIMessage(
                    content=full_chunk.content,
                    tool_calls=list(full_chunk.tool_calls or []),
                    additional_kwargs=dict(full_chunk.additional_kwargs or {}),
                    response_metadata=dict(full_chunk.response_metadata or {}),
                )

            next_status = "running" if final_ai.tool_calls else "completed"
            if not final_ai.tool_calls and isinstance(final_ai.content, str) and final_ai.content.strip():
                writer(
                    _make_event(
                        thread_id,
                        "assistant_message",
                        summary=final_ai.content.strip(),
                    )
                )

            return {
                "messages": [final_ai],
                "step_count": state.get("step_count", 0) + 1,
                "status": next_status,
            }

        async def tools_node(state: AgentState) -> dict[str, Any]:
            thread_id = state["thread_id"]
            guard = SandboxGuard(state["sandbox_root"])
            staged_holder: dict[str, Optional[OrganizationPlan]] = {"plan": None}
            tools = create_agent_tools(guard, thread_id, self.app_settings, staged_holder)
            tool_map = {t.name: t for t in tools}

            last_msg = state["messages"][-1]
            tool_calls = getattr(last_msg, "tool_calls", None) or []
            writer = get_stream_writer()

            tool_messages: list[ToolMessage] = []
            for tc in tool_calls:
                name = tc.get("name", "")
                args = tc.get("args") or {}
                call_id = tc.get("id") or f"call_{uuid.uuid4().hex[:8]}"
                probe_level = TOOL_PROBE_LEVELS.get(name)

                target_hint = (
                    args.get("file_path")
                    or args.get("subpath")
                    or (f"{len(args.get('operations', []))} ops" if name == "propose_organization_plan" else "")
                )
                writer(
                    _make_event(
                        thread_id,
                        "tool_start",
                        summary=f"调用 {name}({target_hint})",
                        probe_level=probe_level,
                        tool_name=name,
                        tool_args=args if name != "propose_organization_plan" else {"summary": args.get("summary", ""), "operation_count": len(args.get("operations", []))},
                    )
                )

                tool_obj = tool_map.get(name)
                if tool_obj is None:
                    err_msg = f"Unknown or disallowed tool '{name}'. Only the 4 read/stage tools are permitted."
                    writer(
                        _make_event(
                            thread_id,
                            "security_blocked",
                            summary=err_msg,
                            probe_level=probe_level,
                            tool_name=name,
                        )
                    )
                    tool_messages.append(ToolMessage(content=err_msg, tool_call_id=call_id, name=name))
                    continue

                try:
                    output = tool_obj.invoke(args)
                    output_str = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
                    summary, detail = _summarize_tool_output(name, output_str)
                    writer(
                        _make_event(
                            thread_id,
                            "tool_end",
                            summary=summary,
                            probe_level=probe_level,
                            tool_name=name,
                            detail=detail,
                        )
                    )
                    tool_messages.append(ToolMessage(content=output_str, tool_call_id=call_id, name=name))
                except SandboxSecurityError as sec_err:
                    blocked_text = f"[SANDBOX_GUARD BLOCKED] {sec_err}"
                    writer(
                        _make_event(
                            thread_id,
                            "security_blocked",
                            summary=f"路径越权拦截：{sec_err}",
                            probe_level=probe_level,
                            tool_name=name,
                            detail={"attempted_path": sec_err.attempted_path},
                        )
                    )
                    tool_messages.append(ToolMessage(content=blocked_text, tool_call_id=call_id, name=name))
                except Exception as exc:
                    err_text = f"[TOOL_ERROR] {exc}"
                    writer(
                        _make_event(
                            thread_id,
                            "tool_end",
                            summary=f"工具执行提示：{exc}",
                            probe_level=probe_level,
                            tool_name=name,
                            detail={"error": str(exc)},
                        )
                    )
                    tool_messages.append(ToolMessage(content=err_text, tool_call_id=call_id, name=name))

            staged_plan = staged_holder.get("plan")
            return {
                "messages": tool_messages,
                "pending_plan": staged_plan.model_dump() if staged_plan is not None else state.get("pending_plan"),
                "status": "awaiting_approval" if staged_plan is not None else "running",
            }

        def human_review_node(state: AgentState) -> dict[str, Any]:
            """LangGraph Native Interrupt: suspends graph cleanly awaiting human diff confirmation."""
            plan_payload = state.get("pending_plan")
            decision = interrupt(
                {
                    "type": "human_approval_required",
                    "thread_id": state["thread_id"],
                    "plan": plan_payload,
                }
            )
            approved = bool(decision.get("approved", False)) if isinstance(decision, dict) else False
            approved_ids = (
                list(decision.get("approved_operation_ids") or [])
                if isinstance(decision, dict)
                else []
            )
            if not approved or not approved_ids:
                comment = (decision or {}).get("user_comment") if isinstance(decision, dict) else None
                cancel_msg = "用户已取消本次整理提案，未对本地沙盒执行任何文件写入。"
                if comment:
                    cancel_msg += f" 备注：{comment}"
                return {
                    "human_decision": decision if isinstance(decision, dict) else {"approved": False},
                    "pending_plan": None,
                    "status": "cancelled",
                    "messages": [AIMessage(content=cancel_msg)],
                }

            return {
                "human_decision": decision,
                "status": "running",
            }

        def execute_plan_node(state: AgentState) -> dict[str, Any]:
            """Executes ONLY the human-approved (and optionally manually adjusted) operations via SandboxExecutor."""
            plan_dict = state.get("pending_plan")
            decision = state.get("human_decision") or {}
            if not plan_dict:
                return {
                    "status": "error",
                    "messages": [AIMessage(content="无法执行：状态中未找到挂起的整理提案。")],
                }

            plan = OrganizationPlan.model_validate(plan_dict)
            # Apply human-adjusted operations (drag-and-drop, inline rename, manual mkdir) if provided.
            # Every path is still strictly re-validated by SandboxGuard inside SandboxExecutor.
            override_ops = decision.get("operations")
            if isinstance(override_ops, list):
                from app.schemas import FileOperation

                plan = plan.model_copy(
                    update={
                        "operations": [
                            op if isinstance(op, FileOperation) else FileOperation.model_validate(op)
                            for op in override_ops
                        ]
                    }
                )

            approved_ids = list(decision.get("approved_operation_ids") or [])
            guard = SandboxGuard(state["sandbox_root"])
            executor = SandboxExecutor(guard)
            batch: ExecutionBatchRecord = executor.execute_plan(plan, approved_ids)

            if batch.status == "executed":
                done_msg = (
                    f"已完成事务级安全整理（批次 `{batch.batch_id}`）："
                    f"执行 {batch.approved_count} 项，跳过 {batch.skipped_count} 项。"
                    "回滚日志已保存至 `.agent_history/`，随时支持一键撤销。"
                )
            elif batch.status == "rolled_back_on_error":
                done_msg = (
                    f"执行过程中遇到异常，事务器已自动全量回滚（批次 `{batch.batch_id}`）："
                    f"{batch.error_message}"
                )
            else:
                done_msg = "本次提案已取消，未执行文件修改。"

            return {
                "execution_result": batch.model_dump(),
                "pending_plan": None,
                "status": "executed" if batch.status == "executed" else ("cancelled" if batch.status == "cancelled" else "error"),
                "messages": [AIMessage(content=done_msg)],
            }

        def route_after_agent(state: AgentState) -> str:
            last_msg = state["messages"][-1]
            if getattr(last_msg, "tool_calls", None):
                return "tools"
            return END

        def route_after_tools(state: AgentState) -> str:
            if state.get("pending_plan") is not None:
                return "human_review"
            if state.get("step_count", 0) >= self.app_settings.max_agent_steps:
                return END
            return "agent"

        def route_after_review(state: AgentState) -> str:
            decision = state.get("human_decision") or {}
            if decision.get("approved") and decision.get("approved_operation_ids"):
                return "execute_plan"
            return END

        builder.add_node("agent", agent_node)
        builder.add_node("tools", tools_node)
        builder.add_node("human_review", human_review_node)
        builder.add_node("execute_plan", execute_plan_node)

        builder.add_edge(START, "agent")
        builder.add_conditional_edges("agent", route_after_agent, {"tools": "tools", END: END})
        builder.add_conditional_edges(
            "tools",
            route_after_tools,
            {"human_review": "human_review", "agent": "agent", END: END},
        )
        builder.add_conditional_edges(
            "human_review",
            route_after_review,
            {"execute_plan": "execute_plan", END: END},
        )
        builder.add_edge("execute_plan", END)

        return builder.compile(checkpointer=self.checkpointer)

    async def stream_run(
        self,
        *,
        instruction: str,
        thread_id: Optional[str] = None,
        sandbox_root: Optional[str] = None,
        api_key_override: Optional[str] = None,
        base_url_override: Optional[str] = None,
        model_name_override: Optional[str] = None,
        llm_override: Any = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Run or continue a multi-turn session up to completion or LangGraph `interrupt()`."""
        requested_tid = (thread_id or "").strip() or None
        resolved_root = str(sandbox_root or runtime_config.sandbox_root)

        is_continuation = False
        prior_human_turns = 0
        tid = requested_tid or f"thread_{uuid.uuid4().hex[:10]}"

        # Verify workspace continuity if reusing an existing thread_id
        if requested_tid:
            try:
                existing_snap = await self.graph.aget_state(
                    {"configurable": {"thread_id": requested_tid}}
                )
                if existing_snap and existing_snap.values:
                    prev_root = str(existing_snap.values.get("sandbox_root") or "")
                    if prev_root and prev_root.lower() != resolved_root.lower():
                        # Workspace changed: isolate cleanly into a new thread
                        tid = f"thread_{uuid.uuid4().hex[:10]}"
                    else:
                        prev_msgs = existing_snap.values.get("messages") or []
                        if prev_msgs:
                            is_continuation = True
                            prior_human_turns = sum(
                                1 for m in prev_msgs if isinstance(m, HumanMessage)
                            )
            except Exception:
                pass

        turn_index = prior_human_turns + 1
        api_key, base_url, model_name = runtime_config.get_effective_llm_params(
            api_key_override=api_key_override,
            base_url_override=base_url_override,
            model_name_override=model_name_override,
        )

        run_summary = (
            f"继续多轮会话（第 {turn_index} 轮 · 模型：{model_name}）"
            if is_continuation
            else f"开始分析沙盒工作区（模型：{model_name}）"
        )
        yield _make_event(
            tid,
            "run_started",
            summary=run_summary,
            detail={
                "thread_id": tid,
                "sandbox_root": resolved_root,
                "model_name": model_name,
                "is_continuation": is_continuation,
                "turn_index": turn_index,
            },
        )

        config: RunnableConfig = {
            "configurable": {
                "thread_id": tid,
                "api_key": api_key,
                "base_url": base_url,
                "model_name": model_name,
                "llm_override": llm_override,
            },
            "recursion_limit": self.app_settings.max_agent_steps * 2 + 10,
        }

        initial_state: AgentState = {
            "messages": [HumanMessage(content=instruction)],
            "thread_id": tid,
            "sandbox_root": resolved_root,
            "step_count": 0,
            "pending_plan": None,
            "human_decision": None,
            "execution_result": None,
            "status": "running",
        }

        interrupted_plan: Optional[dict[str, Any]] = None

        try:
            async for mode, payload in self.graph.astream(
                initial_state,
                config=config,
                stream_mode=["custom", "updates"],
            ):
                if mode == "custom" and isinstance(payload, dict):
                    yield payload
                elif mode == "updates" and isinstance(payload, dict):
                    if "__interrupt__" in payload:
                        interrupts = payload["__interrupt__"]
                        if interrupts:
                            first_int = interrupts[0]
                            int_val = getattr(first_int, "value", first_int)
                            if isinstance(int_val, dict):
                                interrupted_plan = int_val.get("plan")
                        yield _make_event(
                            tid,
                            "plan_interrupt",
                            summary="LangGraph 状态已挂起（Human-in-the-Loop），请在右侧审查目录 Diff 并确认执行",
                            probe_level="PLAN",
                            detail=interrupted_plan,
                        )
        except Exception as exc:
            yield _make_event(
                tid,
                "error",
                summary=str(exc),
                detail={"exception_type": type(exc).__name__},
            )

        yield _make_event(
            tid,
            "done",
            summary="等待人工审批" if interrupted_plan else "会话流结束",
            detail={"awaiting_approval": interrupted_plan is not None},
        )

    async def confirm_plan(
        self,
        *,
        thread_id: str,
        plan_id: str,
        approved: bool,
        approved_operation_ids: list[str],
        operations: Optional[list[dict[str, Any]]] = None,
        user_comment: Optional[str] = None,
    ) -> tuple[str, str, Optional[ExecutionBatchRecord], str]:
        """Resume a suspended LangGraph interrupt with the human's granular approval & manual edits."""
        config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
        snapshot = await self.graph.aget_state(config)
        if not snapshot or not snapshot.values:
            raise KeyError(f"Thread '{thread_id}' not found or expired in LangGraph checkpointer.")

        if not snapshot.next:
            raise ValueError(f"Thread '{thread_id}' is not currently suspended at an approval checkpoint.")

        pending = snapshot.values.get("pending_plan")
        if not pending or pending.get("plan_id") != plan_id:
            raise ValueError(
                f"Plan ID mismatch for thread '{thread_id}': expected '{(pending or {}).get('plan_id')}', got '{plan_id}'."
            )

        sandbox_root = str(snapshot.values["sandbox_root"])

        resume_payload: dict[str, Any] = {
            "approved": approved,
            "approved_operation_ids": approved_operation_ids,
            "user_comment": user_comment,
        }
        if operations is not None:
            resume_payload["operations"] = operations

        result_state = await self.graph.ainvoke(
            Command(resume=resume_payload),
            config=config,
        )

        exec_dict = result_state.get("execution_result")
        batch = ExecutionBatchRecord.model_validate(exec_dict) if exec_dict else None
        final_messages = result_state.get("messages") or []
        message_text = (
            str(final_messages[-1].content)
            if final_messages
            else ("已执行所选变更。" if approved else "已取消执行。")
        )

        if not approved or not approved_operation_ids:
            return "cancelled", message_text, batch, sandbox_root
        if batch and batch.status == "rolled_back_on_error":
            return "rolled_back_on_error", message_text, batch, sandbox_root
        return "executed", message_text, batch, sandbox_root


agent_runner = SafeWorkspaceAgentRunner(settings)
