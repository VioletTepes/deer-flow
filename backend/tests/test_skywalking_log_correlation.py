import json
import logging

from deerflow.logging_config import JsonTraceFormatter, TraceContextFilter
from deerflow.trace_context import request_trace_context


def test_json_keeps_correlation_separate_from_missing_agent_trace():
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "hello", (), None)
    with request_trace_context("request-1"):
        TraceContextFilter().filter(record)
    payload = json.loads(JsonTraceFormatter().format(record))
    assert payload["correlation_id"] == "request-1"
    assert payload["trace_id"] is None


def test_json_captures_real_agent_trace_at_emission(monkeypatch):
    import deerflow.logging_config as module

    monkeypatch.setattr(
        module,
        "skywalking_trace_fields",
        lambda: {
            "trace_id": "actual-oap-trace",
            "span_id": 3,
            "segment_id": "segment-1",
        },
    )
    record = logging.LogRecord("test", logging.WARNING, __file__, 1, "hello", (), None)
    with request_trace_context("request-2"):
        TraceContextFilter().filter(record)
    payload = json.loads(JsonTraceFormatter().format(record))
    assert payload["trace_id"] == "actual-oap-trace"
    assert payload["correlation_id"] == "request-2"
    assert payload["span_id"] == 3


def test_matrixmed_headers_do_not_replace_signing_nonce():
    from deerflow.community.matrixmed.authorization import MatrixMedAuthorizationProvider
    from deerflow.community.matrixmed.correlation import correlation_headers

    with request_trace_context("request-flow"):
        assert correlation_headers() == {"X-Correlation-ID": "request-flow"}
        provider = MatrixMedAuthorizationProvider(policy_api_url="http://policy", context_shared_secret="test-secret")
        headers = provider._headers(method="POST", path="/check", subject="user")
    assert headers["X-Correlation-ID"] == "request-flow"
    assert headers["X-MatrixMed-Request-Id"] != "request-flow"
