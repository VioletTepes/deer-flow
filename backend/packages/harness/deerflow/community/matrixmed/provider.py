"""DeerFlow provider backed by the MatrixMed Sandbox API.

The remote API is a shared executor: acquiring a DeerFlow sandbox creates no
container or Python server.  It only obtains a short-lived, user-bound context
token that the API validates against MatrixMed Identity on every use.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import threading
import uuid
from datetime import UTC, datetime, timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from deerflow.config import get_app_config
from deerflow.sandbox.identity import derive_sandbox_scope_token
from deerflow.sandbox.runtime_identity import sandbox_project_key, sandbox_subject
from deerflow.sandbox.sandbox import Sandbox
from deerflow.sandbox.sandbox_provider import SandboxProvider

from .sandbox import MatrixMedSandbox

_PROJECT_KEY = re.compile(r"[a-z][a-z0-9-]{0,62}\Z")
_REMOTE_THREAD_ID = re.compile(r"[0-9a-f-]{8,64}\Z")


class MatrixMedSandboxProvider(SandboxProvider):
    """Use MatrixMed's shared NsJail executor as a DeerFlow provider."""

    uses_thread_data_mounts = False
    needs_upload_permission_adjustment = False
    supports_agent_skill_isolation = False

    def __init__(self) -> None:
        config = get_app_config().sandbox
        self._base_url = self._config_value(getattr(config, "matrixmed_api_url", "")).rstrip("/")
        configured_secret = self._config_value(getattr(config, "matrixmed_provider_shared_secret", ""))
        self._secret = configured_secret or os.environ.get("MATRIXMED_SANDBOX_PROVIDER_SHARED_SECRET", "")
        self._default_project_key = self._config_value(getattr(config, "matrixmed_default_project_key", "") or getattr(config, "matrixmed_project_key", ""))
        self._allow_default_project = bool(getattr(config, "matrixmed_allow_default_project", False))
        self._request_timeout = float(getattr(config, "matrixmed_request_timeout", 130) or 130)
        if not self._base_url:
            raise ValueError("sandbox.matrixmed_api_url is required for MatrixMedSandboxProvider")
        if not self._secret:
            raise ValueError("sandbox.matrixmed_provider_shared_secret or MATRIXMED_SANDBOX_PROVIDER_SHARED_SECRET is required")
        if self._default_project_key and not _PROJECT_KEY.fullmatch(self._default_project_key):
            raise ValueError("sandbox.matrixmed_default_project_key must be a lowercase MatrixMed project key")
        if self._allow_default_project and not self._default_project_key:
            raise ValueError("sandbox.matrixmed_allow_default_project requires matrixmed_default_project_key")
        if self._request_timeout <= 0:
            raise ValueError("sandbox.matrixmed_request_timeout must be positive")
        self._lock = threading.RLock()
        self._sandboxes: dict[str, MatrixMedSandbox] = {}
        self._thread_sandboxes: dict[tuple[str, str, str], str] = {}

    @staticmethod
    def _config_value(value: object) -> str:
        """Resolve DeerFlow's documented ``$ENV_VAR`` secret convention."""
        result = str(value or "")
        return os.environ.get(result[1:], "") if result.startswith("$") else result

    @staticmethod
    def _remote_thread_id(thread_id: str) -> str:
        if _REMOTE_THREAD_ID.fullmatch(thread_id):
            return thread_id
        return hashlib.sha256(thread_id.encode()).hexdigest()[:32]

    @staticmethod
    def _sandbox_id(thread_id: str, user_id: str, project_key: str) -> str:
        # A thread can only be reused inside its already-authorized project.
        # The public SandboxProvider id is opaque, so include project scope here
        # rather than accidentally reusing a different project's context.
        scoped_thread = f"{project_key}\x00{thread_id}"
        return f"matrixmed-{derive_sandbox_scope_token(user_id=user_id, thread_id=scoped_thread)}"

    def _signed_context(self, *, subject: str, project_key: str, thread_id: str) -> dict[str, object]:
        expires_at = datetime.now(UTC) + timedelta(seconds=240)
        body: dict[str, object] = {
            "request_id": str(uuid.uuid4()),
            "subject": subject,
            "project_key": project_key,
            "thread_id": self._remote_thread_id(thread_id),
            "expires_at": expires_at.isoformat(),
        }
        canonical = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
        signature = hmac.new(self._secret.encode(), canonical, hashlib.sha256).hexdigest()
        response = self._post_json("/api/v1/contexts", body, {"X-Sandbox-Provider-Signature": signature})
        if not isinstance(response.get("context_token"), str):
            raise RuntimeError("MatrixMed Sandbox API returned no context token")
        return response

    def _request_json(self, path: str, *, method: str, body: dict[str, object] | None, headers: dict[str, str]) -> dict[str, object]:
        request = Request(
            f"{self._base_url}{path}",
            data=json.dumps(body, separators=(",", ":")).encode() if body is not None else None,
            headers={"Content-Type": "application/json", **headers},
            method=method,
        )
        try:
            with urlopen(request, timeout=self._request_timeout) as response:  # noqa: S310 - configured private service endpoint
                payload = json.loads(response.read().decode())
        except HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:1000]
            raise RuntimeError(f"MatrixMed Sandbox API rejected {path}: HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError) as exc:
            raise RuntimeError(f"MatrixMed Sandbox API is unavailable: {exc}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("MatrixMed Sandbox API returned an invalid response")
        return payload

    def _post_json(self, path: str, body: dict[str, object], headers: dict[str, str]) -> dict[str, object]:
        return self._request_json(path, method="POST", body=body, headers=headers)

    def acquire(self, thread_id: str | None = None, *, user_id: str | None = None) -> str:
        subject = sandbox_subject()
        if not subject:
            raise PermissionError("MatrixMed sandbox requires an authenticated OIDC subject")
        project_key = sandbox_project_key()
        if project_key is None and self._allow_default_project:
            project_key = self._default_project_key
        if project_key is None:
            raise PermissionError("MatrixMed sandbox requires a trusted matrixmed_project_key; the default project is only permitted in explicit development configuration")
        source_thread_id = thread_id or str(uuid.uuid4())
        local_user_id = user_id or subject
        key = (local_user_id, project_key, source_thread_id)
        sandbox_id = self._sandbox_id(source_thread_id, local_user_id, project_key)
        with self._lock:
            existing = self._thread_sandboxes.get(key)
            if existing is not None and existing in self._sandboxes:
                return existing
        context = self._signed_context(subject=subject, project_key=project_key, thread_id=source_thread_id)
        sandbox = MatrixMedSandbox(
            sandbox_id,
            provider=self,
            subject=subject,
            project_key=project_key,
            source_thread_id=source_thread_id,
            context_token=str(context["context_token"]),
            context_expires_at=str(context.get("expires_at") or ""),
            context_id=str(context["context_id"]),
        )
        with self._lock:
            prior = self._thread_sandboxes.get(key)
            if prior is not None and prior in self._sandboxes:
                return prior
            self._sandboxes[sandbox_id] = sandbox
            self._thread_sandboxes[key] = sandbox_id
        return sandbox_id

    def refresh_context(self, sandbox: MatrixMedSandbox) -> tuple[str, str, str]:
        context = self._signed_context(subject=sandbox.subject, project_key=sandbox.project_key, thread_id=sandbox.source_thread_id)
        return str(context["context_token"]), str(context.get("expires_at") or ""), str(context["context_id"])

    def execute(self, sandbox: MatrixMedSandbox, *, language: str, code: str) -> dict[str, object]:
        # A caller-known identifier makes an in-flight short-lived NsJail run
        # observable and cancellable through the Sandbox API without creating a
        # persistent kernel or per-user execution service.
        execution_id = str(uuid.uuid4())
        return self._post_json(
            "/api/v1/executions",
            {"execution_id": execution_id, "language": language, "code": code},
            {"X-Sandbox-Context": sandbox.context_token()},
        )

    def execution_json(self, sandbox: MatrixMedSandbox, *, method: str, endpoint: str) -> dict[str, object]:
        return self._request_json(
            f"/api/v1/executions/{endpoint}",
            method=method,
            body=None,
            headers={"X-Sandbox-Context": sandbox.context_token()},
        )

    def file_json(self, sandbox: MatrixMedSandbox, *, method: str, endpoint: str, path: str, body: dict[str, object] | None = None, extra_query: dict[str, object] | None = None) -> dict[str, object]:
        query = urlencode({"path": path, **(extra_query or {})})
        return self._request_json(
            f"/api/v1/contexts/{sandbox.context_id()}/{endpoint}?{query}",
            method=method,
            body=body,
            headers={"X-Sandbox-Context": sandbox.context_token()},
        )

    def project_file_json(
        self,
        sandbox: MatrixMedSandbox,
        *,
        method: str,
        suffix: str = "",
        body: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Call the project-file API for the sandbox's already-bound project.

        There is deliberately no project key argument here.  The API derives
        it from the short-lived context, so an Agent cannot name a different
        project or turn an opaque file reference into a cross-project read.
        """
        return self._request_json(
            f"/api/v1/contexts/{sandbox.context_id()}/project-files{suffix}",
            method=method,
            body=body,
            headers={"X-Sandbox-Context": sandbox.context_token()},
        )

    def download_file(self, sandbox: MatrixMedSandbox, path: str) -> bytes:
        request = Request(
            f"{self._base_url}/api/v1/contexts/{sandbox.context_id()}/files:download?{urlencode({'path': path})}",
            headers={"X-Sandbox-Context": sandbox.context_token()},
            method="GET",
        )
        try:
            with urlopen(request, timeout=self._request_timeout) as response:  # noqa: S310 - configured private service endpoint
                return response.read()
        except HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:1000]
            raise OSError(f"MatrixMed Sandbox API rejected file download: HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError) as exc:
            raise OSError(f"MatrixMed Sandbox API is unavailable: {exc}") from exc

    def get(self, sandbox_id: str) -> Sandbox | None:
        with self._lock:
            return self._sandboxes.get(sandbox_id)

    def release(self, sandbox_id: str) -> None:
        with self._lock:
            self._sandboxes.pop(sandbox_id, None)
            for key, value in list(self._thread_sandboxes.items()):
                if value == sandbox_id:
                    self._thread_sandboxes.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._sandboxes.clear()
            self._thread_sandboxes.clear()


__all__ = ["MatrixMedSandboxProvider"]
