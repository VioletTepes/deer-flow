"""Per-tool-call user context for Agentgateway-backed MCP servers."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from langchain_core.tools import ToolException

from deerflow.mcp.headers import apply_header_overrides
from deerflow.runtime.user_context import get_current_user


class _MatrixMedMcpContextInterceptor:
    def __init__(self, *, secret: str, server_names: set[str], ttl_seconds: int, path: str) -> None:
        self._secret = secret.encode()
        self._server_names = server_names
        self._ttl_seconds = ttl_seconds
        self._path = path

    async def __call__(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        if request.server_name not in self._server_names:
            return await handler(request)

        user = get_current_user()
        subject = getattr(user, "oauth_id", None)
        if not isinstance(subject, str) or not subject:
            raise ToolException("MatrixMed MCP requires an authenticated OIDC user")

        expires_at = (datetime.now(UTC) + timedelta(seconds=self._ttl_seconds)).isoformat()
        request_id = str(uuid.uuid4())
        payload = {
            "expires_at": expires_at,
            "method": "POST",
            "path": self._path,
            "request_id": request_id,
            "subject": subject,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        headers = {
            "X-MatrixMed-Subject": subject,
            "X-MatrixMed-Request-Id": request_id,
            "X-MatrixMed-Expires-At": expires_at,
            "X-MatrixMed-Context-Signature": hmac.new(self._secret, canonical, hashlib.sha256).hexdigest(),
        }
        return await handler(request.override(headers=apply_header_overrides(request.headers, headers)))


def build_matrixmed_agentgateway_mcp_interceptor() -> _MatrixMedMcpContextInterceptor:
    """Build the configured interceptor without persisting user credentials.

    ``mcpInterceptors`` builders are process-level DeerFlow hooks and receive no
    server configuration argument.  The selected MCP connection names and HMAC
    material therefore live in the protected process environment, never in the
    editable MCP configuration exposed by DeerFlow's management API.
    """
    raw_servers = os.environ.get("MATRIXMED_AGENTGATEWAY_MCP_SERVERS", "")
    server_names = {name.strip() for name in raw_servers.split(",") if name.strip()}
    secret = os.environ.get("MATRIXMED_AGENTGATEWAY_CONTEXT_SECRET", "")
    ttl_seconds = int(os.environ.get("MATRIXMED_AGENTGATEWAY_CONTEXT_TTL_SECONDS", "240"))
    path = os.environ.get("MATRIXMED_AGENTGATEWAY_MCP_PATH", "/mcp")
    if not server_names:
        raise ValueError("MATRIXMED_AGENTGATEWAY_MCP_SERVERS is required")
    if not secret:
        raise ValueError("MATRIXMED_AGENTGATEWAY_CONTEXT_SECRET is required")
    if not 0 < ttl_seconds <= 300:
        raise ValueError("MATRIXMED_AGENTGATEWAY_CONTEXT_TTL_SECONDS must be 1..300")
    if not path.startswith("/"):
        raise ValueError("MATRIXMED_AGENTGATEWAY_MCP_PATH must start with '/'")
    return _MatrixMedMcpContextInterceptor(
        secret=secret,
        server_names=server_names,
        ttl_seconds=ttl_seconds,
        path=path,
    )
