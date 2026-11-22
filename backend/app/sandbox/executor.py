"""Virtual diff staging, transactional executor, and `.agent_history/` Undo journal."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Optional

from app.schemas import (
    DiffStatus,
    ExecutionBatchRecord,
    ExecutionStepLog,
    FileOperation,
    FileTreeNode,
    OrganizationPlan,
    ProposedOperationInput,
)
from .guard import SandboxGuard, SandboxSecurityError
from .readers import scan_directory_tree


_BATCH_ID_RE = re.compile(r"^batch_[a-zA-Z0-9_-]{4,64}$")
_MAX_HASH_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB cap for SHA-256 fingerprint


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _compute_file_sha256(path: Path) -> Optional[str]:
    if not path.is_file():
        return None
    try:
        if path.stat().st_size > _MAX_HASH_SIZE_BYTES:
            return None
        hasher = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                hasher.update(chunk)
        return hasher.hexdigest()
    except OSError:
        return None


def _is_same_path_case_insensitive(a: Path, b: Path) -> bool:
    if os.name == "nt":
        return str(a.resolve(strict=False)).lower() == str(b.resolve(strict=False)).lower()
    return a.resolve(strict=False) == b.resolve(strict=False)


def _move_with_case_safety(src: Path, dst: Path) -> None:
    """Move file/directory safely, handling case-only renames on Windows NTFS."""
    if os.name == "nt" and _is_same_path_case_insensitive(src, dst) and src.name != dst.name:
        tmp = src.with_name(f".__sw_case_tmp_{uuid.uuid4().hex[:8]}_{src.name}")
        os.rename(src, tmp)
        try:
            os.rename(tmp, dst)
        except Exception:
            os.rename(tmp, src)
            raise
        return
    shutil.move(str(src), str(dst))


def _ensure_parents_tracked(guard: SandboxGuard, target_dir: Path) -> list[str]:
    """Create missing directories up to target_dir and return newly created relative POSIX paths."""
    missing: list[Path] = []
    cursor = target_dir
    while cursor != guard.root and not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent

    created_rels: list[str] = []
    for d in reversed(missing):
        d.mkdir(exist_ok=False)
        created_rels.append(guard.to_relative_posix(d))
    return created_rels


def _remove_empty_dirs_safely(guard: SandboxGuard, dir_rels: list[str]) -> None:
    """Remove previously created directories deepest-first ONLY if they are now completely empty."""
    # Sort deepest path first
    sorted_rels = sorted(
        set(dir_rels),
        key=lambda p: len(PurePosixPath(p).parts),
        reverse=True,
    )
    for rel in sorted_rels:
        try:
            path = guard.resolve_safe_path(rel, allow_root=False)
        except SandboxSecurityError:
            continue
        if path.exists() and path.is_dir():
            try:
                if not any(path.iterdir()):
                    path.rmdir()
            except OSError:
                # Non-empty or locked; do not force delete user files!
                pass


def _collect_tree_maps(
    node: FileTreeNode,
    files_map: dict[str, dict[str, Any]],
    dirs_set: set[str],
) -> None:
    if node.type == "dir":
        if node.path != ".":
            dirs_set.add(node.path)
        for child in node.children:
            _collect_tree_maps(child, files_map, dirs_set)
    else:
        files_map[node.path] = {
            "name": node.name,
            "path": node.path,
            "size": node.size,
            "extension": node.extension,
            "modified_at": node.modified_at,
            "diff_status": "unchanged",
            "operation_id": None,
            "related_path": None,
        }


def _annotate_before_tree(
    node: FileTreeNode,
    source_annotations: dict[str, tuple[DiffStatus, str, str]],
) -> FileTreeNode:
    annotated = node.model_copy(deep=True)

    def _apply(n: FileTreeNode) -> None:
        if n.path in source_annotations:
            status, op_id, target = source_annotations[n.path]
            n.diff_status = status
            n.operation_id = op_id
            n.related_path = target
        for child in n.children:
            _apply(child)

    _apply(annotated)
    return annotated


def _build_after_tree(
    root_name: str,
    virtual_files: dict[str, dict[str, Any]],
    virtual_dirs: dict[str, dict[str, Any]],
) -> FileTreeNode:
    """Reconstruct a sorted recursive FileTreeNode from simulated virtual state."""
    root = FileTreeNode(name=root_name, path=".", type="dir", children=[])
    dir_nodes: dict[str, FileTreeNode] = {".": root}

    def _get_or_create_dir(dir_rel: str) -> FileTreeNode:
        if dir_rel in dir_nodes:
            return dir_nodes[dir_rel]
        parts = PurePosixPath(dir_rel).parts
        parent_rel = "." if len(parts) == 1 else PurePosixPath(*parts[:-1]).as_posix()
        parent_node = _get_or_create_dir(parent_rel)
        meta = virtual_dirs.get(dir_rel, {})
        node = FileTreeNode(
            name=parts[-1],
            path=dir_rel,
            type="dir",
            diff_status=meta.get("diff_status", "unchanged"),
            operation_id=meta.get("operation_id"),
            related_path=meta.get("related_path"),
            children=[],
        )
        dir_nodes[dir_rel] = node
        parent_node.children.append(node)
        return node

    for d_rel in sorted(virtual_dirs.keys(), key=lambda p: (len(PurePosixPath(p).parts), p.lower())):
        _get_or_create_dir(d_rel)

    for f_rel, f_meta in virtual_files.items():
        parts = PurePosixPath(f_rel).parts
        parent_rel = "." if len(parts) == 1 else PurePosixPath(*parts[:-1]).as_posix()
        parent_node = _get_or_create_dir(parent_rel)
        parent_node.children.append(
            FileTreeNode(
                name=parts[-1],
                path=f_rel,
                type="file",
                size=f_meta.get("size", 0),
                extension=PurePosixPath(f_rel).suffix.lower() or None,
                modified_at=f_meta.get("modified_at"),
                diff_status=f_meta.get("diff_status", "unchanged"),
                operation_id=f_meta.get("operation_id"),
                related_path=f_meta.get("related_path"),
                children=[],
            )
        )

    def _sort_tree(n: FileTreeNode) -> None:
        n.children.sort(key=lambda c: (0 if c.type == "dir" else 1, c.name.lower()))
        for c in n.children:
            _sort_tree(c)

    _sort_tree(root)
    return root


def stage_organization_plan(
    guard: SandboxGuard,
    thread_id: str,
    summary: str,
    proposed_ops: list[ProposedOperationInput],
) -> OrganizationPlan:
    """Validate proposed operations against SandboxGuard and simulate Before/After virtual diff.

    ZERO disk modifications happen here.
    Raises SandboxSecurityError if ANY source or target tries to escape SANDBOX_ROOT.
    Raises ValueError if proposed operations contain invalid sources or collisions.
    """
    if not proposed_ops:
        raise ValueError("Organization plan must contain at least one operation.")

    scan = scan_directory_tree(guard, ".", max_depth=6, max_entries=1500)
    raw_before_tree = scan.tree

    virtual_files: dict[str, dict[str, Any]] = {}
    existing_dirs: set[str] = set()
    _collect_tree_maps(raw_before_tree, virtual_files, existing_dirs)
    virtual_dirs: dict[str, dict[str, Any]] = {
        d: {"diff_status": "unchanged", "operation_id": None, "related_path": None}
        for d in existing_dirs
    }

    validated_ops: list[FileOperation] = []
    source_annotations: dict[str, tuple[DiffStatus, str, str]] = {}
    warnings: list[str] = []
    moved_sources: set[str] = set()
    moved_dir_map: list[tuple[str, str]] = []

    if scan.truncated:
        warnings.append(
            "Workspace contains more entries than the L1 preview limit; tree preview may be truncated."
        )

    # Order mkdir before file/dir moves for clean deterministic simulation
    ordered_inputs = sorted(
        proposed_ops,
        key=lambda op: 0 if op.op_type == "mkdir" else 1,
    )

    def _find_moved_ancestor(rel_path: str) -> Optional[tuple[str, str, str]]:
        # Check most recently moved matching ancestor prefix
        for old_dir, new_dir in reversed(moved_dir_map):
            prefix = old_dir + "/"
            if rel_path.startswith(prefix):
                remapped = new_dir + "/" + rel_path[len(prefix) :]
                return old_dir, new_dir, remapped
        return None

    op_counter = 0
    for item in ordered_inputs:
        # Validate target via SandboxGuard (throws SandboxSecurityError on escape!)
        target_path = guard.resolve_safe_path(
            item.target, allow_root=False, validate_target_names=True
        )
        target_rel = guard.to_relative_posix(target_path)

        if item.op_type == "mkdir":
            if target_rel in virtual_files:
                raise ValueError(
                    f"Cannot mkdir '{target_rel}': a file already exists at that path."
                )
            if target_rel in virtual_dirs:
                # Redundant mkdir for existing dir — skip softly with warning
                warnings.append(f"Skipped redundant mkdir '{target_rel}' (already exists).")
                continue

            op_counter += 1
            op_id = f"op_{op_counter:03d}"
            # Ensure intermediate parent virtual dirs are marked added if new
            parts = PurePosixPath(target_rel).parts
            for idx in range(1, len(parts) + 1):
                sub = PurePosixPath(*parts[:idx]).as_posix()
                if sub not in virtual_dirs:
                    virtual_dirs[sub] = {
                        "diff_status": "added",
                        "operation_id": op_id,
                        "related_path": None,
                    }

            validated_ops.append(
                FileOperation(
                    id=op_id,
                    op_type="mkdir",
                    source=None,
                    target=target_rel,
                    reason=item.reason.strip(),
                    probe_evidence=item.probe_evidence,
                    category=item.category,
                    size_bytes=0,
                )
            )
            continue

        # move / rename / archive require a valid source inside sandbox
        if not item.source or not item.source.strip():
            raise ValueError(f"Operation '{item.op_type}' targeting '{target_rel}' is missing 'source'.")

        source_path = guard.resolve_safe_path(item.source, allow_root=False)
        source_rel = guard.to_relative_posix(source_path)

        if source_rel == target_rel:
            warnings.append(f"Skipped no-op {item.op_type} where source == target ('{source_rel}').")
            continue

        is_file_src = source_rel in virtual_files
        is_dir_src = source_rel in virtual_dirs
        if not is_file_src and not is_dir_src:
            if source_rel in moved_sources:
                raise ValueError(
                    f"Operation conflict: source path '{source_rel}' was already moved by an earlier operation."
                )
            moved_ancestor = _find_moved_ancestor(source_rel)
            if moved_ancestor is not None:
                old_dir, new_dir, remapped = moved_ancestor
                raise ValueError(
                    f"Operation order conflict: '{source_rel}' is inside directory '{old_dir}' "
                    f"which was already moved to '{new_dir}' by an earlier operation. "
                    f"Either move/rename child files BEFORE moving '{old_dir}', move individual files "
                    f"instead of moving the parent directory, or reference the post-move path '{remapped}'."
                )

            # Double check disk ONLY when not moved/affected, in case tree was deeper than scan depth
            if source_path.is_file():
                st = source_path.stat()
                virtual_files[source_rel] = {
                    "name": source_path.name,
                    "path": source_rel,
                    "size": st.st_size,
                    "extension": source_path.suffix.lower() or None,
                    "modified_at": _utc_now_iso(),
                    "diff_status": "unchanged",
                    "operation_id": None,
                    "related_path": None,
                }
                is_file_src = True
            elif source_path.is_dir():
                virtual_dirs[source_rel] = {
                    "diff_status": "unchanged",
                    "operation_id": None,
                    "related_path": None,
                }
                is_dir_src = True
            else:
                raise ValueError(
                    f"Source path '{source_rel}' does not exist in sandbox (or was already moved)."
                )

        # Prevent moving a directory into itself
        if is_dir_src and target_rel.startswith(source_rel + "/"):
            raise ValueError(
                f"Cannot move directory '{source_rel}' inside itself ('{target_rel}')."
            )

        # Collision check (allow case-only rename on Windows)
        same_case_insensitive = source_rel.lower() == target_rel.lower()
        if not same_case_insensitive and (
            target_rel in virtual_files or target_rel in virtual_dirs
        ):
            raise ValueError(
                f"Target collision detected: '{target_rel}' already exists in planned workspace."
            )

        op_counter += 1
        op_id = f"op_{op_counter:03d}"

        # Ensure parent dirs exist in virtual_dirs
        target_parts = PurePosixPath(target_rel).parts
        for idx in range(1, len(target_parts)):
            parent_rel = PurePosixPath(*target_parts[:idx]).as_posix()
            if parent_rel in virtual_files:
                raise ValueError(
                    f"Cannot place '{target_rel}' under '{parent_rel}' because '{parent_rel}' is a file."
                )
            if parent_rel not in virtual_dirs:
                virtual_dirs[parent_rel] = {
                    "diff_status": "added",
                    "operation_id": op_id,
                    "related_path": None,
                }

        before_status: DiffStatus = "renamed_from" if item.op_type == "rename" else "moved_from"
        after_status: DiffStatus = (
            "renamed_to"
            if item.op_type == "rename"
            else ("archived_to" if item.op_type == "archive" else "moved_to")
        )
        source_annotations[source_rel] = (before_status, op_id, target_rel)

        size_bytes = 0
        moved_sources.add(source_rel)
        if is_file_src:
            f_meta = virtual_files.pop(source_rel)
            size_bytes = int(f_meta.get("size", 0))
            virtual_files[target_rel] = {
                **f_meta,
                "name": target_parts[-1],
                "path": target_rel,
                "diff_status": after_status,
                "operation_id": op_id,
                "related_path": source_rel,
            }
        else:
            # Move directory and remap all nested virtual files & subdirs
            moved_dir_map.append((source_rel, target_rel))
            virtual_dirs.pop(source_rel, None)
            virtual_dirs[target_rel] = {
                "diff_status": after_status,
                "operation_id": op_id,
                "related_path": source_rel,
            }
            prefix = source_rel + "/"
            nested_dirs = [d for d in list(virtual_dirs.keys()) if d.startswith(prefix)]
            for nd in nested_dirs:
                meta = virtual_dirs.pop(nd)
                new_nd = target_rel + "/" + nd[len(prefix) :]
                virtual_dirs[new_nd] = {**meta, "diff_status": after_status, "operation_id": op_id}
            nested_files = [f for f in list(virtual_files.keys()) if f.startswith(prefix)]
            for nf in nested_files:
                fmeta = virtual_files.pop(nf)
                size_bytes += int(fmeta.get("size", 0))
                new_nf = target_rel + "/" + nf[len(prefix) :]
                virtual_files[new_nf] = {
                    **fmeta,
                    "path": new_nf,
                    "diff_status": after_status,
                    "operation_id": op_id,
                    "related_path": nf,
                }

        validated_ops.append(
            FileOperation(
                id=op_id,
                op_type=item.op_type,
                source=source_rel,
                target=target_rel,
                reason=item.reason.strip(),
                probe_evidence=item.probe_evidence,
                category=item.category,
                size_bytes=size_bytes,
            )
        )

    if not validated_ops:
        raise ValueError("All proposed operations were no-ops or redundant; nothing to stage.")

    before_tree = _annotate_before_tree(raw_before_tree, source_annotations)
    after_tree = _build_after_tree(raw_before_tree.name, virtual_files, virtual_dirs)

    stats = {
        "total_operations": len(validated_ops),
        "mkdir_count": sum(1 for o in validated_ops if o.op_type == "mkdir"),
        "move_count": sum(1 for o in validated_ops if o.op_type == "move"),
        "rename_count": sum(1 for o in validated_ops if o.op_type == "rename"),
        "archive_count": sum(1 for o in validated_ops if o.op_type == "archive"),
        "affected_files": sum(1 for o in validated_ops if o.op_type != "mkdir"),
    }

    return OrganizationPlan(
        plan_id=f"plan_{uuid.uuid4().hex[:10]}",
        thread_id=thread_id,
        summary=summary.strip(),
        created_at=_utc_now_iso(),
        operations=validated_ops,
        before_tree=before_tree,
        after_tree=after_tree,
        warnings=warnings,
        stats=stats,
    )


class SandboxExecutor:
    """Transactional filesystem executor with atomic rollback & `.agent_history/` Undo log."""

    def __init__(self, guard: SandboxGuard) -> None:
        self.guard = guard

    def _manifest_path(self, batch_id: str) -> Path:
        if not _BATCH_ID_RE.match(batch_id):
            raise SandboxSecurityError(
                f"Invalid batch_id format: '{batch_id}'.", attempted_path=batch_id
            )
        return self.guard.ensure_history_dir() / f"{batch_id}.json"

    def _save_manifest(self, record: ExecutionBatchRecord) -> None:
        manifest_path = self._manifest_path(record.batch_id)
        tmp_path = manifest_path.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(record.model_dump(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp_path, manifest_path)

    def _rollback_completed_steps(self, completed_steps: list[ExecutionStepLog]) -> None:
        """Best-effort reverse replay of completed steps after a mid-batch failure."""
        for step in reversed(completed_steps):
            if step.status != "completed":
                continue
            try:
                if step.op_type == "mkdir":
                    _remove_empty_dirs_safely(self.guard, step.created_dirs)
                elif step.source:
                    src_path = self.guard.resolve_safe_path(step.source, allow_root=False)
                    dst_path = self.guard.resolve_safe_path(step.target, allow_root=False)
                    if dst_path.exists():
                        src_path.parent.mkdir(parents=True, exist_ok=True)
                        _move_with_case_safety(dst_path, src_path)
                    _remove_empty_dirs_safely(self.guard, step.created_dirs)
                step.status = "reverted"
            except Exception as rb_err:
                step.error = f"Rollback warning: {rb_err}"

    def execute_plan(
        self,
        plan: OrganizationPlan,
        approved_operation_ids: list[str],
    ) -> ExecutionBatchRecord:
        """Execute approved operations transactionally; auto-rollback all on mid-batch failure."""
        approved_set = set(approved_operation_ids)
        selected_ops = [op for op in plan.operations if op.id in approved_set]
        skipped_ops = [op for op in plan.operations if op.id not in approved_set]

        batch_id = f"batch_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"

        if not selected_ops:
            record = ExecutionBatchRecord(
                batch_id=batch_id,
                plan_id=plan.plan_id,
                thread_id=plan.thread_id,
                summary=plan.summary,
                status="cancelled",
                created_at=_utc_now_iso(),
                approved_count=0,
                skipped_count=len(skipped_ops),
                steps=[
                    ExecutionStepLog(
                        operation_id=op.id,
                        op_type=op.op_type,
                        source=op.source,
                        target=op.target,
                        status="skipped",
                    )
                    for op in skipped_ops
                ],
            )
            return record

        # Deterministic execution order: mkdir first, then moves/renames/archives
        selected_ops.sort(key=lambda op: 0 if op.op_type == "mkdir" else 1)

        # Defense-in-depth pre-flight check before touching disk (supports chained dir+child ops)
        produced_targets: set[str] = set()
        planned_dir_moves: list[tuple[str, str]] = []
        for op in selected_ops:
            self.guard.resolve_safe_path(op.target, allow_root=False, validate_target_names=True)
            if op.op_type != "mkdir":
                if not op.source:
                    raise ValueError(f"Missing source for operation {op.id}")
                src_resolved = self.guard.resolve_safe_path(op.source, allow_root=False)
                src_rel = self.guard.to_relative_posix(src_resolved)
                target_rel_norm = self.guard.to_relative_posix(
                    self.guard.resolve_safe_path(op.target, allow_root=False)
                )
                produced_by_prior = src_rel in produced_targets or any(
                    src_rel.startswith(new_d + "/") for _, new_d in planned_dir_moves
                )
                if not produced_by_prior and not src_resolved.exists():
                    raise FileNotFoundError(f"Path does not exist in sandbox: '{src_rel}'.")
                produced_targets.add(target_rel_norm)
                if src_resolved.is_dir():
                    planned_dir_moves.append((src_rel, target_rel_norm))

        completed_steps: list[ExecutionStepLog] = []
        all_steps: list[ExecutionStepLog] = []

        for op in skipped_ops:
            all_steps.append(
                ExecutionStepLog(
                    operation_id=op.id,
                    op_type=op.op_type,
                    source=op.source,
                    target=op.target,
                    status="skipped",
                )
            )

        try:
            for op in selected_ops:
                target_path = self.guard.resolve_safe_path(
                    op.target, allow_root=False, validate_target_names=True
                )

                if op.op_type == "mkdir":
                    if target_path.exists():
                        if not target_path.is_dir():
                            raise FileExistsError(
                                f"Cannot mkdir '{op.target}': file already exists."
                            )
                        step = ExecutionStepLog(
                            operation_id=op.id,
                            op_type="mkdir",
                            source=None,
                            target=op.target,
                            status="completed",
                            created_dirs=[],
                        )
                    else:
                        created_dirs = _ensure_parents_tracked(self.guard, target_path)
                        step = ExecutionStepLog(
                            operation_id=op.id,
                            op_type="mkdir",
                            source=None,
                            target=op.target,
                            status="completed",
                            created_dirs=created_dirs,
                        )
                    completed_steps.append(step)
                    all_steps.append(step)
                    continue

                assert op.source is not None
                src_path = self.guard.resolve_safe_path(op.source, allow_root=False, must_exist=True)

                case_only_rename = (
                    os.name == "nt"
                    and _is_same_path_case_insensitive(src_path, target_path)
                    and src_path.name != target_path.name
                )
                if target_path.exists() and not case_only_rename:
                    raise FileExistsError(
                        f"Target already exists and would be overwritten: '{op.target}'."
                    )

                file_hash = _compute_file_sha256(src_path)
                created_dirs = _ensure_parents_tracked(self.guard, target_path.parent)
                try:
                    _move_with_case_safety(src_path, target_path)
                except Exception:
                    _remove_empty_dirs_safely(self.guard, created_dirs)
                    raise

                step = ExecutionStepLog(
                    operation_id=op.id,
                    op_type=op.op_type,
                    source=op.source,
                    target=op.target,
                    status="completed",
                    created_dirs=created_dirs,
                    file_hash=file_hash,
                )
                completed_steps.append(step)
                all_steps.append(step)

        except Exception as exc:
            # Transactional rollback of all previously completed steps!
            self._rollback_completed_steps(completed_steps)
            failed_step = ExecutionStepLog(
                operation_id=op.id,
                op_type=op.op_type,
                source=op.source,
                target=op.target,
                status="failed",
                error=str(exc),
            )
            all_steps.append(failed_step)
            record = ExecutionBatchRecord(
                batch_id=batch_id,
                plan_id=plan.plan_id,
                thread_id=plan.thread_id,
                summary=plan.summary,
                status="rolled_back_on_error",
                created_at=_utc_now_iso(),
                approved_count=len(selected_ops),
                skipped_count=len(skipped_ops),
                steps=all_steps,
                error_message=f"Transaction failed and rolled back cleanly: {exc}",
            )
            self._save_manifest(record)
            return record

        record = ExecutionBatchRecord(
            batch_id=batch_id,
            plan_id=plan.plan_id,
            thread_id=plan.thread_id,
            summary=plan.summary,
            status="executed",
            created_at=_utc_now_iso(),
            approved_count=len(selected_ops),
            skipped_count=len(skipped_ops),
            steps=all_steps,
        )
        self._save_manifest(record)
        return record

    def list_batches(self) -> list[ExecutionBatchRecord]:
        """List all persisted execution batches from `.agent_history/`, newest first."""
        hist_dir = self.guard.history_dir
        if not hist_dir.exists():
            return []

        records: list[ExecutionBatchRecord] = []
        for entry in hist_dir.glob("batch_*.json"):
            try:
                data = json.loads(entry.read_text(encoding="utf-8"))
                records.append(ExecutionBatchRecord.model_validate(data))
            except Exception:
                continue

        records.sort(key=lambda r: r.created_at, reverse=True)
        return records

    def get_batch(self, batch_id: str) -> ExecutionBatchRecord:
        manifest_path = self._manifest_path(batch_id)
        if not manifest_path.exists():
            raise FileNotFoundError(f"History batch not found: '{batch_id}'.")
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        return ExecutionBatchRecord.model_validate(data)

    def undo_batch(self, batch_id: str) -> ExecutionBatchRecord:
        """Atomically revert all completed steps of an executed batch in reverse order."""
        record = self.get_batch(batch_id)
        if record.status == "reverted":
            raise ValueError(f"Batch '{batch_id}' has already been reverted.")
        if record.status != "executed":
            raise ValueError(
                f"Cannot undo batch '{batch_id}' with status '{record.status}'."
            )

        completed_steps = [s for s in record.steps if s.status == "completed"]

        # Pre-flight verification before mutating disk (accounts for chained moves & dir+child ops)
        vacated_paths: set[str] = set()
        restored_dirs: list[tuple[str, str]] = []
        for step in reversed(completed_steps):
            if step.op_type == "mkdir":
                continue
            assert step.source is not None
            dst_path = self.guard.resolve_safe_path(step.target, allow_root=False)
            src_path = self.guard.resolve_safe_path(step.source, allow_root=False)
            dst_rel = self.guard.to_relative_posix(dst_path)
            src_rel = self.guard.to_relative_posix(src_path)
            dst_key = str(dst_path).lower() if os.name == "nt" else str(dst_path)
            src_key = str(src_path).lower() if os.name == "nt" else str(src_path)

            # Check if dst currently sits inside a directory that an earlier reversed step will restore
            dst_exists_now = dst_path.exists()
            if not dst_exists_now:
                for restored_src_rel, current_dst_rel in restored_dirs:
                    prefix = restored_src_rel + "/"
                    if dst_rel.startswith(prefix):
                        mapped_rel = current_dst_rel + "/" + dst_rel[len(prefix) :]
                        mapped_path = self.guard.resolve_safe_path(mapped_rel, allow_root=False)
                        if mapped_path.exists():
                            dst_exists_now = True
                            break

            if not dst_exists_now and dst_key not in vacated_paths:
                raise FileNotFoundError(
                    f"Cannot undo '{batch_id}': moved item '{step.target}' no longer exists on disk."
                )
            case_only = (
                os.name == "nt"
                and _is_same_path_case_insensitive(src_path, dst_path)
                and src_path.name != dst_path.name
            )
            if src_path.exists() and not case_only and src_key not in vacated_paths:
                raise FileExistsError(
                    f"Cannot undo '{batch_id}': original path '{step.source}' is now occupied."
                )
            vacated_paths.add(dst_key)
            if dst_path.is_dir():
                restored_dirs.append((src_rel, dst_rel))

        # Replay in strict reverse order
        for step in reversed(completed_steps):
            if step.op_type == "mkdir":
                _remove_empty_dirs_safely(self.guard, step.created_dirs)
                step.status = "reverted"
                continue

            assert step.source is not None
            dst_path = self.guard.resolve_safe_path(step.target, allow_root=False)
            src_path = self.guard.resolve_safe_path(step.source, allow_root=False)
            src_path.parent.mkdir(parents=True, exist_ok=True)
            _move_with_case_safety(dst_path, src_path)
            _remove_empty_dirs_safely(self.guard, step.created_dirs)
            step.status = "reverted"

        record.status = "reverted"
        record.reverted_at = _utc_now_iso()
        self._save_manifest(record)
        return record
