"""Isolated management/live checks and explicitly separated injected failures."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import support
from support import HOME, ROOT, Calls, check_citations, config_copy, save

from baseagent_config.retrieval import RetrievalConfigSource
from tools.retrieval.errors import RetrievalServiceError
from tools.retrieval.search import KeywordIndex
from tools.retrieval.service import RetrievalService

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _temporary_offline_outputs(monkeypatch, tmp_path, request):
    """离线回归的数据库与结果随 pytest 临时目录管理，不遗留项目产物。"""
    if request.node.get_closest_marker("real_llm") is None:
        monkeypatch.setattr(support, "RESULTS", tmp_path / "results")
        monkeypatch.setattr(f"{__name__}.HOME", tmp_path)


@asynccontextmanager
async def isolated(monkeypatch, *, live=False):
    parent = HOME / "isolated"
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="verification-", dir=parent) as directory:
        root = Path(directory)
        sources = []
        paths = []
        for name, prefix, tag in (
            ("a", "03_", "disk"),
            ("b", "09_", "tls"),
            ("c", "02_", "filesystem"),
        ):
            location = root / name
            location.mkdir()
            original = next((ROOT / "docs/运维手册").glob(prefix + "*.md"))
            destination = location / original.name
            shutil.copyfile(original, destination)
            paths.append(destination)
            sources.append(
                dict(
                    source_id=name,
                    root_path=str(location),
                    service=tag,
                    tags=[tag],
                    max_files=10,
                )
            )
        config = root / "copy.toml"
        home = root / "home"
        text = config_copy(config, home, sources)
        # BASEAGENT_HOME takes precedence over TOML; never open the retained manual index here.
        monkeypatch.setenv("BASEAGENT_HOME", str(home))
        if not live:
            text = text.replace(
                'api_key_env = "PANICLESS_RAG_TEST_API_KEY"',
                'api_key_env = "PANICLESS_OFFLINE_ONLY"',
            )
            monkeypatch.setenv("PANICLESS_OFFLINE_ONLY", "fictional-never-sent-to-network")
            config.write_text(text)
        source = RetrievalConfigSource(config)
        assert source.home == home.resolve()
        calls = Calls(live=live)

        def create():
            return RetrievalService(
                source(),
                control_path=source.control_path,
                reload_config=source,
                embedding_factory=calls.embedding,
                rerank_factory=calls.rerank,
            )

        yield root, paths, config, text, source, calls, create
    assert not await asyncio.to_thread(root.exists)


@pytest.mark.real_llm
@pytest.mark.skipif(os.environ.get("PANICLESS_RAG_LIVE") != "1", reason="explicit live run only")
async def test_live_management(monkeypatch):
    evidence: dict[str, Any] = {"models": "real", "steps": {}}

    def record(name, value):
        evidence["steps"][name] = value
        save("management_real.json", evidence)
        print(name, flush=True)

    async with isolated(monkeypatch, live=True) as setup:
        root, paths, config_path, original, _source, calls, create = setup
        async with create() as svc:
            for source_id in ("a", "b", "c"):
                record("index_" + source_id, await svc.index(source_id))
            filter_cases: list[tuple[dict[str, Any], str]] = [
                ({"source_id": "a"}, "a"),
                ({"service": "tls"}, "b"),
                ({"tags": ("disk",)}, "a"),
                ({"source_ids": ("c",)}, "c"),
            ]
            for filters, expected_source in filter_cases:
                result = await svc.search("磁盘空间不足 ENOSPC 证书过期", **filters)
                assert result.hits and not result.degraded_reasons
                assert all(h.document.source_id == expected_source for h in result.hits)
                record("filter_" + str(filters), result.to_dict())

            before = await svc.status()
            hot = original.replace("default_top_k = 5", "default_top_k = 1")
            hot = hot.replace('mode = "adjacent"', 'mode = "section"')
            config_path.write_text(hot)
            result = await svc.search("日志文件删了空间未释放", source_id="a")
            assert len(result.hits) == 1 and not result.degraded_reasons
            assert result.index_generation == before["index_generation"]
            assert result.config_revision != before["active_config_revision"]
            assert all(b.expansion_mode == "section" for b in result.context_blocks)
            async with svc.snapshot() as request:
                citations = check_citations(result, request.corpus, request.config, disk=True)
            record("hot_top_k_and_section", {"result": result.to_dict(), "checks": citations})
            config_path.write_text(hot + "\n[broken")
            status = await svc.status()
            assert status["last_reload_error"] and svc.config.default_top_k == 1
            record("invalid_toml_retains_last_valid", status)
            config_path.write_text(hot.replace("default_top_k = 1", "default_top_k = 99999"))
            status = await svc.status()
            assert status["last_reload_error"] and svc.config.default_top_k == 1
            record("invalid_field_retains_last_valid", status)
            config_path.write_text(original)
            await svc.refresh()
            assert svc.last_reload_error is None

            generation = (await svc.status())["index_generation"]
            # Editing a copy does not change retrieval of its previously indexed snapshot.
            paths[0].write_text(paths[0].read_text() + "\n## TEST_ONLY_UPDATED\n新增虚构说明。\n")
            prior = await svc.search("日志文件删了空间未释放", source_id="a")
            assert prior.hits and not prior.degraded_reasons
            assert prior.index_generation == generation
            assert all("TEST_ONLY_UPDATED" not in h.document.text for h in prior.hits)
            record("snapshot_survives_source_edit", {"generation": prior.index_generation})
            changed = await svc.index("a")
            assert changed["updated"] == 1 and changed["index_generation"] != generation
            async with svc.snapshot() as request:
                assert request.corpus is not None
                a_doc = next(d for d in request.corpus.documents if d.source_id == "a")
                assert "TEST_ONLY_UPDATED" in a_doc.text
            record("update_copy", changed)

            saved_bytes = paths[1].read_bytes()
            paths[1].unlink()
            keep = await svc.index("b")
            assert (await svc.status())["documents"] == 3
            record("missing_without_prune_retained", keep)
            pruned = await svc.index("b", prune=True)
            assert pruned["deleted"] == 1 and pruned["documents"] == 2
            record("explicit_prune", pruned)
            paths[1].write_bytes(saved_bytes)
            await svc.index("b")

            deleted = await svc.index(delete_id=a_doc.doc_id)
            assert deleted["deleted"] == 1 and paths[0].exists()
            hits = await svc.search("TEST_ONLY_UPDATED", source_id="a")
            assert not hits.hits
            record("delete_preserves_source", deleted)
            await svc.index("a")

            config_path.write_text(original.replace("chunk_chars = 1200", "chunk_chars = 1000"))
            pending = await svc.status()
            assert "chunk_chars" in pending["pending_rebuild"]
            old_generation = pending["index_generation"]
            unchanged = await svc.search("ENOSPC", source_id="a")
            assert unchanged.index_generation == old_generation
            record("cold_change_pending", pending)
            rebuilt = await svc.index(rebuild=True)
            assert rebuilt["index_generation"] != old_generation and rebuilt["documents"] == 3
            assert not (await svc.status())["pending_rebuild"]
            record("rebuild_applies_cold_fields", rebuilt)
            # A second rebuild migrates only this isolated application's storage.
            migrated_text = config_path.read_text().replace(
                'persist_dir = "retrieval/store"', 'persist_dir = "retrieval/moved"'
            )
            config_path.write_text(migrated_text)
            assert "persist_dir" in (await svc.status())["pending_rebuild"]
            record("storage_migration", await svc.index(rebuild=True))
            assert (await svc.status())["documents"] == 3
        async with create() as svc:
            status = await svc.status()
            assert status["documents"] == 3 and status["pending_rebuild"] == []
            for source_id, query in (("a", "日志文件删了空间未释放"), ("c", "ENOSPC")):
                result = await svc.search(query, source_id=source_id)
                assert result.hits and not result.degraded_reasons
                assert all(h.document.source_id == source_id for h in result.hits)
                record("reopen_source_" + source_id, status)
        evidence["calls"] = calls.events
        evidence["temporary_root"] = str(root)
    evidence["cleaned"] = not root.exists()
    save("management_real.json", evidence)


async def _fail(*_args, **_kwargs):
    raise RetrievalServiceError("injected test failure")


async def test_search_degradation_boundaries(monkeypatch):
    results = {}
    async with isolated(monkeypatch) as setup:
        _, _, _, _, _, calls, create = setup
        async with create() as svc:
            empty = await svc.search("ENOSPC")
            assert empty.retrieval_mode == "not_indexed" and not empty.hits
            await svc.index("a")
            for name, target in (
                ("rerank", "tools.retrieval.client.RerankClient.rerank"),
                ("vector", "tools.retrieval.client.EmbeddingClient.embed"),
            ):
                with patch(target, _fail):
                    result = await svc.search("lsof 日志文件 磁盘", source_id="a")
                assert result.hits
                assert any(reason.startswith(name + ":") for reason in result.degraded_reasons)
                assert (
                    all(h.rerank_score is None for h in result.hits) if name == "rerank" else True
                )
                assert result.retrieval_mode == ("bm25" if name == "vector" else "hybrid")
                results[name + "_failure"] = result.to_dict()
            with patch.object(KeywordIndex, "query", side_effect=RuntimeError("injected BM25")):
                result = await svc.search("lsof", source_id="a")
                assert result.retrieval_mode == "vector" and result.hits
                results["bm25_failure"] = result.to_dict()
                with (
                    patch("tools.retrieval.client.EmbeddingClient.embed", _fail),
                    pytest.raises(RetrievalServiceError, match="全部召回通道不可用"),
                ):
                    await svc.search("lsof", source_id="a")
            results["both_channels_raise"] = True
            for query, top_k in ((" ", 1), ("a" * 10001, 1), ("a", 0), ("a", 11), ("a", True)):
                with pytest.raises(ValueError):
                    await svc.search(query, top_k=top_k)
            before = len(calls.events)
            with pytest.raises(ValueError, match="来源"):
                await svc.search("ENOSPC", source_ids=("unknown-source",))
            filter_cases: list[dict[str, Any]] = [
                {"source_ids": ()},
                {"source_id": "no-source"},
                {"service": "tls"},
                {"tags": ("absent",)},
            ]
            for filters in filter_cases:
                assert not (await svc.search("ENOSPC", **filters)).hits
            assert len(calls.events) == before
            results["input_validation_and_filters"] = True
    save("fault_service.json", {"models": "mock", "results": results, "cleaned": True})


async def test_failed_writes_keep_active_generation(monkeypatch):
    evidence: dict[str, Any] = {"models": "mock"}
    async with isolated(monkeypatch) as setup:
        root, paths, _, _, _, _, create = setup
        async with create() as svc:
            await svc.index("a")
            before = await svc.status()
            bad = paths[0].parent / "invalid.md"
            for contents in (b"\xff\xfe", b"a\x00b", b"x" * 2097153):
                bad.write_bytes(contents)
                with pytest.raises(RetrievalServiceError, match="来源扫描失败"):
                    await svc.index("a", prune=True)
                assert (await svc.status())["index_generation"] == before["index_generation"]
            bad.unlink()
            bad.symlink_to(paths[1])
            with pytest.raises(RetrievalServiceError, match="source_boundary"):
                await svc.index("a", prune=True)
            bad.unlink()
            for number in range(11):
                (paths[0].parent / f"limit-{number}.txt").write_text("TEST_ONLY max files")
            with pytest.raises(RetrievalServiceError, match="max_files_exceeded"):
                await svc.index("a", prune=True)
            for file in paths[0].parent.glob("limit-*.txt"):
                file.unlink()
            paths[0].write_text(paths[0].read_text() + "\nNEW_ONLY_FAILURE\n")
            with (
                patch("tools.retrieval.client.EmbeddingClient.embed", _fail),
                pytest.raises(RetrievalServiceError, match="injected"),
            ):
                await svc.index("a")
            with (
                patch(
                    "tools.retrieval.storage.KnowledgeStore.stage",
                    side_effect=RetrievalServiceError("injected stage failure"),
                ),
                pytest.raises(RetrievalServiceError, match="stage"),
            ):
                await svc.index("a")
            after = await svc.status()
            assert after["index_generation"] == before["index_generation"]
            assert after["documents"] == 1
            with pytest.raises(ValueError, match="doc_id"):
                await svc.index(delete_id="not-a-document")
            contender = create()
            with pytest.raises(RetrievalServiceError, match="运行进程"):
                await contender.open()
            evidence.update(
                active_generation_preserved=True,
                last_error=after["last_index_error"],
                rejected=[
                    "invalid_utf8",
                    "binary",
                    "oversized",
                    "boundary_symlink",
                    "max_files",
                    "embedding",
                    "stage",
                    "foreign_doc",
                    "owner_lock",
                ],
                temporary_root=str(root),
            )
    evidence["cleaned"] = True
    save("fault_writes.json", evidence)


@pytest.mark.parametrize("migrate", [False, True], ids=["same-store", "migrated-store"])
async def test_success_clears_persisted_index_error(monkeypatch, migrate):
    evidence: dict[str, Any] = {"models": "mock"}
    async with isolated(monkeypatch) as setup:
        _, _, config_path, original, source, _, create = setup
        async with create() as svc:
            with (
                patch("tools.retrieval.client.EmbeddingClient.embed", _fail),
                pytest.raises(RetrievalServiceError),
            ):
                await svc.index("a")
            evidence["failed_status"] = await svc.status()
            if migrate:
                config_path.write_text(
                    original.replace(
                        'persist_dir = "retrieval/store"', 'persist_dir = "retrieval/recovered"'
                    )
                )
            await svc.index("a", rebuild=migrate)
            evidence["successful_status"] = await svc.status()
            assert evidence["successful_status"]["last_index_error"] is None
            assert json.loads(source.control_path.read_text())["last_index_error"] is None
        async with create() as svc:
            evidence["reopened_status"] = await svc.status()
        save("bug_stale_error.json", evidence)
        assert evidence["reopened_status"]["last_index_error"] is None


async def test_unchanged_success_clears_index_error(monkeypatch):
    evidence: dict[str, Any] = {"models": "mock"}
    async with isolated(monkeypatch) as setup:
        _, paths, _, _, source, _, create = setup
        async with create() as svc:
            indexed = await svc.index("a")
            original_bytes = paths[0].read_bytes()
            paths[0].write_bytes(original_bytes + b"\nTEST_ONLY_FAILED_UPDATE\n")
            with (
                patch("tools.retrieval.client.EmbeddingClient.embed", _fail),
                pytest.raises(RetrievalServiceError),
            ):
                await svc.index("a")
            paths[0].write_bytes(original_bytes)
            result = await svc.index("a")
            assert result["unchanged"] == 1 and result["embedded"] == 0
            assert result["index_generation"] == indexed["index_generation"]
            assert json.loads(source.control_path.read_text())["last_index_error"] is None
            evidence.update(index=result, status=await svc.status())
        async with create() as svc:
            reopened = await svc.status()
            assert reopened["last_index_error"] is None
            assert reopened["index_generation"] == indexed["index_generation"]
        save("bug_noop_error.json", evidence)
        assert evidence["status"]["last_index_error"] is None


@pytest.mark.real_llm
@pytest.mark.skipif(os.environ.get("PANICLESS_RAG_LIVE") != "1", reason="explicit live run only")
async def test_expansion_budget_fallback_and_disable(monkeypatch):
    evidence: dict[str, Any] = {"models": "real"}
    async with isolated(monkeypatch, live=True) as setup:
        _, paths, config_path, original, _, _, create = setup
        # Long synthetic section guarantees adjacent expansion and section fallback are exercised.
        paths[0].write_text(
            "# TEST_ONLY_EXPANSION\n\n"
            + "\n\n".join(
                f"段落 {i} lsof 磁盘日志：" + "检查删除日志后未释放空间。" * 12 for i in range(20)
            )
        )
        async with create() as svc:
            await svc.index("a")
            result = await svc.search("lsof 磁盘日志", source_id="a", top_k=1)
            assert result.hits and result.context_blocks and not result.degraded_reasons
            assert all(h.rerank_score is not None for h in result.hits)
            async with svc.snapshot() as request:
                checked = check_citations(result, request.corpus, request.config, disk=True)
            assert checked["expanded_blocks"] > 0
            evidence["adjacent"] = result.to_dict()
            small = original.replace('mode = "adjacent"', 'mode = "section"')
            small = small.replace("max_chars_per_block = 3600", "max_chars_per_block = 1300")
            small = small.replace("max_total_chars = 12000", "max_total_chars = 1400")
            config_path.write_text(small)
            result = await svc.search("lsof 磁盘日志", source_id="a")
            assert result.hits and result.context_blocks and not result.degraded_reasons
            assert all(h.rerank_score is not None for h in result.hits)
            assert any(b.expansion_fallback for b in result.context_blocks)
            assert result.context_truncated
            async with svc.snapshot() as request:
                check_citations(result, request.corpus, request.config)
            evidence["section_fallback_and_budget"] = result.to_dict()
            config_path.write_text(
                original.replace(
                    "[retrieval.context_expansion]\nenabled = true",
                    "[retrieval.context_expansion]\nenabled = false",
                )
            )
            result = await svc.search("lsof 磁盘日志", source_id="a", top_k=1)
            assert result.hits and result.context_blocks and not result.degraded_reasons
            assert all(h.rerank_score is not None for h in result.hits)
            assert all(b.expansion_mode == "disabled" for b in result.context_blocks)
            evidence["disabled"] = result.to_dict()
    save("expansion_real.json", evidence)
