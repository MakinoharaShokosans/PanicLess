"""全局知识入口与缺凭据降级；真实本地存储，模型仅用显式 mock transport。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import httpx
import pytest

from agent_runtime.bootstrap import Application
from agent_runtime.core import new_state
from baseagent_config import ConfigError, load_settings
from baseagent_config.retrieval import RetrievalConfigSource
from tools.retrieval.cli import parser
from tools.retrieval.client import EmbeddingClient, RerankClient
from tools.retrieval.errors import RetrievalConfigurationError
from tools.retrieval.service import RetrievalService
from tools.retrieval.tool import KnowledgeSearchTool

TOKEN = "PANICLESS_GLOBAL_TEST_TOKEN"


@pytest.fixture
def config_file(tmp_path, monkeypatch, config_path):
    monkeypatch.setenv("BASEAGENT_HOME", str(tmp_path / "home"))
    monkeypatch.delenv(TOKEN, raising=False)
    manuals = tmp_path / "manuals"
    manuals.mkdir()
    (manuals / "disk.md").write_text(
        "# 磁盘排查\n\n磁盘空间不足时先检查 df 和 inode。\n\n"
        "检查日志文件占用，确认文件用途后再处理。\n",
        encoding="utf-8",
    )
    text = config_path.read_text(encoding="utf-8")
    text = text.replace('base_url = "https://api.deepseek.com"', 'base_url = "http://127.0.0.1:9"')
    text = text.replace("knowledge_enabled = false", "knowledge_enabled = true")
    for kind in ("embedding", "rerank"):
        text = text.replace(
            f"[retrieval.{kind}]\nenabled = false", f"[retrieval.{kind}]\nenabled = true"
        )
    text = text.replace('api_key_env = "PANICLESS_RAG_API_KEY"', f'api_key_env = "{TOKEN}"')
    text = text.replace(
        "sources = []", 'sources = [{source_id="ops", root_path=' + json.dumps(str(manuals)) + "}]"
    )
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    yield path
    shutil.rmtree(manuals)
    if (tmp_path / "home").exists():
        shutil.rmtree(tmp_path / "home")
    path.unlink()


@pytest.mark.unit
def test_missing_rag_key_is_warning_and_config_remains_reloadable(config_file):
    loaded = load_settings(config_file, create_home=False)
    assert len([w for w in loaded.warnings if TOKEN in w]) == 2
    source = RetrievalConfigSource(config_file)
    assert source().embedding.enabled
    text = config_file.read_text()
    config_file.write_text(text.replace("default_top_k = 5", "default_top_k = 2"))
    assert source().default_top_k == 2
    config_file.write_text(text.replace("default_top_k = 5", "default_top_k = 0"))
    with pytest.raises(ConfigError):
        source()


@pytest.mark.unit
def test_management_cli_no_longer_requires_a_workspace():
    for args in (
        ["status"],
        ["rebuild"],
        ["index", "--source-id", "ops"],
        ["search", "--query", "磁盘"],
    ):
        assert not hasattr(parser().parse_args(args), "workspace_id")


@pytest.mark.integration
async def test_application_starts_with_enabled_rag_without_key(config_file):
    loaded = load_settings(config_file)
    app = await Application.create(loaded)
    try:
        assert app.factory.retrieval.current_config().knowledge_enabled
        assert app.factory.retrieval._service is None
    finally:
        await app.aclose()


@pytest.mark.integration
async def test_global_index_agent_search_missing_key_and_cleanup(config_file, monkeypatch):
    calls = []

    def reply(request):
        body = json.loads(request.content)
        calls.append(body)
        if "input" in body:
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"index": i, "embedding": [1.0, 0.5, 0.25]}
                        for i in range(len(body["input"]))
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": i, "relevance_score": 1.0 / (i + 1)}
                    for i in range(len(body["documents"]))
                ]
            },
        )

    def make_service(config, **kwargs):
        return RetrievalService(
            config,
            **kwargs,
            embedding_factory=lambda c: EmbeddingClient(c, transport=httpx.MockTransport(reply)),
            rerank_factory=lambda c: RerankClient(c, transport=httpx.MockTransport(reply)),
        )

    source = RetrievalConfigSource(config_file)
    monkeypatch.setenv(TOKEN, "explicit-test-only-token")
    async with make_service(source(), control_path=source.control_path) as service:
        indexed = await service.index("ops")
        assert indexed["documents"] == 1
        original = await service.search("磁盘")
        assert original.hits and original.context_blocks
        doc_id = original.hits[0].document.doc_id

    # Agent 不传会话工作区；两个入口读同一个已持久化的全局索引。
    monkeypatch.setattr("agent_runtime.runtime.retrieval.RetrievalService", make_service)
    app = await Application.create(load_settings(config_file))
    runtime = app.factory.retrieval
    try:
        for number in (1, 2):
            workspace, session = f"workspace-{number}", f"session-{number}"
            root = source.home / workspace
            root.mkdir()
            await app.memory.create_workspace(workspace, workspace, str(root))
            await app.memory.create_session(session, workspace)
            assembled = await app.factory._assemble(
                new_state(
                    workspace_id=workspace,
                    workspace_path=str(root),
                    session_id=session,
                    task_id=f"task-{number}",
                    task_goal="磁盘排查",
                    permission_level="read_only",
                    canary_token="test-canary",
                )
            )
            tool = assembled.registry.get("knowledge_search")
            assert tool is not None
            result = await tool.invoke({"query": "磁盘", "source_ids": ["ops"]})
            assert result.ok and result.meta["status"] == "ok"
            assert result.meta["hits"][0]["doc_id"] == doc_id
        before = len(calls)
        monkeypatch.delenv(TOKEN)
        degraded = await KnowledgeSearchTool(runtime).invoke({"query": "磁盘"})
        assert degraded.ok and degraded.meta["status"] == "degraded"
        assert degraded.meta["retrieval_mode"] == "bm25"
        assert degraded.meta["degraded_reasons"] == [
            "vector:RetrievalConfigurationError",
            "rerank:RetrievalConfigurationError",
        ]
        assert len(calls) == before
        assert runtime.last_reload_error is None
        # 缺 Key 不会绕过新资料的真实向量化需求，也不会破坏已有索引。
        (Path(source().sources[0].root_path) / "new.txt").write_text("新资料需要向量化")
        with pytest.raises(RetrievalConfigurationError):
            await runtime._service.index("ops")
        assert (await runtime._service.status())["documents"] == 1
    finally:
        await app.aclose()

    # 关闭后可重新取得本地锁，精确删除本测试文档，不 reset 整库。
    async with make_service(source(), control_path=source.control_path) as service:
        await service.index(delete_id=doc_id)
        assert (await service.status())["documents"] == 0
        assert not (await service.search("磁盘")).hits
