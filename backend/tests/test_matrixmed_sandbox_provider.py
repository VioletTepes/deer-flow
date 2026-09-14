from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from types import SimpleNamespace

from deerflow.community.matrixmed.provider import MatrixMedSandboxProvider
from deerflow.sandbox.runtime_identity import sandbox_identity_scope


def _provider(monkeypatch) -> MatrixMedSandboxProvider:
    import deerflow.community.matrixmed.provider as module

    monkeypatch.setattr(
        module,
        "get_app_config",
        lambda: SimpleNamespace(
            sandbox=SimpleNamespace(
                matrixmed_api_url="http://sandbox.internal",
                matrixmed_provider_shared_secret="provider-secret",
                matrixmed_project_key="deerflow",
                matrixmed_allow_default_project=True,
                matrixmed_request_timeout=10,
            )
        ),
    )
    return MatrixMedSandboxProvider()


def test_acquire_uses_oidc_subject_and_signed_context(monkeypatch):
    provider = _provider(monkeypatch)
    calls: list[tuple[str, dict, dict]] = []

    def post(path, body, headers):
        calls.append((path, body, headers))
        return {"context_token": "context-token", "context_id": "123e4567-e89b-12d3-a456-426614174000", "expires_at": "2030-01-01T00:00:00+00:00"}

    monkeypatch.setattr(provider, "_post_json", post)
    with sandbox_identity_scope({"oauth_id": "keycloak-subject"}):
        sandbox_id = provider.acquire("deerflow-thread", user_id="deerflow-user")

    assert sandbox_id.startswith("matrixmed-")
    path, body, headers = calls[0]
    assert path == "/api/v1/contexts"
    assert body["subject"] == "keycloak-subject"
    assert body["project_key"] == "deerflow"
    assert body["thread_id"] == hashlib.sha256(b"deerflow-thread").hexdigest()[:32]
    canonical = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    assert headers["X-Sandbox-Provider-Signature"] == hmac.new(b"provider-secret", canonical, hashlib.sha256).hexdigest()


def test_acquire_fails_closed_without_oidc_subject(monkeypatch):
    provider = _provider(monkeypatch)

    try:
        provider.acquire("deerflow-thread", user_id="deerflow-user")
    except PermissionError as exc:
        assert "OIDC subject" in str(exc)
    else:  # pragma: no cover - test assertion guard
        raise AssertionError("provider acquired a sandbox without an OIDC subject")


def test_acquire_uses_trusted_project_context_instead_of_default(monkeypatch):
    provider = _provider(monkeypatch)
    calls: list[dict] = []
    monkeypatch.setattr(
        provider,
        "_post_json",
        lambda path, body, headers: calls.append(body) or {
            "context_token": "context-token",
            "context_id": "123e4567-e89b-12d3-a456-426614174000",
            "expires_at": "2030-01-01T00:00:00+00:00",
        },
    )

    with sandbox_identity_scope({"oauth_id": "keycloak-subject", "matrixmed_project_key": "study-alpha"}):
        provider.acquire("deerflow-thread", user_id="deerflow-user")

    assert calls[0]["project_key"] == "study-alpha"


def test_acquire_rejects_missing_project_in_non_development_mode(monkeypatch):
    provider = _provider(monkeypatch)
    provider._allow_default_project = False

    with sandbox_identity_scope({"oauth_id": "keycloak-subject"}):
        try:
            provider.acquire("deerflow-thread", user_id="deerflow-user")
        except PermissionError as exc:
            assert "matrixmed_project_key" in str(exc)
        else:  # pragma: no cover - test assertion guard
            raise AssertionError("provider acquired a sandbox without a project context")


def test_virtual_paths_are_translated_before_execution(monkeypatch):
    provider = _provider(monkeypatch)
    requests: list[dict] = []

    def post(path, body, headers):
        if path == "/api/v1/contexts":
            return {"context_token": "context-token", "context_id": "123e4567-e89b-12d3-a456-426614174000", "expires_at": "2030-01-01T00:00:00+00:00"}
        requests.append(body)
        return {"exit_code": 0, "stdout": "ok\n", "stderr": ""}

    monkeypatch.setattr(provider, "_post_json", post)
    with sandbox_identity_scope({"oauth_id": "keycloak-subject"}):
        sandbox_id = provider.acquire("deerflow-thread", user_id="deerflow-user")
    sandbox = provider.get(sandbox_id)
    assert sandbox is not None
    sandbox.execute_command("cd /mnt/user-data/workspace && pwd")
    assert requests[0]["language"] == "bash"
    assert requests[0]["code"] == "cd /workspace && pwd"
    assert uuid.UUID(requests[0]["execution_id"])


def test_command_path_translation_does_not_rewrite_prefix_lookalikes(monkeypatch):
    provider = _provider(monkeypatch)
    monkeypatch.setattr(
        provider,
        "_post_json",
        lambda path, body, headers: (
            {
                "context_token": "context-token",
                "context_id": "123e4567-e89b-12d3-a456-426614174000",
                "expires_at": "2030-01-01T00:00:00+00:00",
            }
            if path == "/api/v1/contexts"
            else {"exit_code": 0, "stdout": "", "stderr": ""}
        ),
    )
    with sandbox_identity_scope({"oauth_id": "keycloak-subject"}):
        sandbox_id = provider.acquire("deerflow-thread", user_id="deerflow-user")
    sandbox = provider.get(sandbox_id)
    assert sandbox is not None
    assert sandbox._translate_command("echo /mnt/user-data/workspace-copy") == "echo /mnt/user-data/workspace-copy"
    assert sandbox._translate_command("echo done > /mnt/user-data/outputs/report.txt") == "echo done > /output/report.txt"


def test_file_operations_use_logical_file_api(monkeypatch):
    provider = _provider(monkeypatch)
    calls: list[dict] = []
    context = {"context_token": "context-token", "context_id": "123e4567-e89b-12d3-a456-426614174000", "expires_at": "2030-01-01T00:00:00+00:00"}
    monkeypatch.setattr(provider, "_post_json", lambda path, body, headers: context)

    def file_json(sandbox, **kwargs):
        calls.append(kwargs)
        return {"content": "stored"} if kwargs["endpoint"] == "files:read" else {}

    monkeypatch.setattr(provider, "file_json", file_json)
    monkeypatch.setattr(provider, "download_file", lambda sandbox, path: b"bytes")
    with sandbox_identity_scope({"oauth_id": "keycloak-subject"}):
        sandbox_id = provider.acquire("deerflow-thread", user_id="deerflow-user")
    sandbox = provider.get(sandbox_id)
    assert sandbox is not None
    sandbox.write_file("/mnt/user-data/workspace/note.txt", "stored")
    assert sandbox.read_file("/mnt/user-data/workspace/note.txt") == "stored"
    assert sandbox.download_file("/mnt/user-data/workspace/note.txt") == b"bytes"
    assert calls[0]["endpoint"] == "files"
    assert calls[0]["path"] == "/workspace/note.txt"
    assert calls[1]["endpoint"] == "files:read"


def test_search_and_output_paths_use_logical_file_api(monkeypatch):
    provider = _provider(monkeypatch)
    calls: list[dict] = []
    context = {"context_token": "context-token", "context_id": "123e4567-e89b-12d3-a456-426614174000", "expires_at": "2030-01-01T00:00:00+00:00"}
    monkeypatch.setattr(provider, "_post_json", lambda path, body, headers: context)

    def file_json(sandbox, **kwargs):
        calls.append(kwargs)
        if kwargs["endpoint"] == "files:glob":
            return {"paths": ["/workspace/analysis.py"], "truncated": False}
        if kwargs["endpoint"] == "files:grep":
            return {"matches": [{"path": "/workspace/analysis.py", "line_number": 1, "line": "value = 1"}], "truncated": False}
        return {}

    monkeypatch.setattr(provider, "file_json", file_json)
    with sandbox_identity_scope({"oauth_id": "keycloak-subject"}):
        sandbox_id = provider.acquire("deerflow-thread", user_id="deerflow-user")
    sandbox = provider.get(sandbox_id)
    assert sandbox is not None
    assert sandbox.glob("/mnt/user-data/workspace", "*.py") == (["/mnt/user-data/workspace/analysis.py"], False)
    matches, truncated = sandbox.grep("/mnt/user-data/workspace", "value")
    assert matches[0].path == "/mnt/user-data/workspace/analysis.py"
    assert not truncated
    sandbox.write_file("/mnt/user-data/outputs/report.txt", "done")
    assert [call["endpoint"] for call in calls] == ["files:glob", "files:grep", "files"]
    assert calls[-1]["path"] == "/outputs/report.txt"


def test_project_file_v1_uses_only_the_bound_context(monkeypatch):
    provider = _provider(monkeypatch)
    context = {
        "context_token": "context-token",
        "context_id": "123e4567-e89b-12d3-a456-426614174000",
        "expires_at": "2030-01-01T00:00:00+00:00",
    }
    monkeypatch.setattr(provider, "_post_json", lambda path, body, headers: context)
    calls: list[tuple[str, str, dict | None, dict]] = []
    file_id = "123e4567-e89b-12d3-a456-426614174001"

    def request(path, *, method, body, headers):
        calls.append((path, method, body, headers))
        if method == "GET":
            return {"files": [{"file_id": file_id, "version": 1, "display_name": "report.csv"}]}
        if path.endswith(":attach"):
            return {
                "file": {"file_id": file_id, "version": 1, "display_name": "report.csv"},
                "thread_path": "/workspace/project/123e4567-e89b-12d3-a456-426614174001/report.csv",
            }
        return {"file_id": file_id, "version": 1, "display_name": "report.csv"}

    monkeypatch.setattr(provider, "_request_json", request)
    with sandbox_identity_scope({"oauth_id": "keycloak-subject", "matrixmed_project_key": "study-alpha"}):
        sandbox_id = provider.acquire("deerflow-thread", user_id="deerflow-user")
    sandbox = provider.get(sandbox_id)
    assert sandbox is not None

    assert sandbox.list_project_files()[0]["file_id"] == file_id
    saved = sandbox.save_project_file(
        "/mnt/user-data/query-results/cohort.csv", display_name="report.csv", kind="report"
    )
    jupyter_saved = sandbox.save_project_file(
        "/mnt/user-data/jupyter/analysis.ipynb", display_name="analysis.ipynb", kind="notebook"
    )
    attached, virtual_path = sandbox.attach_project_file(file_id, version=1)

    assert saved["file_id"] == file_id
    assert jupyter_saved["file_id"] == file_id
    assert attached["version"] == 1
    assert virtual_path == "/mnt/user-data/workspace/project/123e4567-e89b-12d3-a456-426614174001/report.csv"
    assert [call[0] for call in calls] == [
        "/api/v1/contexts/123e4567-e89b-12d3-a456-426614174000/project-files",
        "/api/v1/contexts/123e4567-e89b-12d3-a456-426614174000/project-files",
        "/api/v1/contexts/123e4567-e89b-12d3-a456-426614174000/project-files",
        "/api/v1/contexts/123e4567-e89b-12d3-a456-426614174000/project-files/123e4567-e89b-12d3-a456-426614174001:attach",
    ]
    assert calls[1][2] == {"source_path": "/query-results/cohort.csv", "display_name": "report.csv", "kind": "report"}
    assert calls[2][2] == {"source_path": "/jupyter/analysis.ipynb", "display_name": "analysis.ipynb", "kind": "notebook"}
    assert calls[3][2] == {"version": 1, "target_name": None}
    assert all(call[3] == {"X-Sandbox-Context": "context-token"} for call in calls)
