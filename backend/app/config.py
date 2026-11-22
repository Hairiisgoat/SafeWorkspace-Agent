"""Pydantic Settings configuration & thread-safe persistent BYOK / workspace state."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.sandbox.guard import SandboxGuard, SandboxSecurityError


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = Path(__file__).resolve().parents[1]
LOCAL_CONFIG_PATH = BACKEND_ROOT / ".local_config.json"
MAX_RECENT_WORKSPACES = 5


def _default_sandbox_root() -> str:
    return str((PROJECT_ROOT / "demo_sandbox").resolve())


class Settings(BaseSettings):
    """Application settings loaded from environment variables and optional .env files."""

    model_config = SettingsConfigDict(
        env_file=(
            str(PROJECT_ROOT / ".env"),
            str(BACKEND_ROOT / ".env"),
        ),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    openai_base_url: str = Field(default="https://api.openai.com/v1", alias="OPENAI_BASE_URL")
    model_name: str = Field(default="gpt-4o-mini", alias="MODEL_NAME")
    sandbox_root: str = Field(default_factory=_default_sandbox_root, alias="SANDBOX_ROOT")

    host: str = Field(default="0.0.0.0", alias="HOST")
    port: int = Field(default=8000, alias="PORT")
    cors_origins: str = Field(
        default="http://localhost:5173,http://127.0.0.1:5173",
        alias="CORS_ORIGINS",
    )

    max_agent_steps: int = Field(default=25, alias="MAX_AGENT_STEPS")
    l1_max_depth: int = Field(default=4, alias="L1_MAX_DEPTH")
    l1_max_entries: int = Field(default=400, alias="L1_MAX_ENTRIES")
    l2_max_chars: int = Field(default=1800, alias="L2_MAX_CHARS")
    l3_max_chars: int = Field(default=8000, alias="L3_MAX_CHARS")

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    def resolve_sandbox_path(self, raw_path: Optional[str] = None) -> Path:
        target = raw_path if raw_path is not None else self.sandbox_root
        p = Path(target).expanduser()
        if not p.is_absolute():
            p = (PROJECT_ROOT / p).resolve()
        else:
            p = p.resolve()
        return p


def mask_api_key(key: str) -> str:
    """Return a safely masked representation of an API key for UI display."""
    cleaned = (key or "").strip()
    if not cleaned:
        return ""
    if len(cleaned) <= 8:
        return "*" * len(cleaned)
    return f"{cleaned[:4]}...{cleaned[-4:]}"


def validate_safe_sandbox_root(path: Path) -> None:
    """Refuse dangerous OS roots, system dirs, and user home root as SANDBOX_ROOT."""
    try:
        SandboxGuard.validate_sandbox_root(path, must_exist=False)
    except SandboxSecurityError as exc:
        raise ValueError(str(exc)) from exc


def _path_dedupe_key(path_str: str) -> str:
    cleaned = path_str.rstrip("\\/")
    return cleaned.lower() if os.name == "nt" else cleaned


class RuntimeConfigStore:
    """Thread-safe runtime configuration holder with local JSON persistence (`.local_config.json`).

    Priority on startup:
      1. `backend/.local_config.json` (user-saved BYOK & workspace settings)
      2. `.env` / environment variables
      3. Built-in defaults
    """

    def __init__(
        self,
        base_settings: Settings,
        *,
        local_config_path: Optional[Path] = None,
    ) -> None:
        self._lock = threading.RLock()
        self.base_settings = base_settings
        self._local_config_path: Path = local_config_path or LOCAL_CONFIG_PATH

        self._api_key: str = base_settings.openai_api_key
        self._base_url: str = base_settings.openai_base_url
        self._model_name: str = base_settings.model_name
        self._sandbox_root: Path = base_settings.resolve_sandbox_path()
        self._recent_workspaces: list[str] = []

        # Ensure default demo_sandbox exists on first boot if using default path
        default_demo = (PROJECT_ROOT / "demo_sandbox").resolve()
        if self._sandbox_root == default_demo:
            self._sandbox_root.mkdir(parents=True, exist_ok=True)

        # Load persisted overrides from backend/.local_config.json (higher priority than .env)
        self._load_persisted_config()

        # Ensure current active sandbox root appears in recent list if valid
        if self._sandbox_root.exists() and self._sandbox_root.is_dir():
            self._record_recent_workspace(str(self._sandbox_root))

    def _record_recent_workspace(self, workspace_path: str) -> None:
        target_key = _path_dedupe_key(workspace_path)
        filtered = [p for p in self._recent_workspaces if _path_dedupe_key(p) != target_key]
        self._recent_workspaces = [workspace_path, *filtered][:MAX_RECENT_WORKSPACES]

    def _load_persisted_config(self) -> None:
        if not self._local_config_path.exists():
            return
        try:
            raw = json.loads(self._local_config_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return
        except Exception:
            return

        if isinstance(raw.get("api_key"), str) and raw["api_key"].strip():
            self._api_key = raw["api_key"].strip()

        if isinstance(raw.get("base_url"), str) and raw["base_url"].strip():
            self._base_url = raw["base_url"].strip().rstrip("/")

        if isinstance(raw.get("model_name"), str) and raw["model_name"].strip():
            self._model_name = raw["model_name"].strip()

        saved_root = raw.get("sandbox_root")
        if isinstance(saved_root, str) and saved_root.strip():
            try:
                candidate = self.base_settings.resolve_sandbox_path(saved_root.strip())
                validated = SandboxGuard.validate_sandbox_root(candidate, must_exist=True)
                self._sandbox_root = validated
            except Exception:
                # Gracefully fall back to .env/default sandbox if saved folder was moved/removed
                pass

        saved_recent = raw.get("recent_workspaces")
        if isinstance(saved_recent, list):
            cleaned_recent: list[str] = []
            seen: set[str] = set()
            for item in saved_recent:
                if not isinstance(item, str) or not item.strip():
                    continue
                try:
                    cand = self.base_settings.resolve_sandbox_path(item.strip())
                    validated = SandboxGuard.validate_sandbox_root(cand, must_exist=True)
                except Exception:
                    continue
                v_str = str(validated)
                key = _path_dedupe_key(v_str)
                if key not in seen:
                    seen.add(key)
                    cleaned_recent.append(v_str)
                if len(cleaned_recent) >= MAX_RECENT_WORKSPACES:
                    break
            self._recent_workspaces = cleaned_recent

    def _persist_config(self) -> None:
        payload: dict[str, Any] = {
            "api_key": self._api_key,
            "base_url": self._base_url,
            "model_name": self._model_name,
            "sandbox_root": str(self._sandbox_root),
            "recent_workspaces": list(self._recent_workspaces),
        }
        self._local_config_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._local_config_path.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        if os.name != "nt":
            try:
                os.chmod(tmp_path, 0o600)
            except OSError:
                pass
        os.replace(tmp_path, self._local_config_path)

    @property
    def local_config_path(self) -> Path:
        with self._lock:
            return self._local_config_path

    @property
    def has_persisted_config(self) -> bool:
        with self._lock:
            return self._local_config_path.exists()

    @property
    def sandbox_root(self) -> Path:
        with self._lock:
            return self._sandbox_root

    @property
    def recent_workspaces(self) -> list[str]:
        with self._lock:
            return list(self._recent_workspaces)

    @property
    def api_key(self) -> str:
        with self._lock:
            return self._api_key

    @property
    def base_url(self) -> str:
        with self._lock:
            return self._base_url

    @property
    def model_name(self) -> str:
        with self._lock:
            return self._model_name

    def update(
        self,
        *,
        sandbox_root: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model_name: Optional[str] = None,
        persist: bool = True,
    ) -> None:
        with self._lock:
            if sandbox_root is not None and sandbox_root.strip():
                candidate = self.base_settings.resolve_sandbox_path(sandbox_root.strip())
                try:
                    validated = SandboxGuard.validate_sandbox_root(candidate, must_exist=True)
                except (SandboxSecurityError, FileNotFoundError, NotADirectoryError) as exc:
                    raise ValueError(str(exc)) from exc
                self._sandbox_root = validated
                self._record_recent_workspace(str(validated))

            if api_key is not None:
                self._api_key = api_key.strip()

            if base_url is not None and base_url.strip():
                self._base_url = base_url.strip().rstrip("/")

            if model_name is not None and model_name.strip():
                self._model_name = model_name.strip()

            if persist:
                self._persist_config()

    def get_effective_llm_params(
        self,
        *,
        api_key_override: Optional[str] = None,
        base_url_override: Optional[str] = None,
        model_name_override: Optional[str] = None,
    ) -> tuple[str, str, str]:
        with self._lock:
            key = (api_key_override if api_key_override else self._api_key).strip()
            url = (base_url_override if base_url_override else self._base_url).strip()
            model = (model_name_override if model_name_override else self._model_name).strip()
            return key, url, model


settings = Settings()
runtime_config = RuntimeConfigStore(settings)
