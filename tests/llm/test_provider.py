import json

import httpx
import pytest

from opslens.llm.context import can_repair
from opslens.llm.provider import OllamaConfig, OllamaProvider


def provider(handler):
    return OllamaProvider(OllamaConfig(model="test:27b"), httpx.MockTransport(handler))


def test_bounded_request_and_thinking_discarded():
    def handler(request):
        body = json.loads(request.content)
        assert body["think"] is False and body["stream"] is False
        assert body["options"]["num_ctx"] == 4096 and body["options"]["num_predict"] == 1024
        assert body["options"]["temperature"] == 0 and body["keep_alive"] == "2m"
        return httpx.Response(
            200,
            json={
                "done": True,
                "done_reason": "stop",
                "message": {
                    "content": json.dumps({"sql": "SELECT 1"}),
                    "thinking": "private reasoning",
                },
                "prompt_eval_count": 20,
                "eval_count": 8,
            },
        )

    result = provider(handler).generate("test", 1)
    assert result.candidate.sql == "SELECT 1"
    assert result.metadata.prompt_tokens == 20
    assert "private reasoning" not in result.model_dump_json()
    assert result.metadata.candidate_sql_hash


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        '```json\n{"sql":"SELECT 1"}\n```',
        '{"sql":""}',
        '{"sql":"SELECT 1", "verified":true}',
        '{"sql":123}',
        "[]",
    ],
)
def test_malformed_output_fails_closed(content):
    result = provider(
        lambda r: httpx.Response(
            200, json={"done": True, "done_reason": "stop", "message": {"content": content}}
        )
    ).generate("x", 1)
    assert result.candidate is None and result.metadata.status == "MALFORMED_OUTPUT"


@pytest.mark.parametrize(
    "error,code",
    [(httpx.ReadTimeout, "PROVIDER_TIMEOUT"), (httpx.ConnectError, "PROVIDER_UNAVAILABLE")],
)
def test_transport_failure_redacted(error, code):
    def handler(request):
        raise error("secret password /home/private", request=request)

    result = provider(handler).generate("x", 1)
    assert result.metadata.status == code
    assert "secret" not in result.model_dump_json()


def test_http_and_incomplete_output():
    assert (
        provider(lambda r: httpx.Response(500, text="secret")).generate("x", 1).metadata.status
        == "PROVIDER_ERROR"
    )
    r = provider(
        lambda r: httpx.Response(
            200,
            json={
                "done": True,
                "done_reason": "length",
                "message": {"content": '{"sql":"SELECT 1"}'},
            },
        )
    ).generate("x", 1)
    assert r.candidate is None


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "http://127.0.0.1@evil.com",
        "http://localhost/?secret=1",
        "http://localhost/other",
        "http://user:pass@localhost",
    ],
)
def test_local_endpoint_only(url):
    with pytest.raises(ValueError):
        OllamaConfig(model="test", base_url=url)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"num_ctx": 100000},
        {"num_predict": 100000},
        {"timeout_seconds": 9999.0},
        {"max_repairs": 2},
        {"model": "secret /home/private"},
    ],
)
def test_config_limits(kwargs):
    with pytest.raises(ValueError):
        OllamaConfig(**({"model": "test"} | kwargs))


def test_context_budget_prevents_request():
    def handler(r):
        pytest.fail("Oversized prompt reached provider")

    assert provider(handler).generate("x" * 18001, 1).metadata.status == "CONTEXT_BUDGET_EXCEEDED"


@pytest.mark.parametrize(
    "reasons,expected",
    [
        (["UNKNOWN_COLUMN"], True),
        (["PARSE_ERROR"], True),
        ([], False),
        (["UNKNOWN_COLUMN", "COLUMN_DENIED"], False),
        (["TABLE_NOT_ALLOWED"], False),
        (["STATEMENT_NOT_ALLOWED"], False),
        (["TABLE_FUNCTION_NOT_ALLOWED"], False),
        (["FUNCTION_NOT_ALLOWED"], False),
        (["METRIC_RESULT_MISMATCH"], False),
    ],
)
def test_repair_allowlist(reasons, expected):
    assert can_repair(reasons) is expected
