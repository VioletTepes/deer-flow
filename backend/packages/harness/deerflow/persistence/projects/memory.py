"""Process-local project repository used by database.backend=memory."""

from __future__ import annotations

import uuid
from typing import Any

from deerflow.runtime.user_context import AUTO, _AutoSentinel, resolve_user_id
from deerflow.utils.time import now_iso


class MemoryProjectRepository:
    def __init__(self) -> None:
        self._projects: dict[str, dict[str, Any]] = {}
        self._files: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _owner(user_id: str | None | _AutoSentinel, name: str) -> str:
        owner = resolve_user_id(user_id, method_name=name)
        if owner is None:
            raise ValueError("project owner is required")
        return owner

    async def create_project(self, name: str, *, user_id=AUTO, project_id: str | None = None) -> dict:
        owner = self._owner(user_id, "MemoryProjectRepository.create_project")
        if any(p["user_id"] == owner and p["name"] == name.strip() for p in self._projects.values()):
            raise ValueError("project name already exists")
        now = now_iso()
        value = {"project_id": project_id or str(uuid.uuid4()), "user_id": owner, "name": name.strip(), "created_at": now, "updated_at": now}
        self._projects[value["project_id"]] = value
        return dict(value)

    async def get_project(self, project_id: str, *, user_id=AUTO):
        owner = resolve_user_id(user_id, method_name="MemoryProjectRepository.get_project")
        value = self._projects.get(project_id)
        return dict(value) if value and (owner is None or value["user_id"] == owner) else None

    async def list_projects(self, *, user_id=AUTO):
        owner = resolve_user_id(user_id, method_name="MemoryProjectRepository.list_projects")
        values = [v for v in self._projects.values() if owner is None or v["user_id"] == owner]
        return [dict(v) for v in sorted(values, key=lambda v: (v["updated_at"], v["project_id"]), reverse=True)]

    async def update_project(self, project_id: str, *, name: str, user_id=AUTO):
        value = await self.get_project(project_id, user_id=user_id)
        if value is None:
            return None
        value["name"] = name.strip()
        value["updated_at"] = now_iso()
        self._projects[project_id] = value
        return dict(value)

    async def delete_project(self, project_id: str, *, user_id=AUTO) -> bool:
        value = await self.get_project(project_id, user_id=user_id)
        if value is None:
            return False
        self._projects.pop(project_id, None)
        for file_id, file in list(self._files.items()):
            if file["project_id"] == project_id:
                self._files.pop(file_id)
        return True

    async def create_file(self, project_id: str, *, display_name: str, relative_path: str, size_bytes: int, sha256: str, media_type=None, source_type="upload", source_thread_id=None, user_id=AUTO):
        project = await self.get_project(project_id, user_id=user_id)
        if project is None:
            return None
        if any(f["project_id"] == project_id and f["display_name"] == display_name for f in self._files.values()):
            raise ValueError("file name already exists")
        now = now_iso()
        value = {
            "file_id": str(uuid.uuid4()),
            "project_id": project_id,
            "user_id": project["user_id"],
            "display_name": display_name,
            "media_type": media_type,
            "size_bytes": size_bytes,
            "sha256": sha256,
            "relative_path": relative_path,
            "source_type": source_type,
            "source_thread_id": source_thread_id,
            "version": 1,
            "status": "active",
            "created_at": now,
            "updated_at": now,
        }
        self._files[value["file_id"]] = value
        return dict(value)

    async def get_file(self, file_id: str, *, user_id=AUTO):
        owner = resolve_user_id(user_id, method_name="MemoryProjectRepository.get_file")
        value = self._files.get(file_id)
        return dict(value) if value and (owner is None or value["user_id"] == owner) else None

    async def list_files(self, project_id: str, *, user_id=AUTO):
        project = await self.get_project(project_id, user_id=user_id)
        if project is None:
            return []
        return [dict(v) for v in sorted(self._files.values(), key=lambda v: v["display_name"]) if v["project_id"] == project_id]

    async def delete_file(self, file_id: str, *, user_id=AUTO) -> bool:
        value = await self.get_file(file_id, user_id=user_id)
        if value is None:
            return False
        self._files.pop(file_id, None)
        return True
