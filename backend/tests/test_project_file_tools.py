from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError

from deerflow.config.paths import Paths
from deerflow.tools.builtins import project_files_tool as module


class FakeProjectRepository:
    def __init__(self, project, files):
        self.project = project
        self.files = files

    async def list_projects(self, *, user_id):
        return [self.project] if user_id == self.project["user_id"] else []

    async def get_project(self, project_id, *, user_id):
        return self.project if project_id == self.project["project_id"] and user_id == self.project["user_id"] else None

    async def list_files(self, project_id, *, user_id):
        return self.files if project_id == self.project["project_id"] and user_id == self.project["user_id"] else []

    async def create_file(self, project_id, **kwargs):
        value = {"file_id": "new-file", "project_id": project_id, **kwargs}
        self.files.append(value)
        return value


class ConflictingProjectRepository(FakeProjectRepository):
    async def create_file(self, project_id, **kwargs):
        raise IntegrityError("INSERT", {}, Exception("UNIQUE constraint failed"))


def _runtime(thread_id="thread-1", user_id="alice"):
    return SimpleNamespace(context={"thread_id": thread_id, "user_id": user_id}, config={}, state={})


@pytest.mark.asyncio
async def test_list_project_files_uses_project_name_and_durable_metadata(tmp_path, monkeypatch):
    paths = Paths(tmp_path)
    project = {"project_id": "project-1", "user_id": "alice", "name": "Research"}
    relative_path = "blob/test.html"
    file_path = paths.project_file_path("alice", "project-1", relative_path)
    file_path.parent.mkdir(parents=True)
    file_path.write_text("ok", encoding="utf-8")
    record = {"file_id": "file-1", "project_id": "project-1", "display_name": "test.html", "relative_path": relative_path, "size_bytes": 2, "status": "active"}
    repo = FakeProjectRepository(project, [record])
    monkeypatch.setattr(module, "get_paths", lambda: paths)
    monkeypatch.setattr(module, "_repo", lambda: repo)

    result = await module.list_project_files.coroutine(_runtime(), "Research")

    assert result["projects"][0]["project_name"] == "Research"
    assert result["files"][0]["content_available"] is True
    assert result["files"][0]["path"] is None


@pytest.mark.asyncio
async def test_attach_and_save_project_file_use_thread_virtual_paths(tmp_path, monkeypatch):
    paths = Paths(tmp_path)
    paths.ensure_thread_dirs("thread-1", user_id="alice")
    project = {"project_id": "project-1", "user_id": "alice", "name": "Research"}
    relative_path = "blob/input.txt"
    durable_path = paths.project_file_path("alice", "project-1", relative_path)
    durable_path.parent.mkdir(parents=True)
    durable_path.write_text("durable", encoding="utf-8")
    record = {"file_id": "file-1", "project_id": "project-1", "display_name": "input.txt", "relative_path": relative_path, "size_bytes": 7, "status": "active"}
    repo = FakeProjectRepository(project, [record])
    provider = SimpleNamespace(uses_thread_data_mounts=True)
    monkeypatch.setattr(module, "get_paths", lambda: paths)
    monkeypatch.setattr(module, "_repo", lambda: repo)
    monkeypatch.setattr(module, "get_sandbox_provider", lambda: provider)

    runtime = _runtime()
    attached = await module.attach_project_file.coroutine(runtime, "Research", "input.txt")
    attached_path = paths.resolve_virtual_path("thread-1", attached["virtual_path"], user_id="alice")
    assert attached_path.read_text(encoding="utf-8") == "durable"

    source = paths.resolve_virtual_path("thread-1", "/mnt/user-data/workspace/result.txt", user_id="alice")
    source.write_text("result", encoding="utf-8")
    saved = await module.save_project_file.coroutine(runtime, "Research", "/mnt/user-data/workspace/result.txt")
    assert saved["success"] is True
    saved_path = paths.project_file_path("alice", "project-1", repo.files[-1]["relative_path"])
    assert saved_path.read_text(encoding="utf-8") == "result"


@pytest.mark.asyncio
async def test_save_project_file_returns_friendly_name_conflict(tmp_path, monkeypatch):
    paths = Paths(tmp_path)
    paths.ensure_thread_dirs("thread-1", user_id="alice")
    project = {"project_id": "project-1", "user_id": "alice", "name": "Research"}
    existing = {"file_id": "old-file", "project_id": "project-1", "display_name": "bubble_sort.py"}
    repo = FakeProjectRepository(project, [existing])
    monkeypatch.setattr(module, "get_paths", lambda: paths)
    monkeypatch.setattr(module, "_repo", lambda: repo)
    monkeypatch.setattr(module, "get_sandbox_provider", lambda: SimpleNamespace(uses_thread_data_mounts=True))

    source = paths.resolve_virtual_path("thread-1", "/mnt/user-data/workspace/bubble_sort.py", user_id="alice")
    source.write_text("new version", encoding="utf-8")

    result = await module.save_project_file.coroutine(_runtime(), "Research", "/mnt/user-data/workspace/bubble_sort.py")

    assert result["success"] is False
    assert result["code"] == "project_file_name_conflict"
    assert "already exists" in result["message"]
    assert repo.files == [existing]
    project_files_dir = paths.project_files_dir("alice", "project-1")
    assert not project_files_dir.exists() or not any(path.is_file() for path in project_files_dir.rglob("*"))


@pytest.mark.asyncio
async def test_save_project_file_handles_concurrent_name_conflict(tmp_path, monkeypatch):
    paths = Paths(tmp_path)
    paths.ensure_thread_dirs("thread-1", user_id="alice")
    project = {"project_id": "project-1", "user_id": "alice", "name": "Research"}
    repo = ConflictingProjectRepository(project, [])
    monkeypatch.setattr(module, "get_paths", lambda: paths)
    monkeypatch.setattr(module, "_repo", lambda: repo)
    monkeypatch.setattr(module, "get_sandbox_provider", lambda: SimpleNamespace(uses_thread_data_mounts=True))

    source = paths.resolve_virtual_path("thread-1", "/mnt/user-data/workspace/result.txt", user_id="alice")
    source.write_text("result", encoding="utf-8")

    result = await module.save_project_file.coroutine(_runtime(), "Research", "/mnt/user-data/workspace/result.txt")

    assert result["success"] is False
    assert result["code"] == "project_file_name_conflict"
    project_files_dir = paths.project_files_dir("alice", "project-1")
    assert not any(path.is_file() for path in project_files_dir.rglob("*"))


def test_project_file_tool_descriptions_cover_natural_language_scope():
    description = module.list_project_files.description
    assert "persistent files" in description
    assert "跨会话保存的文件" in description
    assert "current conversation workspace" in description
