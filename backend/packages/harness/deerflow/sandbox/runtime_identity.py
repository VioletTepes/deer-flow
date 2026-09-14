"""Request-scoped external identity for sandbox providers.

DeerFlow's ``user_id`` is its local account id.  A provider that delegates
authorization to MatrixMed Identity must instead use the OIDC subject carried
in the authenticated runtime context.  Keeping it in a ContextVar preserves
the existing provider interface and never writes identity into a thread's
persisted state.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar

_sandbox_subject: ContextVar[str | None] = ContextVar("sandbox_subject", default=None)
_sandbox_project_key: ContextVar[str | None] = ContextVar("sandbox_project_key", default=None)
_PROJECT_KEY = re.compile(r"[a-z][a-z0-9-]{0,62}\Z")


def sandbox_subject() -> str | None:
    """Return the OIDC subject bound to the current sandbox operation."""
    return _sandbox_subject.get()


def sandbox_project_key() -> str | None:
    """Return the trusted research-project key bound to this operation."""
    return _sandbox_project_key.get()


@contextmanager
def sandbox_identity_scope(context: Mapping[str, object] | None) -> Iterator[None]:
    """Bind verified OIDC identity and research-project context for a provider.

    Providers fail closed when the subject is absent.  This is intentional:
    falling back to a DeerFlow-local UUID would create storage identities that
    Identity cannot authorize.
    """
    subject = (context or {}).get("oauth_id")
    if subject is not None and not isinstance(subject, str):
        subject = None
    project_key = (context or {}).get("matrixmed_project_key")
    if not isinstance(project_key, str) or not _PROJECT_KEY.fullmatch(project_key):
        project_key = None
    subject_token = _sandbox_subject.set(subject)
    project_token = _sandbox_project_key.set(project_key)
    try:
        yield
    finally:
        _sandbox_project_key.reset(project_token)
        _sandbox_subject.reset(subject_token)
