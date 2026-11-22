"""Strict local sandbox layer: guard, L1-L3 progressive readers, and transactional executor."""

from .guard import HISTORY_DIR_NAME, SandboxGuard, SandboxSecurityError
from .readers import inspect_file_head, read_file_deep, scan_directory_tree, seed_demo_workspace
from .executor import SandboxExecutor, stage_organization_plan

__all__ = [
    "HISTORY_DIR_NAME",
    "SandboxGuard",
    "SandboxSecurityError",
    "scan_directory_tree",
    "inspect_file_head",
    "read_file_deep",
    "seed_demo_workspace",
    "stage_organization_plan",
    "SandboxExecutor",
]
