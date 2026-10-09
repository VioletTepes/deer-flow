"""Diagnostic headers only; the signed MatrixMed nonce remains independent."""

from deerflow.trace_context import get_current_trace_id


def correlation_headers() -> dict[str, str]:
    correlation_id = get_current_trace_id()
    return {"X-Correlation-ID": correlation_id} if correlation_id else {}
