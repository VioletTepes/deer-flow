"""Persistent project storage ownership and path invariants."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from deerflow.config.paths import Paths
from deerflow.persistence.base import Base
from deerflow.persistence.projects import MemoryProjectRepository, ProjectRepository


@pytest.mark.asyncio
async def test_memory_repository_hides_other_users_projects_and_files() -> None:
    repo = MemoryProjectRepository()
    project = await repo.create_project("Research", user_id="alice")
    file = await repo.create_file(
        project["project_id"],
        display_name="cohort.csv",
        relative_path="blob/cohort.csv",
        size_bytes=3,
        sha256="0" * 64,
        user_id="alice",
    )

    assert await repo.get_project(project["project_id"], user_id="bob") is None
    assert await repo.get_file(file["file_id"], user_id="bob") is None
    assert await repo.list_projects(user_id="bob") == []
    assert await repo.list_files(project["project_id"], user_id="bob") == []
    assert not await repo.delete_file(file["file_id"], user_id="bob")
    assert not await repo.delete_project(project["project_id"], user_id="bob")


@pytest.mark.asyncio
async def test_sql_repository_hides_other_users_projects_and_files(tmp_path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'projects.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        repo = ProjectRepository(async_sessionmaker(engine, expire_on_commit=False))
        project = await repo.create_project("Research", user_id="alice")
        file = await repo.create_file(
            project["project_id"],
            display_name="cohort.csv",
            relative_path="blob/cohort.csv",
            size_bytes=3,
            sha256="0" * 64,
            user_id="alice",
        )

        assert await repo.get_project(project["project_id"], user_id="bob") is None
        assert await repo.get_file(file["file_id"], user_id="bob") is None
        assert await repo.list_projects(user_id="bob") == []
        assert await repo.list_files(project["project_id"], user_id="bob") == []
    finally:
        await engine.dispose()


def test_project_files_live_outside_thread_lifecycle(tmp_path) -> None:
    paths = Paths(tmp_path)
    paths.ensure_thread_dirs("thread-1", user_id="alice")
    paths.ensure_project_dirs("alice", "project-1")
    durable_file = paths.project_file_path("alice", "project-1", "blob/report.csv")
    durable_file.parent.mkdir(parents=True)
    durable_file.write_text("result", encoding="utf-8")

    paths.delete_thread_dir("thread-1", user_id="alice")

    assert durable_file.read_text(encoding="utf-8") == "result"


def test_project_file_path_rejects_traversal(tmp_path) -> None:
    paths = Paths(tmp_path)
    with pytest.raises(ValueError, match="escapes"):
        paths.project_file_path("alice", "project-1", "../other-user/file")
    with pytest.raises(ValueError, match="Invalid project_id"):
        paths.project_files_dir("alice", "../project-1")
