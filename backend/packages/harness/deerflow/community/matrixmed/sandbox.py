"""File and command adapter for the MatrixMed shared executor."""

from __future__ import annotations

import base64
import posixpath
import re
import threading
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from deerflow.config.paths import VIRTUAL_PATH_PREFIX
from deerflow.sandbox.sandbox import Sandbox, _validate_extra_env
from deerflow.sandbox.search import GrepMatch, should_ignore_path, truncate_line

if TYPE_CHECKING:
    from .provider import MatrixMedSandboxProvider

_MAX_DOWNLOAD_SIZE = 100 * 1024 * 1024


class MatrixMedSandbox(Sandbox):
    """A logical DeerFlow sandbox backed by stateless remote executions."""

    persistent_shell_sessions = False

    def __init__(
        self,
        id: str,
        *,
        provider: MatrixMedSandboxProvider,
        subject: str,
        project_key: str,
        source_thread_id: str,
        context_token: str,
        context_expires_at: str,
        context_id: str,
    ) -> None:
        super().__init__(id)
        self._provider = provider
        self.subject = subject
        self.project_key = project_key
        self.source_thread_id = source_thread_id
        self._token = context_token
        self._expires_at = self._parse_expiry(context_expires_at)
        self._context_id = context_id
        self._lock = threading.RLock()

    @staticmethod
    def _parse_expiry(value: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is not None:
                return parsed.astimezone(UTC)
        except ValueError:
            pass
        return datetime.now(UTC)

    def context_token(self) -> str:
        with self._lock:
            if self._expires_at <= datetime.now(UTC) + timedelta(seconds=30):
                self._token, expires_at, self._context_id = self._provider.refresh_context(self)
                self._expires_at = self._parse_expiry(expires_at)
            return self._token

    def context_id(self) -> str:
        self.context_token()
        with self._lock:
            return self._context_id

    @staticmethod
    def _resolve_path(path: str, *, write: bool = False) -> str:
        if not isinstance(path, str) or not path:
            raise ValueError("path must be a non-empty string")
        raw = path.replace("\\", "/")
        if any(part == ".." for part in raw.split("/")):
            raise PermissionError(f"Access denied: invalid sandbox path {path!r}")
        normalized = posixpath.normpath(raw)
        if not normalized.startswith("/"):
            raise PermissionError(f"Access denied: invalid sandbox path {path!r}")
        virtual = VIRTUAL_PATH_PREFIX.rstrip("/")
        mappings = {
            f"{virtual}/workspace": "/workspace",
            f"{virtual}/uploads": "/workspace/uploads",
            f"{virtual}/outputs": "/outputs",
            # File APIs use Sandbox API logical paths; `/inputs/...` exists
            # only inside an NsJail execution mount.
            f"{virtual}/query-results": "/query-results",
        }
        for source, target in mappings.items():
            if normalized == source or normalized.startswith(f"{source}/"):
                resolved = target + normalized[len(source) :]
                if write and target == "/inputs/query-results":
                    raise PermissionError("query-results is read-only")
                return resolved
        raise PermissionError(f"Access denied: path must be under {virtual}/workspace, {virtual}/uploads, {virtual}/outputs, or {virtual}/query-results")

    @staticmethod
    def _virtualize(path: str) -> str:
        mappings = {
            "/workspace": f"{VIRTUAL_PATH_PREFIX.rstrip('/')}/workspace",
            "/outputs": f"{VIRTUAL_PATH_PREFIX.rstrip('/')}/outputs",
            "/query-results": f"{VIRTUAL_PATH_PREFIX.rstrip('/')}/query-results",
        }
        for source, target in mappings.items():
            if path == source or path.startswith(f"{source}/"):
                return target + path[len(source) :]
        return path

    @staticmethod
    def _resolve_project_file_source(path: str) -> str:
        """Resolve a source accepted by the immutable project-file API only.

        Agent execution and ordinary logical file operations intentionally do
        not expose Jupyter files.  A user can nevertheless explicitly save one
        file from their own Jupyter space to the currently authorized project;
        the Sandbox API derives that user's physical directory and performs the
        symlink-safe read.
        """
        try:
            return MatrixMedSandbox._resolve_path(path)
        except PermissionError as original:
            if not isinstance(path, str) or not path:
                raise original
            raw = path.replace("\\", "/")
            if any(part == ".." for part in raw.split("/")):
                raise original
            normalized = posixpath.normpath(raw)
            virtual = VIRTUAL_PATH_PREFIX.rstrip("/")
            source = f"{virtual}/jupyter"
            if normalized == source or normalized.startswith(f"{source}/"):
                return "/jupyter" + normalized[len(source) :]
            raise original

    @staticmethod
    def _translate_command(command: str) -> str:
        """Map DeerFlow's documented virtual paths into the jail namespace."""
        virtual = VIRTUAL_PATH_PREFIX.rstrip("/")
        mappings = (
            (f"{virtual}/query-results", "/inputs/query-results"),
            (f"{virtual}/uploads", "/workspace/uploads"),
            (f"{virtual}/workspace", "/workspace"),
            (f"{virtual}/outputs", "/output"),
        )
        translated = command
        for source, target in mappings:
            translated = re.sub(
                re.escape(source) + r"(?=$|[^A-Za-z0-9._-])",
                target,
                translated,
            )
        return translated

    def _execute(self, language: str, code: str) -> tuple[str, str, int]:
        response = self._provider.execute(self, language=language, code=code)
        stdout, stderr = str(response.get("stdout") or ""), str(response.get("stderr") or "")
        return stdout, stderr, int(response.get("exit_code", 1))

    def execute_command(self, command: str, env: dict[str, str] | None = None, timeout: float | None = None) -> str:
        _validate_extra_env(env)
        if env:
            return "Error: MatrixMed Sandbox does not accept per-command environment variables"
        if timeout is not None and timeout <= 0:
            raise ValueError("timeout must be positive")
        stdout, stderr, exit_code = self._execute("bash", self._translate_command(command))
        output = "\n".join(part for part in (stdout.rstrip("\n"), stderr.rstrip("\n")) if part)
        if exit_code != 0:
            return f"{output}\nExit Code: {exit_code}" if output else f"Command exited with code {exit_code}"
        return output or "(no output)"

    def read_file(self, path: str, start_line: int | None = None, end_line: int | None = None) -> str:
        resolved = self._resolve_path(path)
        response = self._provider.file_json(self, method="GET", endpoint="files:read", path=resolved)
        content = str(response.get("content") or "")
        if start_line is None and end_line is None:
            return content
        return "\n".join(content.splitlines()[(start_line or 1) - 1 : end_line])

    def write_file(self, path: str, content: str, append: bool = False) -> None:
        resolved = self._resolve_path(path, write=True)
        self._provider.file_json(self, method="PUT", endpoint="files", path=resolved, body={"content": content, "append": append})

    def update_file(self, path: str, content: bytes) -> None:
        resolved = self._resolve_path(path, write=True)
        self._provider.file_json(self, method="PUT", endpoint="files:binary", path=resolved, body={"content_base64": base64.b64encode(content).decode()})

    def download_file(self, path: str) -> bytes:
        resolved = self._resolve_path(path)
        return self._provider.download_file(self, resolved)

    def list_project_files(self) -> list[dict[str, object]]:
        """List latest immutable files for this context's research project."""
        response = self._provider.project_file_json(self, method="GET")
        files = response.get("files") or []
        return [item for item in files if isinstance(item, dict)]

    def save_project_file(
        self,
        source_path: str,
        *,
        display_name: str | None = None,
        kind: str = "other",
    ) -> dict[str, object]:
        """Create an immutable project-file version from an authorized file."""
        source = self._resolve_project_file_source(source_path)
        return self._provider.project_file_json(
            self,
            method="POST",
            body={"source_path": source, "display_name": display_name, "kind": kind},
        )

    def attach_project_file(
        self,
        file_id: str,
        *,
        version: int | None = None,
        target_name: str | None = None,
    ) -> tuple[dict[str, object], str]:
        """Copy one fixed project-file version into this thread workspace."""
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", file_id):
            raise ValueError("project file ID must be a UUID")
        response = self._provider.project_file_json(
            self,
            method="POST",
            suffix=f"/{file_id}:attach",
            body={"version": version, "target_name": target_name},
        )
        thread_path = response.get("thread_path")
        if not isinstance(thread_path, str):
            raise RuntimeError("MatrixMed Sandbox API returned no attached thread path")
        file = response.get("file")
        if not isinstance(file, dict):
            raise RuntimeError("MatrixMed Sandbox API returned no attached project file")
        return file, self._virtualize(thread_path)

    def list_dir(self, path: str, max_depth: int = 2) -> list[str]:
        if max_depth < 0:
            raise ValueError("max_depth must be non-negative")
        resolved = self._resolve_path(path)
        response = self._provider.file_json(self, method="GET", endpoint="files:list", path=resolved, extra_query={"max_depth": max_depth})
        entries = response.get("entries") or []
        return [self._virtualize(str(entry["path"])) for entry in entries if isinstance(entry, dict) and isinstance(entry.get("path"), str)]

    def glob(self, path: str, pattern: str, *, include_dirs: bool = False, max_results: int = 200) -> tuple[list[str], bool]:
        if max_results <= 0:
            raise ValueError("max_results must be positive")
        resolved = self._resolve_path(path)
        response = self._provider.file_json(
            self, method="GET", endpoint="files:glob", path=resolved,
            extra_query={"pattern": pattern, "include_dirs": include_dirs, "max_results": max_results},
        )
        values = response.get("paths") or []
        return [self._virtualize(str(value)) for value in values if isinstance(value, str)], bool(response.get("truncated"))

    def grep(self, path: str, pattern: str, *, glob: str | None = None, literal: bool = False, case_sensitive: bool = False, max_results: int = 100) -> tuple[list[GrepMatch], bool]:
        if max_results <= 0:
            raise ValueError("max_results must be positive")
        resolved = self._resolve_path(path)
        response = self._provider.file_json(
            self, method="GET", endpoint="files:grep", path=resolved,
            extra_query={
                "pattern": pattern, "glob": glob, "literal": literal,
                "case_sensitive": case_sensitive, "max_results": max_results,
            },
        )
        values = response.get("matches") or []
        matches = [
            GrepMatch(
                path=self._virtualize(str(item["path"])),
                line_number=int(item["line_number"]),
                line=truncate_line(str(item["line"])),
            )
            for item in values
            if isinstance(item, dict)
            and isinstance(item.get("path"), str)
            and isinstance(item.get("line_number"), int)
            and isinstance(item.get("line"), str)
            and not should_ignore_path(str(item["path"]))
        ]
        return matches, bool(response.get("truncated"))


__all__ = ["MatrixMedSandbox"]
