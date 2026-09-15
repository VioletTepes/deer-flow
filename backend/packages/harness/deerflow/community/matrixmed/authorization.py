"""Identity-backed authorization provider for MatrixMed model and MCP resources.

The provider never contains roles or capability grants.  It signs the already
authenticated OIDC subject from DeerFlow's runtime context and asks the local
Policy API to evaluate the resource against MatrixMed Identity.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from deerflow.authz.provider import AuthzDecision, AuthzReason, AuthzRequest, Principal


class MatrixMedAuthorizationProvider:
    """Filter and authorize models/MCP resources through Agentgateway Policy API."""

    name = "matrixmed-agentgateway"

    def __init__(
        self,
        *,
        policy_api_url: str,
        context_shared_secret: str,
        request_timeout: float = 5.0,
        context_ttl_seconds: int = 240,
    ) -> None:
        self._base_url = self._resolve(policy_api_url).rstrip("/")
        self._secret = self._resolve(context_shared_secret)
        self._timeout = float(request_timeout)
        self._ttl = int(context_ttl_seconds)
        if not self._base_url or not self._secret:
            raise ValueError("MatrixMed Policy API URL and context shared secret are required")
        if self._timeout <= 0 or self._ttl <= 0 or self._ttl > 300:
            raise ValueError("MatrixMed Policy API timeout and context TTL must be positive and TTL <= 300")

    @staticmethod
    def _resolve(value: str) -> str:
        value = str(value or "")
        return os.environ.get(value[1:], "") if value.startswith("$") else value

    @staticmethod
    def _resource_type(resource: str) -> str | None:
        return {
            "model": "model",
            "mcp_server": "mcp_target",
            "mcp_tool": "mcp_tool",
        }.get(resource)

    def _headers(self, *, method: str, path: str, subject: str) -> dict[str, str]:
        expires_at = (datetime.now(UTC) + timedelta(seconds=self._ttl)).isoformat()
        request_id = str(uuid.uuid4())
        body = {
            "expires_at": expires_at,
            "method": method,
            "path": path,
            "request_id": request_id,
            "subject": subject,
        }
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        signature = hmac.new(self._secret.encode(), canonical, hashlib.sha256).hexdigest()
        return {
            "Content-Type": "application/json",
            "X-MatrixMed-Subject": subject,
            "X-MatrixMed-Request-Id": request_id,
            "X-MatrixMed-Expires-At": expires_at,
            "X-MatrixMed-Context-Signature": signature,
        }

    def _post(self, path: str, *, subject: str, payload: dict[str, object]) -> dict[str, object]:
        request = Request(
            f"{self._base_url}{path}",
            data=json.dumps(payload, separators=(",", ":")).encode(),
            headers=self._headers(method="POST", path=path, subject=subject),
            method="POST",
        )
        try:
            with urlopen(request, timeout=self._timeout) as response:  # noqa: S310 - operator-configured internal endpoint
                body = json.loads(response.read().decode())
        except (HTTPError, URLError, TimeoutError, ValueError) as exc:
            raise RuntimeError("MatrixMed Policy API is unavailable") from exc
        if not isinstance(body, dict):
            raise RuntimeError("MatrixMed Policy API returned invalid JSON")
        return body

    @staticmethod
    def _subject(principal: Principal) -> str | None:
        return principal.oauth_id if isinstance(principal.oauth_id, str) and principal.oauth_id else None

    def authorize(self, request: AuthzRequest) -> AuthzDecision:
        resource_type = self._resource_type(request.resource)
        if resource_type is None:
            # This provider owns only Agentgateway resources. Existing DeerFlow
            # route/sandbox/tool behavior must not be silently redefined.
            return AuthzDecision(allow=True, policy_id=self.name)
        subject = self._subject(request.principal)
        if subject is None:
            return AuthzDecision(
                allow=False,
                policy_id=self.name,
                reasons=[AuthzReason("missing_oidc_subject", "MatrixMed resource requires an OIDC user")],
            )
        try:
            result = self._post(
                "/api/v1/authorization/check",
                subject=subject,
                payload={
                    "resource_type": resource_type,
                    "resource_id": request.target,
                    "action": request.action,
                },
            )
            allowed = result.get("allow") is True
            reason = str(result.get("reason") or "policy_response_invalid")
        except RuntimeError:
            allowed, reason = False, "policy_unavailable"
        return AuthzDecision(
            allow=allowed,
            policy_id=self.name,
            reasons=[] if allowed else [AuthzReason(reason, "MatrixMed Agentgateway policy denied access")],
        )

    async def aauthorize(self, request: AuthzRequest) -> AuthzDecision:
        return await asyncio.to_thread(self.authorize, request)

    def filter_resources(self, principal: Principal, resource_type: str, candidates: list[str]) -> list[str]:
        mapped = self._resource_type(resource_type)
        if mapped is None:
            return list(candidates)
        subject = self._subject(principal)
        if subject is None:
            return []
        try:
            result = self._post(
                "/api/v1/authorization/filter",
                subject=subject,
                payload={"resource_type": mapped, "resource_ids": candidates, "action": "use"},
            )
            allowed = result.get("allowed_resource_ids")
            if not isinstance(allowed, list) or any(not isinstance(item, str) for item in allowed):
                raise RuntimeError("invalid policy filter response")
            return allowed
        except RuntimeError:
            return []
