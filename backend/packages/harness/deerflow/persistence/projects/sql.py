"""SQL-backed project repository with mandatory user scoping."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deerflow.persistence.projects.model import ProjectFileRow, ProjectRow
from deerflow.runtime.user_context import AUTO, _AutoSentinel, resolve_user_id
from deerflow.utils.time import coerce_iso


class ProjectRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    @staticmethod
    def _project(row: ProjectRow) -> dict[str, Any]:
        value = row.to_dict()
        for key in ("created_at", "updated_at"):
            value[key] = coerce_iso(value[key])
        return value

    @staticmethod
    def _file(row: ProjectFileRow) -> dict[str, Any]:
        value = row.to_dict()
        for key in ("created_at", "updated_at"):
            value[key] = coerce_iso(value[key])
        return value

    async def create_project(self, name: str, *, user_id: str | None | _AutoSentinel = AUTO, project_id: str | None = None) -> dict:
        owner = resolve_user_id(user_id, method_name="ProjectRepository.create_project")
        if owner is None:
            raise ValueError("project owner is required")
        row = ProjectRow(project_id=project_id or str(uuid.uuid4()), user_id=owner, name=name.strip(), created_at=datetime.now(UTC), updated_at=datetime.now(UTC))
        async with self._sf() as session:
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                raise
            await session.refresh(row)
            return self._project(row)

    async def get_project(self, project_id: str, *, user_id: str | None | _AutoSentinel = AUTO) -> dict | None:
        owner = resolve_user_id(user_id, method_name="ProjectRepository.get_project")
        async with self._sf() as session:
            row = await session.get(ProjectRow, project_id)
            if row is None or (owner is not None and row.user_id != owner):
                return None
            return self._project(row)

    async def list_projects(self, *, user_id: str | None | _AutoSentinel = AUTO) -> list[dict]:
        owner = resolve_user_id(user_id, method_name="ProjectRepository.list_projects")
        stmt = select(ProjectRow).order_by(ProjectRow.updated_at.desc(), ProjectRow.project_id.asc())
        if owner is not None:
            stmt = stmt.where(ProjectRow.user_id == owner)
        async with self._sf() as session:
            return [self._project(row) for row in (await session.execute(stmt)).scalars()]

    async def update_project(self, project_id: str, *, name: str, user_id: str | None | _AutoSentinel = AUTO) -> dict | None:
        owner = resolve_user_id(user_id, method_name="ProjectRepository.update_project")
        async with self._sf() as session:
            row = await session.get(ProjectRow, project_id)
            if row is None or (owner is not None and row.user_id != owner):
                return None
            row.name = name.strip()
            row.updated_at = datetime.now(UTC)
            await session.commit()
            return self._project(row)

    async def delete_project(self, project_id: str, *, user_id: str | None | _AutoSentinel = AUTO) -> bool:
        owner = resolve_user_id(user_id, method_name="ProjectRepository.delete_project")
        async with self._sf() as session:
            row = await session.get(ProjectRow, project_id)
            if row is None or (owner is not None and row.user_id != owner):
                return False
            await session.delete(row)
            await session.commit()
            return True

    async def create_file(
        self,
        project_id: str,
        *,
        display_name: str,
        relative_path: str,
        size_bytes: int,
        sha256: str,
        media_type: str | None = None,
        source_type: str = "upload",
        source_thread_id: str | None = None,
        user_id: str | None | _AutoSentinel = AUTO,
    ) -> dict | None:
        owner = resolve_user_id(user_id, method_name="ProjectRepository.create_file")
        async with self._sf() as session:
            project = await session.get(ProjectRow, project_id)
            if project is None or (owner is not None and project.user_id != owner):
                return None
            row = ProjectFileRow(
                file_id=str(uuid.uuid4()),
                project_id=project_id,
                user_id=project.user_id,
                display_name=display_name,
                relative_path=relative_path,
                size_bytes=size_bytes,
                sha256=sha256,
                media_type=media_type,
                source_type=source_type,
                source_thread_id=source_thread_id,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                raise
            await session.refresh(row)
            return self._file(row)

    async def get_file(self, file_id: str, *, user_id: str | None | _AutoSentinel = AUTO) -> dict | None:
        owner = resolve_user_id(user_id, method_name="ProjectRepository.get_file")
        async with self._sf() as session:
            row = await session.get(ProjectFileRow, file_id)
            if row is None or (owner is not None and row.user_id != owner):
                return None
            return self._file(row)

    async def list_files(self, project_id: str, *, user_id: str | None | _AutoSentinel = AUTO) -> list[dict]:
        owner = resolve_user_id(user_id, method_name="ProjectRepository.list_files")
        stmt = select(ProjectFileRow).where(ProjectFileRow.project_id == project_id).order_by(ProjectFileRow.display_name.asc())
        if owner is not None:
            stmt = stmt.where(ProjectFileRow.user_id == owner)
        async with self._sf() as session:
            return [self._file(row) for row in (await session.execute(stmt)).scalars()]

    async def delete_file(self, file_id: str, *, user_id: str | None | _AutoSentinel = AUTO) -> bool:
        owner = resolve_user_id(user_id, method_name="ProjectRepository.delete_file")
        async with self._sf() as session:
            row = await session.get(ProjectFileRow, file_id)
            if row is None or (owner is not None and row.user_id != owner):
                return False
            await session.delete(row)
            await session.commit()
            return True
