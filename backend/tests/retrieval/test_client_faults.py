"""模型客户端故障注入与协议边界；全部离线，不作为真实接口成功证据。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from baseagent_config.models import EmbeddingConfig, RerankConfig
from tools.retrieval.adapters import RankedItem
from tools.retrieval.client import EmbeddingClient, RerankClient
from tools.retrieval.errors import (
    RetrievalAuthenticationError,
    RetrievalConfigurationError,
    RetrievalInvalidResponseError,
    RetrievalNotConfiguredError,
    RetrievalRateLimitError,
    RetrievalServiceError,
    RetrievalTimeoutError,
)

pytestmark = pytest.mark.unit

_KINDS = ("embedding", "rerank")
_AUTH_ENV = "PANICLESS_OFFLINE_FAULT_TEST_TOKEN"
_SENTINEL = "fictional-fault-test-secret"


def _client(
    kind: str, transport: httpx.AsyncBaseTransport, **overrides: Any
) -> EmbeddingClient | RerankClient:
    data: dict[str, Any] = {
        "enabled": True,
        "endpoint_url": f"https://offline.invalid/v1/{kind}",
        "model": "offline-fixture-model",
        "api_key_env": "",
        "timeout_sec": 1.0,
        **overrides,
    }
    if kind == "embedding":
        return EmbeddingClient(
            EmbeddingConfig(protocol="openai_embeddings", **data), transport=transport
        )
    return RerankClient(RerankConfig(protocol="indexed_rerank", **data), transport=transport)


async def _invoke(client: EmbeddingClient | RerankClient) -> object:
    if isinstance(client, EmbeddingClient):
        return await client.embed(["测试资料"])
    return await client.rerank("测试问题", ["测试资料"], 1)


@pytest.mark.parametrize("kind", _KINDS)
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, RetrievalAuthenticationError),
        (403, RetrievalAuthenticationError),
        (429, RetrievalRateLimitError),
        (500, RetrievalServiceError),
        (302, RetrievalServiceError),
    ],
)
async def test_http_failures_are_typed_sanitized_and_not_retried(
    kind: str, status: int, expected: type[Exception], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[httpx.Request] = []
    monkeypatch.setenv(_AUTH_ENV, _SENTINEL)

    def reply(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["Authorization"] == f"Bearer {_SENTINEL}"
        return httpx.Response(
            status, text=f"sensitive body: {_SENTINEL}", headers={"Location": "/elsewhere"}
        )

    async with _client(kind, httpx.MockTransport(reply), api_key_env=_AUTH_ENV) as client:
        with pytest.raises(expected) as caught:
            await _invoke(client)
    assert len(calls) == 1
    assert _SENTINEL not in str(caught.value)
    assert "sensitive body" not in str(caught.value)


@pytest.mark.parametrize("kind", _KINDS)
@pytest.mark.parametrize("error", [httpx.ReadTimeout, httpx.ConnectTimeout, httpx.ConnectError])
async def test_transport_failures_are_typed_and_sanitized(
    kind: str, error: type[httpx.RequestError]
) -> None:
    calls = 0

    def fail(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise error(_SENTINEL, request=request)

    expected = (
        RetrievalTimeoutError
        if issubclass(error, httpx.TimeoutException)
        else (RetrievalServiceError)
    )
    async with _client(kind, httpx.MockTransport(fail)) as client:
        with pytest.raises(expected) as caught:
            await _invoke(client)
    assert calls == 1
    assert _SENTINEL not in str(caught.value)


@pytest.mark.parametrize("kind", _KINDS)
async def test_total_request_deadline_applies_to_async_transport(kind: str) -> None:
    async def delayed(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.1)
        return httpx.Response(200, json={})

    async with _client(kind, httpx.MockTransport(delayed), timeout_sec=0.01) as client:
        with pytest.raises(RetrievalTimeoutError):
            await _invoke(client)


@pytest.mark.parametrize("kind", _KINDS)
async def test_task_cancellation_is_not_converted_to_service_failure(kind: str) -> None:
    def cancelled(_request: httpx.Request) -> httpx.Response:
        raise asyncio.CancelledError

    async with _client(kind, httpx.MockTransport(cancelled)) as client:
        with pytest.raises(asyncio.CancelledError):
            await _invoke(client)


@pytest.mark.parametrize("kind", _KINDS)
async def test_success_status_with_invalid_json_is_not_empty_success(kind: str) -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(200, text="not JSON"))
    async with _client(kind, transport) as client:
        with pytest.raises(RetrievalInvalidResponseError, match="JSON"):
            await _invoke(client)


@pytest.mark.parametrize("kind", _KINDS)
@pytest.mark.parametrize("mode", ["disabled", "missing_auth", "wrong_protocol"])
async def test_configuration_errors_fail_before_http(
    kind: str, mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def unexpected(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    monkeypatch.delenv(_AUTH_ENV, raising=False)
    overrides: dict[str, Any] = {}
    if mode == "disabled":
        overrides["enabled"] = False
    elif mode == "missing_auth":
        overrides["api_key_env"] = _AUTH_ENV
    client = _client(kind, httpx.MockTransport(unexpected), **overrides)
    if mode == "wrong_protocol":
        client._config = client._config.model_copy(update={"protocol": "wrong-protocol"})
    expected = RetrievalNotConfiguredError if mode == "disabled" else RetrievalConfigurationError
    async with client:
        with pytest.raises(expected):
            await _invoke(client)
    assert calls == 0


@pytest.mark.parametrize("kind", _KINDS)
async def test_empty_inputs_skip_http_even_when_disabled(kind: str) -> None:
    calls = 0

    def unexpected(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    async with _client(kind, httpx.MockTransport(unexpected), enabled=False) as client:
        if isinstance(client, EmbeddingClient):
            assert await client.embed([]) == []
        else:
            assert await client.rerank("问题", [], 0) == []
    assert calls == 0


async def test_embedding_preserves_input_order_from_response_indexes() -> None:
    def reply(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {
            "model": "offline-fixture-model",
            "input": ["第一篇", "第二篇"],
        }
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0.3, 0.4]},
                    {"index": 0, "embedding": [0.1, 0.2]},
                ]
            },
        )

    async with _client("embedding", httpx.MockTransport(reply)) as client:
        assert isinstance(client, EmbeddingClient)
        assert await client.embed(["第一篇", "第二篇"]) == [[0.1, 0.2], [0.3, 0.4]]


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [{"index": 0, "embedding": [0.1]}],
        [{"index": 0, "embedding": [0.1]}, {"index": 0, "embedding": [0.2]}],
        [{"index": 0, "embedding": [0.1]}, {"index": 2, "embedding": [0.2]}],
        [{"index": -1, "embedding": [0.1]}, {"index": 1, "embedding": [0.2]}],
        [{"index": True, "embedding": [0.1]}, {"index": 1, "embedding": [0.2]}],
        [{"index": 0, "embedding": []}, {"index": 1, "embedding": [0.2]}],
        [{"index": 0, "embedding": [0.1]}, {"index": 1, "embedding": [0.2, 0.3]}],
        [{"index": 0, "embedding": ["0.1"]}, {"index": 1, "embedding": [0.2]}],
        [{"index": 0, "embedding": [True]}, {"index": 1, "embedding": [0.2]}],
        [{"index": 0, "embedding": [float("nan")]}, {"index": 1, "embedding": [0.2]}],
        [{"index": 0, "embedding": [float("inf")]}, {"index": 1, "embedding": [0.2]}],
    ],
    ids=[
        "empty",
        "missing",
        "duplicate",
        "out-of-range",
        "negative",
        "boolean-index",
        "empty-vector",
        "mixed-dimensions",
        "string-number",
        "boolean-number",
        "nan",
        "inf",
    ],
)
async def test_embedding_rejects_incomplete_or_invalid_vectors(rows: list[dict[str, Any]]) -> None:
    payload = json.dumps({"data": rows})
    transport = httpx.MockTransport(lambda _: httpx.Response(200, content=payload))
    async with _client("embedding", transport) as client:
        assert isinstance(client, EmbeddingClient)
        with pytest.raises(RetrievalInvalidResponseError):
            await client.embed(["第一篇", "第二篇"])


async def test_rerank_maps_indexes_sorts_scores_and_caps_top_n() -> None:
    def reply(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {
            "model": "offline-fixture-model",
            "query": "问题",
            "documents": ["资料0", "资料1", "资料2"],
            "top_n": 3,
        }
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 2, "relevance_score": 0.7},
                    {"index": 0, "relevance_score": 0.1},
                    {"index": 1, "relevance_score": 0.7},
                ]
            },
        )

    async with _client("rerank", httpx.MockTransport(reply)) as client:
        assert isinstance(client, RerankClient)
        assert await client.rerank("问题", ["资料0", "资料1", "资料2"], 9) == [
            RankedItem(1, 0.7),
            RankedItem(2, 0.7),
            RankedItem(0, 0.1),
        ]


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [{"index": 0, "relevance_score": 0.1}],
        [{"index": 0, "relevance_score": 0.1}, {"index": 0, "relevance_score": 0.2}],
        [{"index": 0, "relevance_score": 0.1}, {"index": 2, "relevance_score": 0.2}],
        [{"index": -1, "relevance_score": 0.1}, {"index": 1, "relevance_score": 0.2}],
        [{"index": True, "relevance_score": 0.1}, {"index": 1, "relevance_score": 0.2}],
        [{"index": 0}, {"index": 1, "relevance_score": 0.2}],
        [{"index": 0, "relevance_score": "0.1"}, {"index": 1, "relevance_score": 0.2}],
        [{"index": 0, "relevance_score": True}, {"index": 1, "relevance_score": 0.2}],
        [{"index": 0, "relevance_score": float("nan")}, {"index": 1, "relevance_score": 0.2}],
        [{"index": 0, "relevance_score": float("inf")}, {"index": 1, "relevance_score": 0.2}],
    ],
    ids=[
        "empty",
        "missing",
        "duplicate",
        "out-of-range",
        "negative",
        "boolean-index",
        "missing-score",
        "string-score",
        "boolean-score",
        "nan",
        "inf",
    ],
)
async def test_rerank_rejects_incomplete_or_invalid_mapping(rows: list[dict[str, Any]]) -> None:
    payload = json.dumps({"results": rows})
    transport = httpx.MockTransport(lambda _: httpx.Response(200, content=payload))
    async with _client("rerank", transport) as client:
        assert isinstance(client, RerankClient)
        with pytest.raises(RetrievalInvalidResponseError):
            await client.rerank("问题", ["资料0", "资料1"], 2)


@pytest.mark.parametrize("top_n", [0, -1, True, 1.5])
async def test_rerank_rejects_invalid_top_n_before_http(top_n: Any) -> None:
    calls = 0

    def unexpected(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    async with _client("rerank", httpx.MockTransport(unexpected)) as client:
        assert isinstance(client, RerankClient)
        with pytest.raises(ValueError, match="top_n"):
            await client.rerank("问题", ["资料"], top_n)
    assert calls == 0
