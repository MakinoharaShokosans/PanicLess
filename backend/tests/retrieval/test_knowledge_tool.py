"""知识工具的离线入口契约；不创建索引、不请求模型。"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from baseagent_config.models import KnowledgeSource, RetrievalConfig
from tools.retrieval.records import Chunk, ContextBlock, Document, Hit, SearchResult
from tools.retrieval.tool import KnowledgeSearchTool

pytestmark = pytest.mark.unit


class Backend:
    def __init__(self) -> None:
        self.config = RetrievalConfig(
            knowledge_enabled=True,
            sources=(
                KnowledgeSource(source_id="ops", root_path="/ops"),
                KnowledgeSource(source_id="network", root_path="/network"),
            ),
        )
        self.refresh = AsyncMock()
        self.search = AsyncMock(return_value=SearchResult("磁盘", "config", "index", "hybrid"))

    def current_config(self) -> RetrievalConfig:
        return self.config


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"query": " "},
        {"query": 3},
        {"query": "磁盘", "workspace_id": "other"},
        {"query": "磁盘", "model": "override"},
        {"query": "磁盘", "top_k": True},
        {"query": "磁盘", "top_k": 0},
        {"query": "磁盘", "top_k": 11},
        {"query": "磁盘", "source_ids": "ops"},
        {"query": "磁盘", "source_ids": [""]},
        {"query": "磁盘", "source_ids": ["unknown"]},
    ],
)
async def test_invalid_arguments_never_reach_search(args: dict) -> None:
    backend = Backend()
    result = await KnowledgeSearchTool(backend).invoke(args)
    assert not result.ok and result.meta["status"] == "invalid_arguments"
    assert result.trust == "untrusted"
    backend.search.assert_not_awaited()


@pytest.mark.parametrize("selected, expected", [(None, None), ([], ()), (["ops", "ops"], ("ops",))])
async def test_all_sources_are_visible_and_selection_is_preserved(selected, expected) -> None:
    backend = Backend()
    tool = KnowledgeSearchTool(backend)
    assert tool.parameters["properties"]["source_ids"]["items"]["enum"] == ["network", "ops"]
    args = {"query": " 磁盘 "}
    if selected is not None:
        args["source_ids"] = selected
    result = await tool.invoke(args)
    assert result.ok and result.meta["status"] == "no_results"
    backend.search.assert_awaited_once_with("磁盘", top_k=None, source_ids=expected)


async def test_degraded_result_keeps_citations_and_renders_merged_body_once() -> None:
    backend = Backend()
    text = "唯一扩展正文：检查磁盘和 inode。"
    document = Document(
        "doc", "mine", "ops", "disk.md", "/ops", "磁盘手册", "hash", "rev", text, (), 0
    )
    chunks = [
        Chunk(f"chunk-{i}", "doc", "rev", i, "section", ("磁盘",), 1, 1, 0, len(text), text, "hash")
        for i in range(2)
    ]
    block = ContextBlock(
        "block",
        "doc",
        "rev",
        "ops",
        "disk.md",
        [c.chunk_id for c in chunks],
        [c.chunk_id for c in chunks],
        0,
        len(text),
        1,
        1,
        text,
        "adjacent",
    )
    backend.search.return_value = SearchResult(
        "磁盘",
        "config",
        "index",
        "bm25",
        degraded_reasons=["vector:RetrievalConfigurationError", "rerank_disabled"],
        hits=[
            Hit(c, document, 0.01, bm25_rank=i + 1, context_block_id="block")
            for i, c in enumerate(chunks)
        ],
        context_blocks=[block],
    )
    result = await KnowledgeSearchTool(backend).invoke({"query": "磁盘"})
    assert result.ok and result.meta["status"] == "degraded" and result.trust == "untrusted"
    assert result.content.count(text) == 1
    assert "磁盘手册" in result.content and '"line_start": 1' in result.content
    assert {hit["chunk_id"] for hit in result.meta["hits"]} == {"chunk-0", "chunk-1"}
    assert result.meta["context_blocks"][0]["revision"] == "rev"


async def test_failure_is_sanitized_and_not_reported_as_no_results() -> None:
    backend = Backend()
    backend.search.side_effect = RuntimeError("secret-token-in-exception")
    result = await KnowledgeSearchTool(backend).invoke({"query": "磁盘"})
    assert not result.ok and result.meta["status"] == "failed"
    assert result.meta["error_type"] == "RuntimeError"
    assert "secret-token" not in result.model_dump_json()


async def test_cancel_propagates() -> None:
    backend = Backend()
    backend.search.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await KnowledgeSearchTool(backend).invoke({"query": "磁盘"})


async def test_refresh_changes_limits_and_disable_state() -> None:
    backend = Backend()
    tool = KnowledgeSearchTool(backend)
    backend.config = backend.config.model_copy(
        update={"default_top_k": 1, "max_top_k": 2, "max_query_chars": 2}
    )
    assert tool.parameters["properties"]["top_k"]["maximum"] == 2
    assert tool.parameters["properties"]["top_k"]["default"] == 1
    result = await tool.invoke({"query": "三个字"})
    assert result.meta["status"] == "invalid_arguments"
    backend.refresh.side_effect = lambda: setattr(
        backend, "config", backend.config.model_copy(update={"knowledge_enabled": False})
    )
    result = await tool.invoke({"query": "磁盘"})
    assert not tool.enabled and result.meta["status"] == "disabled"
    backend.search.assert_not_awaited()


async def test_not_indexed_is_distinct_from_empty_results() -> None:
    backend = Backend()
    backend.search.return_value = SearchResult("磁盘", "config", None, "not_indexed")
    result = await KnowledgeSearchTool(backend).invoke({"query": "磁盘"})
    assert result.ok and result.meta["status"] == "not_indexed"


@pytest.mark.parametrize("source_ids", [["network"], ["ops", "network"]])
async def test_configured_sources_are_globally_selectable(source_ids) -> None:
    backend = Backend()
    result = await KnowledgeSearchTool(backend).invoke({"query": "磁盘", "source_ids": source_ids})
    assert result.ok
    backend.search.assert_awaited_once_with("磁盘", top_k=None, source_ids=tuple(source_ids))


async def test_removing_source_updates_schema_and_rejects_old_selection() -> None:
    backend = Backend()
    tool = KnowledgeSearchTool(backend)
    assert tool.parameters["properties"]["source_ids"]["items"]["enum"] == ["network", "ops"]

    def remove_sources():
        backend.config = backend.config.model_copy(update={"sources": ()})

    backend.refresh.side_effect = remove_sources
    result = await tool.invoke({"query": "磁盘", "source_ids": ["ops"]})
    assert not result.ok and result.meta["status"] == "invalid_arguments"
    assert tool.parameters["properties"]["source_ids"]["maxItems"] == 0
    backend.search.assert_not_awaited()
