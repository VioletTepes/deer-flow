"""Agent tools for user-owned durable project files."""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import uuid
from pathlib import Path
from typing import Annotated, Any

from langchain.tools import tool
from sqlalchemy.exc import IntegrityError

from deerflow.config.paths import get_paths
from deerflow.persistence import get_session_factory
from deerflow.persistence.projects import ProjectRepository
from deerflow.runtime.user_context import resolve_runtime_user_id
from deerflow.sandbox.sandbox_provider import get_sandbox_provider
from deerflow.tools.types import Runtime
from deerflow.utils.file_io import run_file_io

_ALLOWED_THREAD_ROOTS = ("workspace", "outputs")


def _thread_id(runtime: Runtime) -> str | None:
    context = runtime.context or {}
    value = context.get("thread_id")
    if value:
        return str(value)
    config = runtime.config or {}
    configurable = config.get("configurable", {})
    return str(configurable.get("thread_id")) if configurable.get("thread_id") else None


def _safe_name(name: str) -> str:
    value = Path(name).name.strip()
    if not value or value in {".", ".."} or len(value) > 256:
        raise ValueError("Invalid file name")
    return value


def _repo() -> ProjectRepository | None:
    session_factory = get_session_factory()
    return ProjectRepository(session_factory) if session_factory is not None else None


async def _matrixmed_project_sandbox(runtime: Runtime) -> Any | None:
    """Return the current MatrixMed sandbox only when its V1 file API is usable.

    Other DeerFlow providers retain the existing local ProjectRepository path.
    MatrixMed must never read that local repository: its project membership and
    file scope are already bound into the Sandbox context token.
    """
    try:
        provider = get_sandbox_provider()
    except (FileNotFoundError, ValueError):
        # Legacy project-file callers can run before a sandbox configuration is
        # loaded (for example, during local maintenance).  In that case retain
        # their existing repository-backed behavior rather than making an
        # optional MatrixMed integration a global configuration prerequisite.
        return None
    if not hasattr(provider, "project_file_json"):
        return None
    from deerflow.sandbox.tools import ensure_sandbox_initialized_async

    sandbox = await ensure_sandbox_initialized_async(runtime)
    if not all(hasattr(sandbox, method) for method in ("list_project_files", "save_project_file", "attach_project_file", "project_key")):
        return None
    return sandbox


def _matrixmed_project_matches(sandbox: Any, project_ref: str) -> bool:
    """A MatrixMed context authorizes exactly one project, never a caller name."""
    return isinstance(project_ref, str) and project_ref == sandbox.project_key


def _matrixmed_kind(name: str) -> str:
    suffix = Path(name).suffix.casefold()
    if suffix == ".ipynb":
        return "notebook"
    if suffix in {".py", ".r", ".jl", ".sql"}:
        return "source"
    if suffix in {".sh", ".bash"}:
        return "script"
    if suffix in {".png", ".jpg", ".jpeg", ".svg"}:
        return "chart"
    if suffix in {".pdf", ".docx", ".md", ".html"}:
        return "report"
    return "other"


def _matrixmed_file_view(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "file_id": record.get("file_id"),
        "name": record.get("display_name"),
        "version": record.get("version"),
        "size": record.get("size_bytes"),
        "kind": record.get("kind"),
        "status": "active",
        "content_available": True,
        "path": None,
        "access": "Call attach_project_file to materialize this immutable version in the current sandbox.",
    }


def _name_conflict_result(project: dict[str, Any], name: str) -> dict[str, Any]:
    return {
        "success": False,
        "code": "project_file_name_conflict",
        "project_id": project["project_id"],
        "project_name": project["name"],
        "name": name,
        "message": (f"A file named '{name}' already exists in project '{project['name']}'. The existing file was kept unchanged. Choose a different display_name to save this file as a new project file."),
    }


async def _resolve_project(project_ref: str, user_id: str) -> dict[str, Any] | None:
    repo = _repo()
    if repo is None:
        project_id = project_ref
        return {"project_id": project_id, "name": project_id} if get_paths().project_dir(user_id, project_id).is_dir() else None
    direct = await repo.get_project(project_ref, user_id=user_id)
    if direct is not None:
        return direct
    wanted = project_ref.casefold().strip()
    return next((item for item in await repo.list_projects(user_id=user_id) if item["name"].casefold() == wanted), None)


async def _list_projects(user_id: str, project_name: str | None) -> list[dict[str, Any]]:
    repo = _repo()
    if repo is not None:
        projects = await repo.list_projects(user_id=user_id)
    else:
        root = get_paths().user_projects_dir(user_id)
        projects = [{"project_id": entry.name, "name": entry.name} for entry in root.iterdir() if entry.is_dir()] if root.is_dir() else []
    wanted = project_name.casefold().strip() if project_name else None
    return [item for item in projects if wanted is None or item["name"].casefold() == wanted or item["project_id"] == project_name]


@tool
async def list_project_files(
    runtime: Runtime,
    project_name: Annotated[str | None, "Optional project name or project ID. Omit to list every durable project."] = None,
) -> dict:
    """List the current user's durable project files across all conversations.

    Use this for requests such as "my project files", "files I saved before",
    "persistent files", "跨会话保存的文件", "项目资料", or "之前生成的文件".
    Do not use this for the current conversation workspace; use ``ls`` or
    ``glob`` for ``/mnt/user-data/workspace``. The returned metadata is not a
    sandbox path. Call ``attach_project_file`` before reading a durable file
    with shell or document tools.
    """
    matrixmed = await _matrixmed_project_sandbox(runtime)
    if matrixmed is not None:
        if project_name is not None and not _matrixmed_project_matches(matrixmed, project_name):
            return {"projects": [], "files": [], "message": "The requested project is outside the current authorized Sandbox context."}
        files = [_matrixmed_file_view(item) for item in await asyncio.to_thread(matrixmed.list_project_files)]
        project = {"project_id": matrixmed.project_key, "project_name": matrixmed.project_key, "files": files}
        return {
            "projects": [project],
            "files": files,
            "message": "Immutable project files listed for the current authorized research project.",
        }

    user_id = resolve_runtime_user_id(runtime)
    repo = _repo()
    result_projects: list[dict[str, Any]] = []
    for project in await _list_projects(user_id, project_name):
        project_id = project["project_id"]
        records = await repo.list_files(project_id, user_id=user_id) if repo is not None else []
        files: list[dict[str, Any]] = []
        if records:
            for record in records:
                path = get_paths().project_file_path(user_id, project_id, record["relative_path"])
                files.append(
                    {
                        "file_id": record["file_id"],
                        "name": record["display_name"],
                        "size": record["size_bytes"],
                        "status": record["status"],
                        "content_available": await run_file_io(path.is_file),
                        "path": None,
                        "access": "Call attach_project_file to materialize this file in the current sandbox.",
                    }
                )
        else:
            files_dir = get_paths().project_files_dir(user_id, project_id)
            if files_dir.is_dir():
                for path in sorted((item for item in files_dir.rglob("*") if item.is_file()), key=lambda item: item.name):
                    files.append({"file_id": None, "name": path.name, "size": path.stat().st_size, "status": "active", "content_available": True, "path": None, "access": "Use attach_project_file with this project ID and file name."})
        result_projects.append({"project_id": project_id, "project_name": project["name"], "files": files})
    return {"projects": result_projects, "files": [file for project in result_projects for file in project["files"]], "message": "Durable project files listed. These files are outside the current sandbox until attached."}


def _resolve_thread_source(runtime: Runtime, virtual_path: str) -> Path:
    stripped = virtual_path.lstrip("/")
    parts = Path(stripped).parts
    if len(parts) < 4 or parts[:2] != ("mnt", "user-data") or parts[2] not in _ALLOWED_THREAD_ROOTS:
        raise ValueError("Only files in /mnt/user-data/workspace or /mnt/user-data/outputs can be saved")
    thread_id = _thread_id(runtime)
    if thread_id is None:
        raise ValueError("Thread ID is not available")
    return get_paths().resolve_virtual_path(thread_id, virtual_path, user_id=resolve_runtime_user_id(runtime))


@tool
async def attach_project_file(
    runtime: Runtime,
    project_ref: Annotated[str, "Project name or project ID."],
    file_name: Annotated[str, "Exact durable file name to attach."],
    target_name: Annotated[str | None, "Optional name in the current sandbox."] = None,
) -> dict:
    """Attach one durable project file to the current conversation sandbox.

    Use this after ``list_project_files`` when the Agent needs to read or
    analyze a persistent file. It returns the only path that sandbox tools may
    use. It does not delete or move the durable copy.
    """
    matrixmed = await _matrixmed_project_sandbox(runtime)
    if matrixmed is not None:
        if not _matrixmed_project_matches(matrixmed, project_ref):
            return {"success": False, "message": "The requested project is outside the current authorized Sandbox context."}
        name = _safe_name(file_name)
        files = await asyncio.to_thread(matrixmed.list_project_files)
        record = next(
            (item for item in files if item.get("display_name") == name or item.get("file_id") == file_name),
            None,
        )
        if record is None or not isinstance(record.get("file_id"), str):
            return {"success": False, "message": "Project file not found in the current authorized project."}
        attached, virtual_path = await asyncio.to_thread(
            matrixmed.attach_project_file,
            record["file_id"],
            version=record.get("version") if isinstance(record.get("version"), int) else None,
            target_name=_safe_name(target_name or name),
        )
        return {
            "success": True,
            "project_id": matrixmed.project_key,
            "project_name": matrixmed.project_key,
            "file_id": attached.get("file_id"),
            "version": attached.get("version"),
            "file_name": attached.get("display_name", name),
            "virtual_path": virtual_path,
            "message": "Immutable project file attached to the current sandbox.",
        }

    user_id = resolve_runtime_user_id(runtime)
    project = await _resolve_project(project_ref, user_id)
    if project is None:
        return {"success": False, "message": "Project not found for the current user."}
    name = _safe_name(file_name)
    repo = _repo()
    record = None
    if repo is not None:
        record = next((item for item in await repo.list_files(project["project_id"], user_id=user_id) if item["display_name"] == name), None)
    if record is None:
        return {"success": False, "message": "Project file not found."}
    source = get_paths().project_file_path(user_id, project["project_id"], record["relative_path"])
    if not await run_file_io(source.is_file):
        return {"success": False, "message": "Project file content is unavailable."}
    thread_id = _thread_id(runtime)
    if thread_id is None:
        return {"success": False, "message": "Thread ID is not available."}
    attached_name = _safe_name(target_name or name)
    virtual_path = f"/mnt/user-data/workspace/project/{project['project_id']}/{attached_name}"
    provider = get_sandbox_provider()
    data = await run_file_io(source.read_bytes)
    if bool(getattr(provider, "uses_thread_data_mounts", False)):
        target = get_paths().resolve_virtual_path(thread_id, virtual_path, user_id=user_id)
        await run_file_io(target.parent.mkdir, parents=True, exist_ok=True)
        await run_file_io(target.write_bytes, data)
    else:
        from deerflow.sandbox.tools import ensure_sandbox_initialized_async

        sandbox = await ensure_sandbox_initialized_async(runtime)
        await sandbox.update_file(virtual_path, data)
    return {"success": True, "project_id": project["project_id"], "project_name": project["name"], "file_name": name, "virtual_path": virtual_path, "message": "Project file attached to the current sandbox."}


@tool
async def save_project_file(
    runtime: Runtime,
    project_ref: Annotated[str, "Project name or project ID."],
    virtual_path: Annotated[str, "File path in /mnt/user-data/workspace, /mnt/user-data/outputs, or an explicitly selected /mnt/user-data/jupyter file."],
    display_name: Annotated[str | None, "Optional durable file name."] = None,
) -> dict:
    """Save a file generated in this conversation into a durable project.

    Use only when the user asks to save, keep, persist, archive, or store a
    result in a project. The source may be in the current thread workspace or
    outputs directory, or be one explicitly selected file from the current
    user's Jupyter space. Host paths and other users' files are never accepted.
    """
    matrixmed = await _matrixmed_project_sandbox(runtime)
    if matrixmed is not None:
        if not _matrixmed_project_matches(matrixmed, project_ref):
            return {"success": False, "message": "The requested project is outside the current authorized Sandbox context."}
        name = _safe_name(display_name or Path(virtual_path).name)
        try:
            record = await asyncio.to_thread(
                matrixmed.save_project_file,
                virtual_path,
                display_name=name,
                kind=_matrixmed_kind(name),
            )
        except (PermissionError, ValueError) as exc:
            return {"success": False, "message": str(exc)}
        return {
            "success": True,
            "project_id": matrixmed.project_key,
            "project_name": matrixmed.project_key,
            "file_id": record.get("file_id"),
            "version": record.get("version"),
            "name": record.get("display_name", name),
            "size": record.get("size_bytes"),
            "message": "File saved as an immutable version in the current authorized project.",
        }

    user_id = resolve_runtime_user_id(runtime)
    project = await _resolve_project(project_ref, user_id)
    if project is None:
        return {"success": False, "message": "Project not found for the current user."}
    source = _resolve_thread_source(runtime, virtual_path)
    name = _safe_name(display_name or Path(virtual_path).name)
    repo = _repo()
    if repo is None:
        return {"success": False, "message": "Project persistence is unavailable."}
    existing_files = await repo.list_files(project["project_id"], user_id=user_id)
    if any(file["display_name"] == name for file in existing_files):
        return _name_conflict_result(project, name)
    provider = get_sandbox_provider()
    if bool(getattr(provider, "uses_thread_data_mounts", False)):
        if not await run_file_io(source.is_file):
            return {"success": False, "message": "Source file not found in the current sandbox."}
        data = await run_file_io(source.read_bytes)
    else:
        from deerflow.sandbox.tools import ensure_sandbox_initialized_async

        sandbox = await ensure_sandbox_initialized_async(runtime)
        data = await sandbox.download_file(virtual_path)
    relative_path = f"{uuid.uuid4()}/{name}"
    destination = get_paths().project_file_path(user_id, project["project_id"], relative_path)
    await run_file_io(destination.parent.mkdir, parents=True, exist_ok=True)
    await run_file_io(destination.write_bytes, data)
    digest = hashlib.sha256(data).hexdigest()
    try:
        record = await repo.create_file(
            project["project_id"],
            display_name=name,
            relative_path=relative_path,
            size_bytes=len(data),
            sha256=digest,
            media_type=mimetypes.guess_type(name)[0],
            source_type="thread",
            source_thread_id=_thread_id(runtime),
            user_id=user_id,
        )
    except IntegrityError:
        await run_file_io(destination.unlink, missing_ok=True)
        return _name_conflict_result(project, name)
    except Exception:
        await run_file_io(destination.unlink, missing_ok=True)
        raise
    if record is None:
        await run_file_io(destination.unlink, missing_ok=True)
        return {"success": False, "message": "Project is no longer available."}
    return {"success": True, "project_id": project["project_id"], "project_name": project["name"], "file_id": record["file_id"] if record else None, "name": name, "size": len(data), "message": "File saved to the durable project."}
