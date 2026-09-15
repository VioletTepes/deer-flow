"""OpenAI-compatible DeerFlow model that signs each request for Policy API.

The transport intentionally removes LangChain's placeholder Bearer value.
Identity is provided by a short-lived HMAC context derived from the authenticated
DeerFlow user, never from a user token stored in a Cookie or agent state.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import httpx
from langchain_openai import ChatOpenAI

from deerflow.runtime.user_context import get_current_user


class _MatrixMedContextAuth(httpx.Auth):
    requires_request_body = True

    def __init__(self, secret: str, ttl_seconds: int) -> None:
        self._secret = secret.encode()
        self._ttl = ttl_seconds

    def _authorize(self, request: httpx.Request) -> None:
        user = get_current_user()
        subject = getattr(user, "oauth_id", None)
        if not isinstance(subject, str) or not subject:
            raise RuntimeError("MatrixMed Agentgateway requires an authenticated OIDC subject")
        expires_at = (datetime.now(UTC) + timedelta(seconds=self._ttl)).isoformat()
        request_id = str(uuid.uuid4())
        payload = {
            "expires_at": expires_at,
            "method": request.method.upper(),
            "path": request.url.path,
            "request_id": request_id,
            "subject": subject,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        request.headers.pop("Authorization", None)
        request.headers["X-MatrixMed-Subject"] = subject
        request.headers["X-MatrixMed-Request-Id"] = request_id
        request.headers["X-MatrixMed-Expires-At"] = expires_at
        request.headers["X-MatrixMed-Context-Signature"] = hmac.new(self._secret, canonical, hashlib.sha256).hexdigest()

    def auth_flow(self, request: httpx.Request) -> Iterator[httpx.Request]:
        self._authorize(request)
        yield request

    async def async_auth_flow(self, request: httpx.Request):
        self._authorize(request)
        yield request


class MatrixMedAgentgatewayChatModel(ChatOpenAI):
    """ChatOpenAI transport for the MatrixMed Policy API.

    ``matrixmed_context_shared_secret`` accepts either a literal development
    value or DeerFlow's conventional ``$ENV_VAR`` reference. It is consumed by
    this provider and never sent to Agentgateway.
    """

    def __init__(self, *args, matrixmed_context_shared_secret: str | None = None, matrixmed_context_ttl_seconds: int = 240, **kwargs) -> None:
        raw_secret = matrixmed_context_shared_secret or ""
        secret = os.environ.get(raw_secret[1:], "") if raw_secret.startswith("$") else raw_secret
        if not secret:
            raise ValueError("matrixmed_context_shared_secret is required")
        ttl = int(matrixmed_context_ttl_seconds)
        if ttl <= 0 or ttl > 300:
            raise ValueError("matrixmed_context_ttl_seconds must be between 1 and 300")
        auth = _MatrixMedContextAuth(secret, ttl)
        # ChatOpenAI requires an api key even though Policy API removes this
        # placeholder before network I/O. Supplying an internal constant avoids
        # an operator accidentally reusing an upstream provider key here.
        kwargs["api_key"] = "matrixmed-policy-context"
        kwargs.setdefault("http_client", httpx.Client(auth=auth))
        kwargs.setdefault("http_async_client", httpx.AsyncClient(auth=auth))
        super().__init__(*args, **kwargs)
