"""Agent tools for user-owned durable project files."""

from __future__ import annotations

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
    virtual_path: Annotated[str, "File path in /mnt/user-data/workspace or /mnt/user-data/outputs."],
    display_name: Annotated[str | None, "Optional durable file name."] = None,
) -> dict:
    """Save a file generated in this conversation into a durable project.

    Use only when the user asks to save, keep, persist, archive, or store a
    result in a project. The source must be in the current thread workspace or
    outputs directory; host paths and other users' files are never accepted.
    """
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
