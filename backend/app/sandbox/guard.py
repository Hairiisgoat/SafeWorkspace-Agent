"""Strict SANDBOX_ROOT path security guard defending against path traversal and escapes."""

from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath


HISTORY_DIR_NAME = ".agent_history"
PROTECTED_DIR_NAMES = frozenset({HISTORY_DIR_NAME, ".git"})

RESERVED_WINDOWS_NAMES = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{i}" for i in range(1, 10)),
        *(f"lpt{i}" for i in range(1, 10)),
    }
)

_WINDOWS_DRIVE_RE = re.compile(r"^[a-zA-Z]:")
_INVALID_SEGMENT_CHARS = set('<>"|?*')


class SandboxSecurityError(PermissionError):
    """Raised whenever an agent or caller attempts to access or escape outside SANDBOX_ROOT."""

    def __init__(self, message: str, attempted_path: str = "") -> None:
        super().__init__(message)
        self.attempted_path = attempted_path


class SandboxGuard:
    """Enforces strict directory containment within a single resolved SANDBOX_ROOT."""

    @staticmethod
    def _normalize_cmp(path: Path) -> str:
        text = str(path.resolve(strict=False)).rstrip("\\/")
        return text.lower() if os.name == "nt" else text

    @classmethod
    def validate_sandbox_root(
        cls,
        sandbox_root: str | Path,
        *,
        must_exist: bool = True,
    ) -> Path:
        """Validate that a candidate SANDBOX_ROOT is safe to mount.

        Strictly blocks:
        - Drive / filesystem roots (e.g., `C:\\`, `D:\\`, `/`)
        - OS system directories and their subdirectories (`Windows`, `Program Files`, `/etc`, `/System`, ...)
        - OS users root (`/Users`, `C:\\Users`, `/home`) and the current user's home root directory (`~`)
        """
        raw_str = str(sandbox_root or "").strip()
        if not raw_str:
            raise SandboxSecurityError("SANDBOX_ROOT 路径不能为空。", attempted_path=raw_str)
        if "\x00" in raw_str:
            raise SandboxSecurityError("SANDBOX_ROOT 包含非法空字节。", attempted_path=raw_str)
        if raw_str.startswith("\\\\") or raw_str.startswith("//"):
            raise SandboxSecurityError(
                f"禁止挂载 UNC 网络路径作为沙盒根目录：'{raw_str}'。",
                attempted_path=raw_str,
            )

        resolved = Path(raw_str).expanduser().resolve(strict=False)

        # 1. Block filesystem / drive roots (e.g. '/', 'C:\\', 'D:\\')
        if resolved == resolved.parent:
            raise SandboxSecurityError(
                f"安全拦截：禁止挂载盘符或文件系统根目录 '{resolved}' 作为沙盒工作区，请选择具体业务/个人子目录。",
                attempted_path=raw_str,
            )

        norm = cls._normalize_cmp(resolved)

        # 2. Block current user home directory root & OS users container (e.g. C:\Users\<user>, C:\Users, /Users, /home)
        try:
            home_dir = Path.home().resolve(strict=False)
            home_norm = cls._normalize_cmp(home_dir)
            users_parent_norm = cls._normalize_cmp(home_dir.parent)
            if norm == home_norm:
                raise SandboxSecurityError(
                    f"安全拦截：禁止直接挂载用户主目录根层级 '{resolved}'，请在主目录下选择具体的子文件夹（如桌面或文档下的某个项目文件夹）。",
                    attempted_path=raw_str,
                )
            if home_dir.parent != home_dir.parent.parent and norm == users_parent_norm:
                raise SandboxSecurityError(
                    f"安全拦截：禁止挂载系统用户根目录 '{resolved}' 作为沙盒工作区。",
                    attempted_path=raw_str,
                )
        except SandboxSecurityError:
            raise
        except Exception:
            pass

        # 3. Block OS system directories (exact match OR inside system directories)
        if os.name == "nt":
            win_forbidden_exact: set[str] = {"c:\\users"}
            drive = resolved.drive.lower()
            if drive:
                win_forbidden_exact.add(f"{drive}\\users")
            if norm in win_forbidden_exact:
                raise SandboxSecurityError(
                    f"安全拦截：禁止挂载系统用户根目录 '{resolved}' 作为沙盒工作区。",
                    attempted_path=raw_str,
                )

            win_forbidden_roots: set[str] = {
                "c:\\windows",
                "c:\\program files",
                "c:\\program files (x86)",
                "c:\\programdata",
                "c:\\recovery",
                "c:\\perflogs",
                "c:\\system volume information",
                "c:\\$recycle.bin",
            }
            for env_var in ("SystemRoot", "WINDIR", "ProgramFiles", "ProgramFiles(x86)", "ProgramData"):
                val = os.environ.get(env_var)
                if val:
                    win_forbidden_roots.add(cls._normalize_cmp(Path(val)))

            # Also block any drive's top-level Windows / Program Files / $Recycle.Bin / System Volume Information
            if drive:
                for sys_name in ("windows", "program files", "program files (x86)", "programdata", "system volume information", "$recycle.bin"):
                    win_forbidden_roots.add(f"{drive}\\{sys_name}")

            for forbidden in win_forbidden_roots:
                if norm == forbidden or norm.startswith(forbidden + "\\"):
                    raise SandboxSecurityError(
                        f"安全拦截：禁止挂载操作系统受保护目录 '{resolved}' 作为沙盒工作区。",
                        attempted_path=raw_str,
                    )
        else:
            posix_forbidden_roots = (
                "/etc",
                "/usr",
                "/bin",
                "/sbin",
                "/var",
                "/sys",
                "/proc",
                "/dev",
                "/boot",
                "/lib",
                "/lib64",
                "/root",
                "/system",
                "/library",
                "/applications",
                "/private/etc",
                "/private/var",
            )
            norm_lower = norm.lower()
            for forbidden in posix_forbidden_roots:
                if norm_lower == forbidden or norm_lower.startswith(forbidden + "/"):
                    raise SandboxSecurityError(
                        f"安全拦截：禁止挂载操作系统受保护目录 '{resolved}' 作为沙盒工作区。",
                        attempted_path=raw_str,
                    )
            if norm_lower in ("/users", "/home"):
                raise SandboxSecurityError(
                    f"安全拦截：禁止挂载用户根目录 '{resolved}' 作为沙盒工作区。",
                    attempted_path=raw_str,
                )

        if must_exist:
            if not resolved.exists():
                raise FileNotFoundError(f"指定的沙盒目录不存在：{resolved}")
            if not resolved.is_dir():
                raise NotADirectoryError(f"指定路径不是有效目录：{resolved}")

        return resolved

    def __init__(self, sandbox_root: str | Path) -> None:
        self.root: Path = self.validate_sandbox_root(sandbox_root, must_exist=True)

    @property
    def history_dir(self) -> Path:
        """Internal trusted directory for transactional rollback manifests."""
        return self.root / HISTORY_DIR_NAME

    def ensure_history_dir(self) -> Path:
        """Create `.agent_history/` inside SANDBOX_ROOT if it does not yet exist."""
        self.history_dir.mkdir(parents=True, exist_ok=True)
        return self.history_dir

    def to_relative_posix(self, path: Path) -> str:
        """Convert a verified path inside SANDBOX_ROOT to a clean relative POSIX string."""
        resolved = path.resolve(strict=False)
        if not resolved.is_relative_to(self.root):
            raise SandboxSecurityError(
                f"Path '{path}' is outside SANDBOX_ROOT '{self.root}'.",
                attempted_path=str(path),
            )
        rel = resolved.relative_to(self.root)
        if not rel.parts:
            return "."
        return PurePosixPath(*rel.parts).as_posix()

    def is_protected_relative(self, rel_posix: str) -> bool:
        parts = PurePosixPath(rel_posix).parts
        return any(part in PROTECTED_DIR_NAMES for part in parts)

    def _validate_segments(self, parts: tuple[str, ...], raw_path: str) -> None:
        for seg in parts:
            if not seg or seg in (".", ".."):
                continue
            if ":" in seg:
                raise SandboxSecurityError(
                    f"Illegal colon/ADS character in path segment '{seg}'.",
                    attempted_path=raw_path,
                )
            if any(ch in _INVALID_SEGMENT_CHARS for ch in seg):
                raise SandboxSecurityError(
                    f"Illegal filesystem character in path segment '{seg}'.",
                    attempted_path=raw_path,
                )
            stem = seg.split(".", 1)[0].rstrip(" ").lower()
            if stem in RESERVED_WINDOWS_NAMES:
                raise SandboxSecurityError(
                    f"Reserved OS device filename blocked: '{seg}'.",
                    attempted_path=raw_path,
                )
            if seg.endswith(" ") or seg.endswith("."):
                raise SandboxSecurityError(
                    f"Trailing whitespace or dot in path segment blocked: '{seg}'.",
                    attempted_path=raw_path,
                )

    def _check_symlink_chain(self, target_path: Path, raw_path: str) -> None:
        """Verify that neither target_path nor any existing ancestor symlink escapes SANDBOX_ROOT."""
        try:
            rel_parts = target_path.relative_to(self.root).parts
        except ValueError as exc:
            raise SandboxSecurityError(
                f"Path escapes SANDBOX_ROOT: '{raw_path}'.",
                attempted_path=raw_path,
            ) from exc

        cursor = self.root
        for part in rel_parts:
            cursor = cursor / part
            if cursor.is_symlink():
                link_target = cursor.resolve(strict=False)
                if not link_target.is_relative_to(self.root):
                    raise SandboxSecurityError(
                        f"Symlink escape blocked: '{raw_path}' points outside SANDBOX_ROOT.",
                        attempted_path=raw_path,
                    )

    def resolve_safe_path(
        self,
        raw_path: str,
        *,
        allow_root: bool = False,
        must_exist: bool = False,
        allow_protected: bool = False,
        validate_target_names: bool = False,
    ) -> Path:
        """Validate and resolve a path strictly inside SANDBOX_ROOT.

        Blocks:
        - Null bytes, UNC paths, alternate data streams
        - Directory traversal (`../`, `..\\`, mixed slashes)
        - Absolute paths pointing outside SANDBOX_ROOT
        - Symlinks resolving outside SANDBOX_ROOT
        - Protected internal directories (`.agent_history`, `.git`)
        """
        if raw_path is None:
            raise SandboxSecurityError("Path cannot be None.", attempted_path="")

        cleaned = raw_path.strip()
        if "\x00" in cleaned:
            raise SandboxSecurityError(
                "Null byte detected in path.", attempted_path=raw_path
            )

        if cleaned in ("", ".", "./", ".\\"):
            if allow_root:
                return self.root
            raise SandboxSecurityError(
                "Direct mutation/read targeting SANDBOX_ROOT itself is blocked.",
                attempted_path=raw_path,
            )

        # Block UNC paths (\\server\share or //server/share)
        if cleaned.startswith("\\\\") or cleaned.startswith("//"):
            raise SandboxSecurityError(
                f"UNC/network path blocked: '{raw_path}'.", attempted_path=raw_path
            )

        # Detect absolute path inputs (POSIX leading '/' or Windows drive 'C:\...')
        has_drive = bool(_WINDOWS_DRIVE_RE.match(cleaned))
        is_abs_like = has_drive or cleaned.startswith("/") or cleaned.startswith("\\")

        if is_abs_like:
            # Only allow an absolute path if it is literally inside SANDBOX_ROOT
            candidate_abs = Path(cleaned)
            resolved_abs = candidate_abs.resolve(strict=False)
            if not resolved_abs.is_relative_to(self.root):
                raise SandboxSecurityError(
                    f"Absolute path outside SANDBOX_ROOT blocked: '{raw_path}'.",
                    attempted_path=raw_path,
                )
            rel_from_root = resolved_abs.relative_to(self.root)
            if not rel_from_root.parts:
                if allow_root:
                    return self.root
                raise SandboxSecurityError(
                    "Direct targeting of SANDBOX_ROOT is blocked.",
                    attempted_path=raw_path,
                )
            candidate_unres = self.root.joinpath(*rel_from_root.parts)
        else:
            # Normalize mixed Windows/POSIX separators into path segments
            normalized = cleaned.replace("\\", "/")
            raw_parts = [p for p in normalized.split("/") if p not in ("", ".")]
            candidate_unres = self.root.joinpath(*raw_parts)

        # Resolve and check strict containment
        resolved = candidate_unres.resolve(strict=False)
        if not resolved.is_relative_to(self.root):
            raise SandboxSecurityError(
                f"Path traversal blocked: '{raw_path}' resolves outside SANDBOX_ROOT.",
                attempted_path=raw_path,
            )

        # Check every symlink along the lexical path inside root (prevents symlink escape)
        # Reconstruct lexical path within root only if candidate_unres lexically stayed in root
        try:
            lexical_norm = Path(os.path.normpath(str(candidate_unres)))
            if not lexical_norm.is_relative_to(self.root):
                raise SandboxSecurityError(
                    f"Path traversal blocked: '{raw_path}' escapes SANDBOX_ROOT.",
                    attempted_path=raw_path,
                )
            self._check_symlink_chain(lexical_norm, raw_path)
        except SandboxSecurityError:
            raise
        except Exception as exc:
            raise SandboxSecurityError(
                f"Path validation failed for '{raw_path}': {exc}",
                attempted_path=raw_path,
            ) from exc

        if resolved == self.root:
            if allow_root:
                return self.root
            raise SandboxSecurityError(
                "Direct operation on SANDBOX_ROOT is blocked.",
                attempted_path=raw_path,
            )

        rel_parts = resolved.relative_to(self.root).parts

        if not allow_protected and any(p in PROTECTED_DIR_NAMES for p in rel_parts):
            raise SandboxSecurityError(
                f"Access to protected internal path blocked: '{raw_path}'.",
                attempted_path=raw_path,
            )

        if validate_target_names:
            self._validate_segments(rel_parts, raw_path)

        if must_exist and not resolved.exists():
            rel_str = PurePosixPath(*rel_parts).as_posix()
            raise FileNotFoundError(f"Path does not exist in sandbox: '{rel_str}'.")

        return resolved
