"""Transfer files between durable projects and thread sandboxes."""

from __future__ import annotations

import hashlib
import os
import shutil
import uuid
from pathlib import Path

from fastapi import HTTPException, Request

from app.gateway.authz import try_acquire_sandbox_for_request
from deerflow.config.app_config import AppConfig
from deerflow.config.paths import get_paths
from deerflow.sandbox.sandbox_provider import get_sandbox_provider
from deerflow.utils.file_io import run_file_io

_ALLOWED_SAVE_ROOTS = ("workspace", "outputs")


def _copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copyfile(source, staging)
        os.replace(staging, destination)
    finally:
        staging.unlink(missing_ok=True)


def _write_bytes(data: bytes, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        staging.write_bytes(data)
        os.replace(staging, destination)
    finally:
        staging.unlink(missing_ok=True)


def _read_bytes(path: Path) -> bytes:
    return path.read_bytes()


def _thread_source_path(thread_id: str, virtual_path: str, user_id: str) -> Path:
    stripped = virtual_path.lstrip("/")
    parts = Path(stripped).parts
    if len(parts) < 4 or parts[:2] != ("mnt", "user-data") or parts[2] not in _ALLOWED_SAVE_ROOTS:
        raise ValueError("Only files in /mnt/user-data/workspace or /mnt/user-data/outputs can be saved")
    return get_paths().resolve_virtual_path(thread_id, virtual_path, user_id=user_id)


async def attach_project_file(*, request: Request, config: AppConfig, user_id: str, thread_id: str, source: Path, target_name: str, project_id: str | None = None) -> str:
    """Copy a durable file into one thread and sync non-mounted sandboxes."""
    project_prefix = f"{project_id}/" if project_id else ""
    virtual_path = f"/mnt/user-data/workspace/project/{project_prefix}{target_name}"
    target = get_paths().resolve_virtual_path(thread_id, virtual_path, user_id=user_id)
    await run_file_io(_copy_file, source, target)

    provider = get_sandbox_provider()
    if not bool(getattr(provider, "uses_thread_data_mounts", False)):
        lease = await try_acquire_sandbox_for_request(
            request,
            provider,
            thread_id,
            user_id=user_id,
            app_config=config,
            owner_prefix="gateway:project-attach",
            release_on_last=False,
        )
        try:
            if lease.denied:
                raise HTTPException(status_code=403, detail="Sandbox execution is not permitted")
            if lease.sandbox is None:
                raise HTTPException(status_code=500, detail="Failed to acquire sandbox")
            data = await run_file_io(_read_bytes, target)
            await run_file_io(lease.sandbox.update_file, virtual_path, data)
        finally:
            await lease.release()
    return virtual_path


async def read_thread_file(*, request: Request, config: AppConfig, user_id: str, thread_id: str, virtual_path: str) -> bytes:
    """Read an allowed thread file from mounted storage or a remote sandbox."""
    local_path = _thread_source_path(thread_id, virtual_path, user_id)
    provider = get_sandbox_provider()
    if bool(getattr(provider, "uses_thread_data_mounts", False)):
        if not await run_file_io(local_path.is_file):
            raise HTTPException(status_code=404, detail="Thread file not found")
        return await run_file_io(_read_bytes, local_path)

    lease = await try_acquire_sandbox_for_request(
        request,
        provider,
        thread_id,
        user_id=user_id,
        app_config=config,
        owner_prefix="gateway:project-save",
        release_on_last=False,
    )
    try:
        if lease.denied:
            raise HTTPException(status_code=403, detail="Sandbox execution is not permitted")
        if lease.sandbox is None:
            raise HTTPException(status_code=500, detail="Failed to acquire sandbox")
        return await run_file_io(lease.sandbox.download_file, virtual_path)
    except OSError as exc:
        raise HTTPException(status_code=404, detail="Thread file not found") from exc
    finally:
        await lease.release()


async def store_project_bytes(*, user_id: str, project_id: str, display_name: str, data: bytes) -> tuple[str, int, str]:
    """Atomically store bytes and return relative path, size, and digest."""
    relative_path = f"{uuid.uuid4()}/{display_name}"
    destination = get_paths().project_file_path(user_id, project_id, relative_path)
    await run_file_io(_write_bytes, data, destination)
    return relative_path, len(data), hashlib.sha256(data).hexdigest()
