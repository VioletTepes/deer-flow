"""User-isolated persistent project and project-file endpoints."""

from __future__ import annotations

import mimetypes
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from app.gateway.authz import require_permission
from app.gateway.deps import get_config, get_project_repo
from app.gateway.project_files import attach_project_file, read_thread_file, store_project_bytes
from deerflow.config.app_config import AppConfig
from deerflow.config.paths import get_paths
from deerflow.runtime.user_context import get_effective_user_id
from deerflow.utils.file_io import run_file_io

router = APIRouter(prefix="/api/projects", tags=["projects"])


class ProjectCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)


class ProjectResponse(BaseModel):
    project_id: str
    user_id: str
    name: str
    created_at: str
    updated_at: str


class ProjectFileResponse(BaseModel):
    file_id: str
    project_id: str
    display_name: str
    media_type: str | None = None
    size_bytes: int
    sha256: str
    source_type: str
    source_thread_id: str | None = None
    version: int
    status: str
    created_at: str
    updated_at: str


class AttachProjectFileRequest(BaseModel):
    target_name: str | None = Field(default=None, max_length=256)


class SaveThreadFileRequest(BaseModel):
    virtual_path: str
    display_name: str | None = Field(default=None, max_length=256)


def _uid() -> str:
    return get_effective_user_id()


def _safe_name(name: str) -> str:
    value = Path(name).name.strip()
    if not value or value in {".", ".."} or len(value) > 256:
        raise HTTPException(status_code=400, detail="Invalid file name")
    return value


@router.get("", response_model=list[ProjectResponse])
@require_permission("projects", "read")
async def list_projects(request: Request):
    return await get_project_repo(request).list_projects(user_id=_uid())


@router.post("", response_model=ProjectResponse)
@require_permission("projects", "write")
async def create_project(body: ProjectCreateRequest, request: Request):
    try:
        return await get_project_repo(request).create_project(body.name, user_id=_uid())
    except (IntegrityError, ValueError) as exc:
        raise HTTPException(status_code=409, detail="Project name already exists") from exc


@router.get("/{project_id}", response_model=ProjectResponse)
@require_permission("projects", "read")
async def get_project(project_id: str, request: Request):
    value = await get_project_repo(request).get_project(project_id, user_id=_uid())
    if value is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return value


@router.patch("/{project_id}", response_model=ProjectResponse)
@require_permission("projects", "write")
async def update_project(project_id: str, body: ProjectCreateRequest, request: Request):
    try:
        value = await get_project_repo(request).update_project(project_id, name=body.name, user_id=_uid())
    except IntegrityError:
        raise HTTPException(status_code=409, detail="Project name already exists") from None
    if value is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return value


@router.delete("/{project_id}")
@require_permission("projects", "delete")
async def delete_project(project_id: str, request: Request):
    repo = get_project_repo(request)
    if await repo.get_project(project_id, user_id=_uid()) is None:
        raise HTTPException(status_code=404, detail="Project not found")
    deleted = await repo.delete_project(project_id, user_id=_uid())
    if deleted:
        await run_file_io(shutil.rmtree, get_paths().project_dir(_uid(), project_id), ignore_errors=True)
    return {"success": deleted}


@router.get("/{project_id}/files", response_model=list[ProjectFileResponse])
@require_permission("projects", "read")
async def list_project_files(project_id: str, request: Request):
    repo = get_project_repo(request)
    if await repo.get_project(project_id, user_id=_uid()) is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return await repo.list_files(project_id, user_id=_uid())


@router.post("/{project_id}/files", response_model=ProjectFileResponse)
@require_permission("projects", "write")
async def upload_project_file(project_id: str, request: Request, file: UploadFile):
    repo = get_project_repo(request)
    if await repo.get_project(project_id, user_id=_uid()) is None:
        raise HTTPException(status_code=404, detail="Project not found")
    name = _safe_name(file.filename or "upload")
    chunks: list[bytes] = []
    while chunk := await file.read(1024 * 1024):
        chunks.append(chunk)
    data = b"".join(chunks)
    relative_path, size, digest = await store_project_bytes(user_id=_uid(), project_id=project_id, display_name=name, data=data)
    try:
        value = await repo.create_file(project_id, display_name=name, relative_path=relative_path, size_bytes=size, sha256=digest, media_type=file.content_type or mimetypes.guess_type(name)[0], user_id=_uid())
    except (IntegrityError, ValueError):
        await run_file_io(get_paths().project_file_path(_uid(), project_id, relative_path).unlink, missing_ok=True)
        raise HTTPException(status_code=409, detail="File name already exists") from None
    except Exception:
        await run_file_io(get_paths().project_file_path(_uid(), project_id, relative_path).unlink, missing_ok=True)
        raise
    if value is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return value


@router.get("/{project_id}/files/{file_id}", response_model=ProjectFileResponse)
@require_permission("projects", "read")
async def get_project_file(project_id: str, file_id: str, request: Request):
    value = await get_project_repo(request).get_file(file_id, user_id=_uid())
    if value is None or value["project_id"] != project_id:
        raise HTTPException(status_code=404, detail="Project file not found")
    return value


@router.get("/{project_id}/files/{file_id}/content")
@require_permission("projects", "read")
async def download_project_file(project_id: str, file_id: str, request: Request):
    value = await get_project_repo(request).get_file(file_id, user_id=_uid())
    if value is None or value["project_id"] != project_id:
        raise HTTPException(status_code=404, detail="Project file not found")
    path = get_paths().project_file_path(_uid(), project_id, value["relative_path"])
    if not await run_file_io(path.is_file):
        raise HTTPException(status_code=404, detail="Project file content not found")
    return FileResponse(path, media_type=value.get("media_type"), filename=value["display_name"])


@router.delete("/{project_id}/files/{file_id}")
@require_permission("projects", "delete")
async def delete_project_file(project_id: str, file_id: str, request: Request):
    repo = get_project_repo(request)
    value = await repo.get_file(file_id, user_id=_uid())
    if value is None or value["project_id"] != project_id:
        raise HTTPException(status_code=404, detail="Project file not found")
    deleted = await repo.delete_file(file_id, user_id=_uid())
    if deleted:
        await run_file_io(get_paths().project_file_path(_uid(), project_id, value["relative_path"]).unlink, missing_ok=True)
    return {"success": deleted}


@router.post("/{project_id}/files/{file_id}/attach/{thread_id}")
@require_permission("projects", "read")
@require_permission("threads", "write", owner_check=True, require_existing=True)
async def attach_file_to_thread(project_id: str, file_id: str, thread_id: str, body: AttachProjectFileRequest, request: Request, config: AppConfig = Depends(get_config)):
    value = await get_project_repo(request).get_file(file_id, user_id=_uid())
    if value is None or value["project_id"] != project_id:
        raise HTTPException(status_code=404, detail="Project file not found")
    source = get_paths().project_file_path(_uid(), project_id, value["relative_path"])
    if not await run_file_io(source.is_file):
        raise HTTPException(status_code=404, detail="Project file content not found")
    target_name = _safe_name(body.target_name or value["display_name"])
    virtual_path = await attach_project_file(request=request, config=config, user_id=_uid(), thread_id=thread_id, source=source, target_name=target_name, project_id=project_id)
    return {"success": True, "virtual_path": virtual_path}


@router.post("/{project_id}/files/import-thread/{thread_id}", response_model=ProjectFileResponse)
@require_permission("projects", "write")
@require_permission("threads", "read", owner_check=True, require_existing=True)
async def save_thread_file(project_id: str, thread_id: str, body: SaveThreadFileRequest, request: Request, config: AppConfig = Depends(get_config)):
    repo = get_project_repo(request)
    if await repo.get_project(project_id, user_id=_uid()) is None:
        raise HTTPException(status_code=404, detail="Project not found")
    name = _safe_name(body.display_name or Path(body.virtual_path).name)
    try:
        data = await read_thread_file(request=request, config=config, user_id=_uid(), thread_id=thread_id, virtual_path=body.virtual_path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    relative_path, size, digest = await store_project_bytes(user_id=_uid(), project_id=project_id, display_name=name, data=data)
    try:
        value = await repo.create_file(project_id, display_name=name, relative_path=relative_path, size_bytes=size, sha256=digest, media_type=mimetypes.guess_type(name)[0], source_type="thread", source_thread_id=thread_id, user_id=_uid())
    except (IntegrityError, ValueError):
        await run_file_io(get_paths().project_file_path(_uid(), project_id, relative_path).unlink, missing_ok=True)
        raise HTTPException(status_code=409, detail="File name already exists") from None
    except Exception:
        await run_file_io(get_paths().project_file_path(_uid(), project_id, relative_path).unlink, missing_ok=True)
        raise
    if value is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return value
