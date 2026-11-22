"""L1 / L2 / L3 Progressive Disclosure file probes strictly confined to SANDBOX_ROOT."""

from __future__ import annotations

import mimetypes
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.schemas import FileTreeNode, L1ScanResult, L2HeadResult, L3DeepResult
from .guard import PROTECTED_DIR_NAMES, SandboxGuard, SandboxSecurityError


IGNORED_SCAN_DIRS = frozenset(
    {
        *PROTECTED_DIR_NAMES,
        "__pycache__",
        "node_modules",
        ".venv",
        "venv",
        ".idea",
        ".vscode",
    }
)

IGNORED_SCAN_FILES = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})

IMAGE_EXTENSIONS = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".tiff", ".svg", ".heic"}
)
ARCHIVE_EXTENSIONS = frozenset(
    {".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".whl"}
)
TEXT_EXTENSIONS = frozenset(
    {
        ".txt",
        ".md",
        ".markdown",
        ".rst",
        ".csv",
        ".tsv",
        ".json",
        ".jsonl",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
        ".conf",
        ".env",
        ".xml",
        ".html",
        ".htm",
        ".css",
        ".scss",
        ".js",
        ".mjs",
        ".cjs",
        ".ts",
        ".tsx",
        ".jsx",
        ".vue",
        ".py",
        ".java",
        ".go",
        ".rs",
        ".c",
        ".cpp",
        ".h",
        ".hpp",
        ".cs",
        ".rb",
        ".php",
        ".sh",
        ".bash",
        ".zsh",
        ".ps1",
        ".bat",
        ".sql",
        ".log",
        ".tex",
        ".dockerfile",
    }
)


def _format_mtime(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_likely_binary(sample: bytes) -> bool:
    if not sample:
        return False
    if b"\x00" in sample:
        return True
    # Check ratio of non-text control bytes
    text_chars = bytearray({7, 8, 9, 10, 12, 13, 27} | set(range(0x20, 0x100)))
    non_text = sample.translate(None, text_chars)
    return (len(non_text) / len(sample)) > 0.30


def _decode_bytes(raw: bytes) -> tuple[str, str]:
    for enc in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8(replace)"


def scan_directory_tree(
    guard: SandboxGuard,
    subpath: str = ".",
    *,
    max_depth: int = 4,
    max_entries: int = 400,
) -> L1ScanResult:
    """L1 Progressive Disclosure: Lightweight metadata-only directory tree scan.

    Does NOT read file contents. Skips `.agent_history`, `.git`, `node_modules`, etc.
    Never follows symlinks pointing outside SANDBOX_ROOT.
    """
    max_depth = max(1, min(max_depth, 8))
    max_entries = max(10, min(max_entries, 2000))

    target_dir = guard.resolve_safe_path(subpath, allow_root=True, must_exist=True)
    if not target_dir.is_dir():
        raise NotADirectoryError(
            f"L1 scan expects a directory, got file: '{guard.to_relative_posix(target_dir)}'"
        )

    total_files = 0
    total_dirs = 0
    total_size = 0
    seen_entries = 0
    truncated = False
    skipped_items: list[str] = []
    flat_entries: list[dict[str, Any]] = []

    def _walk(current: Path, depth: int) -> FileTreeNode:
        nonlocal total_files, total_dirs, total_size, seen_entries, truncated

        rel_posix = guard.to_relative_posix(current)
        node_name = current.name if rel_posix != "." else guard.root.name
        try:
            st = current.stat()
            mtime = _format_mtime(st.st_mtime)
        except OSError:
            mtime = None

        node = FileTreeNode(
            name=node_name,
            path=rel_posix,
            type="dir",
            size=0,
            modified_at=mtime,
            children=[],
        )

        if depth > max_depth:
            return node

        try:
            raw_entries = list(current.iterdir())
        except PermissionError:
            skipped_items.append(f"{rel_posix} (permission denied)")
            return node

        # Sort dirs first, then files alphabetically
        dirs: list[Path] = []
        files: list[Path] = []
        for entry in raw_entries:
            name = entry.name
            if name in IGNORED_SCAN_DIRS or name in IGNORED_SCAN_FILES:
                continue
            if entry.is_symlink():
                resolved_link = entry.resolve(strict=False)
                if not resolved_link.is_relative_to(guard.root):
                    skipped_items.append(f"{name} (external symlink blocked)")
                    continue
            try:
                if entry.is_dir():
                    dirs.append(entry)
                elif entry.is_file():
                    files.append(entry)
            except OSError:
                skipped_items.append(f"{name} (unreadable)")

        dirs.sort(key=lambda p: p.name.lower())
        files.sort(key=lambda p: p.name.lower())

        for d in dirs:
            if seen_entries >= max_entries:
                truncated = True
                break
            seen_entries += 1
            total_dirs += 1
            d_rel = guard.to_relative_posix(d)
            flat_entries.append({"path": d_rel, "type": "dir"})
            child_node = _walk(d, depth + 1)
            node.children.append(child_node)

        for f in files:
            if seen_entries >= max_entries:
                truncated = True
                break
            seen_entries += 1
            total_files += 1
            try:
                fst = f.stat()
                fsize = fst.st_size
                fmtime = _format_mtime(fst.st_mtime)
            except OSError:
                fsize = 0
                fmtime = None

            total_size += fsize
            f_rel = guard.to_relative_posix(f)
            ext = f.suffix.lower() or None
            node.children.append(
                FileTreeNode(
                    name=f.name,
                    path=f_rel,
                    type="file",
                    size=fsize,
                    extension=ext,
                    modified_at=fmtime,
                    children=[],
                )
            )
            flat_entries.append(
                {
                    "path": f_rel,
                    "type": "file",
                    "size": fsize,
                    "ext": ext or "",
                    "mtime": fmtime,
                }
            )

        return node

    tree = _walk(target_dir, depth=1)

    return L1ScanResult(
        root_path=guard.to_relative_posix(target_dir),
        max_depth=max_depth,
        total_files=total_files,
        total_dirs=total_dirs,
        total_size_bytes=total_size,
        truncated=truncated,
        skipped_items=skipped_items,
        tree=tree,
        flat_entries=flat_entries,
    )


def _read_pdf_head(path: Path, max_chars: int) -> tuple[int, dict[str, str], str, bool]:
    try:
        from pypdf import PdfReader
    except ImportError:
        return 0, {}, "[pypdf is not installed; PDF text preview unavailable]", False

    try:
        reader = PdfReader(str(path))
        page_count = len(reader.pages)
        meta: dict[str, str] = {}
        if reader.metadata:
            for key in ("/Title", "/Author", "/Subject", "/Creator"):
                val = reader.metadata.get(key)
                if val:
                    meta[key.lstrip("/").lower()] = str(val)[:200]

        if page_count == 0:
            return 0, meta, "[Empty PDF document]", False

        first_page_text = (reader.pages[0].extract_text() or "").strip()
        if not first_page_text:
            first_page_text = "[PDF page 1 contains no extractable text (possibly scanned image)]"
            return page_count, meta, first_page_text, False

        truncated = len(first_page_text) > max_chars or page_count > 1
        snippet = first_page_text[:max_chars]
        return page_count, meta, snippet, truncated
    except Exception as exc:
        return 0, {}, f"[Unable to parse PDF preview: {exc}]", False


def _extract_docx_blocks(path: Path) -> tuple[dict[str, str], list[str]]:
    """Extract non-empty paragraphs and table rows from a .docx document via python-docx."""
    try:
        from docx import Document

        doc = Document(str(path))
        meta: dict[str, str] = {}
        props = doc.core_properties
        for field in ("title", "author", "subject", "category"):
            val = getattr(props, field, None)
            if val and str(val).strip():
                meta[field] = str(val).strip()[:200]

        blocks: list[str] = []
        # Extract non-empty paragraphs
        for p in doc.paragraphs:
            text = " ".join(p.text.split()).strip()
            if text:
                blocks.append(text)

        # Also extract non-empty rows from tables (crucial for lab report cover pages!)
        table_count = len(doc.tables)
        if table_count > 0:
            meta["tables"] = str(table_count)
            for table in doc.tables[:8]:
                for row in table.rows[:30]:
                    cells: list[str] = []
                    for cell in row.cells:
                        cell_txt = " ".join(cell.text.split()).strip()
                        if cell_txt and (not cells or cells[-1] != cell_txt):
                            cells.append(cell_txt)
                    if cells:
                        row_line = " | ".join(cells)
                        if row_line not in blocks[-5:]:
                            blocks.append(row_line)

        meta["paragraphs"] = str(len([p for p in doc.paragraphs if p.text.strip()]))
        return meta, blocks
    except ImportError:
        # Lightweight stdlib fallback via word/document.xml if python-docx isn't installed
        import xml.etree.ElementTree as ET
        import zipfile

        try:
            with zipfile.ZipFile(path) as zf:
                xml_bytes = zf.read("word/document.xml")
            root = ET.fromstring(xml_bytes)
            ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
            blocks = []
            for p_elem in root.findall(".//w:p", ns):
                texts = [t.text for t in p_elem.findall(".//w:t", ns) if t.text]
                line = " ".join("".join(texts).split()).strip()
                if line:
                    blocks.append(line)
            return {"parser": "xml_fallback"}, blocks
        except Exception as exc:
            return {}, [f"[Unable to parse DOCX document: {exc}]"]
    except Exception as exc:
        return {}, [f"[Unable to parse DOCX document: {exc}]"]


def _read_docx_head(path: Path, max_lines: int, max_chars: int) -> tuple[dict[str, str], int, str, bool]:
    meta, blocks = _extract_docx_blocks(path)
    total_blocks = len(blocks)
    if total_blocks == 0:
        return meta, 0, "[Empty DOCX document (no extractable text)]", False

    selected = blocks[:max_lines]
    preview = "\n".join(selected)
    truncated = total_blocks > max_lines or len(preview) > max_chars
    if len(preview) > max_chars:
        preview = preview[:max_chars]
    return meta, total_blocks, preview, truncated


def inspect_file_head(
    guard: SandboxGuard,
    file_path: str,
    *,
    max_lines: int = 40,
    max_chars: int = 1800,
) -> L2HeadResult:
    """L2 Progressive Disclosure: Lightweight head/outline/PDF-page-1/DOCX-outline inspection."""
    max_lines = max(5, min(max_lines, 120))
    max_chars = max(200, min(max_chars, 6000))

    resolved = guard.resolve_safe_path(file_path, allow_root=False, must_exist=True)
    if resolved.is_dir():
        raise IsADirectoryError(
            f"Cannot L2-inspect a directory: '{guard.to_relative_posix(resolved)}'. Use scan_directory_tree instead."
        )

    rel_posix = guard.to_relative_posix(resolved)
    st = resolved.stat()
    size_bytes = st.st_size
    modified_at = _format_mtime(st.st_mtime)
    ext = resolved.suffix.lower()
    mime_type = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"

    # PDF handling (optional pypdf)
    if ext == ".pdf":
        page_count, pdf_meta, preview, truncated = _read_pdf_head(resolved, max_chars)
        return L2HeadResult(
            path=rel_posix,
            file_type="pdf",
            mime_type="application/pdf",
            size_bytes=size_bytes,
            modified_at=modified_at,
            page_count=page_count,
            pdf_metadata=pdf_meta or None,
            preview=preview,
            truncated=truncated,
        )

    # Word .docx handling (python-docx)
    if ext == ".docx":
        docx_meta, total_blocks, preview, truncated = _read_docx_head(
            resolved, max_lines=max_lines, max_chars=max_chars
        )
        return L2HeadResult(
            path=rel_posix,
            file_type="docx",
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            size_bytes=size_bytes,
            modified_at=modified_at,
            line_count_estimate=total_blocks,
            docx_metadata=docx_meta or None,
            preview=preview,
            truncated=truncated,
        )

    # Image metadata only (never dump binary bytes)
    if ext in IMAGE_EXTENSIONS and ext != ".svg":
        return L2HeadResult(
            path=rel_posix,
            file_type="image",
            mime_type=mime_type,
            size_bytes=size_bytes,
            modified_at=modified_at,
            preview=f"[Image file: {resolved.name}, {size_bytes:,} bytes, MIME={mime_type}]",
            truncated=False,
        )

    # Archive metadata only
    if ext in ARCHIVE_EXTENSIONS:
        return L2HeadResult(
            path=rel_posix,
            file_type="archive",
            mime_type=mime_type,
            size_bytes=size_bytes,
            modified_at=modified_at,
            preview=f"[Archive file: {resolved.name}, {size_bytes:,} bytes, MIME={mime_type}]",
            truncated=False,
        )

    # Sample bytes to detect binary vs text
    read_budget = min(max(max_chars * 4, 8192), 65536)
    with resolved.open("rb") as fh:
        raw_chunk = fh.read(read_budget)

    if ext not in TEXT_EXTENSIONS and _is_likely_binary(raw_chunk[:4096]):
        return L2HeadResult(
            path=rel_posix,
            file_type="binary",
            mime_type=mime_type,
            size_bytes=size_bytes,
            modified_at=modified_at,
            preview=f"[Binary file: {resolved.name}, {size_bytes:,} bytes, MIME={mime_type}]",
            truncated=False,
        )

    text, encoding = _decode_bytes(raw_chunk)
    lines = text.splitlines()
    selected_lines = lines[:max_lines]
    preview = "\n".join(selected_lines)
    truncated = (
        len(lines) > max_lines
        or len(preview) > max_chars
        or size_bytes > len(raw_chunk)
    )
    if len(preview) > max_chars:
        preview = preview[:max_chars]

    # Estimate line count cleanly without reading huge files
    if size_bytes <= 2 * 1024 * 1024:
        try:
            with resolved.open("rb") as fh:
                line_estimate = fh.read().count(b"\n") + (1 if size_bytes > 0 else 0)
        except OSError:
            line_estimate = len(lines)
    else:
        avg_line_bytes = max(1, len(raw_chunk) // max(1, len(lines)))
        line_estimate = max(len(lines), size_bytes // avg_line_bytes)

    return L2HeadResult(
        path=rel_posix,
        file_type="text",
        mime_type=mime_type if mime_type != "application/octet-stream" else "text/plain",
        size_bytes=size_bytes,
        modified_at=modified_at,
        encoding=encoding,
        line_count_estimate=line_estimate,
        preview=preview,
        truncated=truncated,
    )


def read_file_deep(
    guard: SandboxGuard,
    file_path: str,
    *,
    start_line: int = 1,
    max_lines: int = 200,
    max_chars: int = 8000,
) -> L3DeepResult:
    """L3 Progressive Disclosure: Bounded deep read when L1+L2 is insufficient."""
    start_line = max(1, start_line)
    max_lines = max(10, min(max_lines, 500))
    max_chars = max(500, min(max_chars, 24000))

    resolved = guard.resolve_safe_path(file_path, allow_root=False, must_exist=True)
    if resolved.is_dir():
        raise IsADirectoryError(
            f"Cannot L3-read a directory: '{guard.to_relative_posix(resolved)}'."
        )

    rel_posix = guard.to_relative_posix(resolved)
    ext = resolved.suffix.lower()

    if ext == ".pdf":
        try:
            from pypdf import PdfReader

            reader = PdfReader(str(resolved))
            pages_text: list[str] = []
            for idx, page in enumerate(reader.pages[:10], start=1):
                pages_text.append(f"--- Page {idx} ---")
                pages_text.append((page.extract_text() or "").strip())
            full_text = "\n".join(pages_text)
        except Exception as exc:
            return L3DeepResult(
                path=rel_posix,
                start_line=1,
                end_line=1,
                total_lines=1,
                content=f"[Unable to deep-read PDF: {exc}]",
                truncated=False,
            )
    elif ext == ".docx":
        _, blocks = _extract_docx_blocks(resolved)
        full_text = "\n".join(blocks)
    else:
        with resolved.open("rb") as fh:
            # Cap raw read at 1MB to prevent memory/context blowups
            raw = fh.read(1024 * 1024)
        if ext not in TEXT_EXTENSIONS and _is_likely_binary(raw[:4096]):
            raise ValueError(
                f"File '{rel_posix}' is binary; L3 deep text read is not supported."
            )
        full_text, _ = _decode_bytes(raw)

    all_lines = full_text.splitlines()
    total_lines = len(all_lines)
    if total_lines == 0:
        return L3DeepResult(
            path=rel_posix,
            start_line=1,
            end_line=0,
            total_lines=0,
            content="",
            truncated=False,
        )

    slice_start = min(start_line - 1, total_lines)
    slice_end = min(slice_start + max_lines, total_lines)
    selected = all_lines[slice_start:slice_end]

    content = "\n".join(selected)
    truncated = slice_end < total_lines or len(content) > max_chars
    if len(content) > max_chars:
        content = content[:max_chars]

    return L3DeepResult(
        path=rel_posix,
        start_line=slice_start + 1,
        end_line=slice_end,
        total_lines=total_lines,
        content=content,
        truncated=truncated,
    )


def seed_demo_workspace(guard: SandboxGuard) -> tuple[list[str], list[str]]:
    """Populate realistic messy sample files inside SANDBOX_ROOT for testing L1-L3 disclosure.

    Never overwrites existing files. Uses SandboxGuard for every path.
    """
    demo_files: dict[str, str] = {
        "IMG_2026_001.txt": (
            "Project Phoenix - Q4 Architecture Meeting Notes\n"
            "Date: 2026-10-12\n"
            "Attendees: Alice, Bob, Chen\n"
            "Summary: Decided to adopt LangGraph interrupt checkpoints for human-in-the-loop safety.\n"
            "Action Items:\n"
            "- Finalize FastAPI SSE event schema\n"
            "- Benchmark L1/L2/L3 progressive disclosure token savings\n"
        ),
        "doc_final_v2.md": (
            "# SafeWorkspace Agent API Design Spec\n\n"
            "## Endpoints\n"
            "- POST /api/agent/run/stream (SSE)\n"
            "- POST /api/agent/confirm (HITL resume)\n"
            "- POST /api/history/{batch_id}/undo (Transactional rollback)\n"
        ),
        "scan_8821.txt": (
            "TAX INVOICE #INV-2026-08821\n"
            "Vendor: CloudScale Infrastructure Ltd.\n"
            "Billing Period: October 2026\n"
            "Amount Due: $1,240.00 USD\n"
            "Payment Status: PAID (Wire Transfer)\n"
            "Cost Center: Engineering Cloud Ops\n"
        ),
        "untitled_notes.txt": (
            "Quick scratchpad before lunch:\n"
            "1. Need to renew SSL cert for staging.\n"
            "2. Check Q3 expense reimbursement receipt scan_8821.\n"
            "3. Remember to archive old 2024 onboarding slides.\n"
        ),
        "old_2024_onboarding_checklist.md": (
            "# 2024 Legacy Onboarding Checklist (Deprecated)\n"
            "Status: ARCHIVED - Superseded by 2026 handbook.\n"
            "- Old VPN setup (retired)\n"
            "- Legacy Jenkins CI credentials (decommissioned)\n"
        ),
        "budget_q4_2026.csv": (
            "department,category,planned_usd,actual_usd\n"
            "Engineering,Cloud Compute,12000,11450\n"
            "Engineering,AI Inference API,4500,3920\n"
            "Design,Figma Enterprise,1800,1800\n"
            "Operations,Travel & Events,3000,2100\n"
        ),
        "script_cleanup_tmp.py": (
            '"""Helper script to normalize CSV columns for Q4 analytics pipeline."""\n\n'
            "import csv\n"
            "from pathlib import Path\n\n"
            "def normalize_headers(csv_path: Path) -> list[str]:\n"
            "    with csv_path.open('r', encoding='utf-8') as f:\n"
            "        return [h.strip().lower() for h in next(csv.reader(f))]\n"
        ),
        "data_export_raw.json": (
            '{\n'
            '  "project": "Phoenix",\n'
            '  "environment": "staging",\n'
            '  "metrics": {"p95_latency_ms": 142, "sandbox_blocks": 3}\n'
            '}\n'
        ),
        "misc/draft_readme_copy.md": (
            "# Draft Release Notes v0.1\n"
            "Highlights: Strict local sandbox + transactional undo log.\n"
        ),
    }

    created: list[str] = []
    skipped: list[str] = []

    for rel_path, content in demo_files.items():
        try:
            target = guard.resolve_safe_path(
                rel_path, allow_root=False, validate_target_names=True
            )
        except SandboxSecurityError:
            skipped.append(rel_path)
            continue

        if target.exists():
            skipped.append(rel_path)
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        created.append(rel_path)

    return created, skipped
