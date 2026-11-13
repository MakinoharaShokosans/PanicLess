"""Ordered live verification.

Provide PANICLESS_RAG_TEST_API_KEY and BASEAGENT_HOME in the environment.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from support import ROOT, Calls, check_citations, save

from baseagent_config.retrieval import RetrievalConfigSource, model_fingerprint
from tools.retrieval.cli import health_probe
from tools.retrieval.service import RetrievalService

SOURCE = "ops-manuals"


async def run():
    source = RetrievalConfigSource(Path(__file__).parent / "fixtures/rag-test.toml")
    config = source()
    calls = Calls(live=True)
    evidence = {"mode": "real HTTP models; real SQLite/Chroma", "steps": {}, "queries": []}
    original = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted((ROOT / "docs/运维手册").glob("*.md"))
    }
    assert len(original) == 10

    def checkpoint(step, value):
        evidence["steps"][step] = value
        evidence["model_calls"] = calls.events
        save("real_chain.json", evidence)
        print(json.dumps({"step": step, "result": value}, ensure_ascii=False), flush=True)

    def service():
        return RetrievalService(
            source(),
            control_path=source.control_path,
            reload_config=source,
            embedding_factory=calls.embedding,
            rerank_factory=calls.rerank,
        )

    try:
        async with service() as svc:
            checkpoint("initial_status", await svc.status())
            checkpoint("health", await svc.run(health_probe, svc))
            indexed = await svc.index(SOURCE)
            checkpoint("index", indexed)
            status = await svc.status()
            assert status["documents"] == 10 and status["chunks"] > 0
            assert status["fingerprint"] == model_fingerprint(config)
            async with svc.snapshot() as request:
                corpus = request.corpus
                assert corpus is not None and request.store.chroma is not None
                assert {d.relative_path for d in corpus.documents} == set(original)
                assert all(d.source_id == SOURCE for d in corpus.documents)
                assert all(
                    any(c.doc_id == d.doc_id for c in corpus.chunks) for d in corpus.documents
                )
                cache = await svc.run(
                    request.store.cached,
                    corpus.generation.fingerprint,
                    [c.text_hash for c in corpus.chunks],
                )
                assert set(cache) == {c.text_hash for c in corpus.chunks}
                assert all(
                    len(v) == status["dimension"] and all(math.isfinite(x) for x in v) and any(v)
                    for v in cache.values()
                )
                chroma_count = await svc.run(
                    request.store.chroma.call, "count", corpus.generation.generation_id
                )
                assert chroma_count == len(corpus.chunks)
                save(
                    "corpus_manifest.json",
                    {
                        "status": status,
                        "chroma_vectors": chroma_count,
                        "documents": [
                            {
                                "doc_id": d.doc_id,
                                "path": d.relative_path,
                                "revision": d.revision,
                                "hash": d.content_hash,
                                "source_root": d.source_root,
                                "chunks": sum(c.doc_id == d.doc_id for c in corpus.chunks),
                            }
                            for d in corpus.documents
                        ],
                    },
                )
            checkpoint("index_integrity", status)
            before = len(calls.events)
            repeated = await svc.index(SOURCE)
            assert repeated["unchanged"] == 10 and repeated["embedded"] == 0
            assert repeated["index_generation"] == status["index_generation"]
            assert len(calls.events) == before
            checkpoint("repeat_no_model_calls", repeated)

        async with service() as svc:
            reopened = await svc.status()
            assert reopened["index_generation"] == status["index_generation"]
            assert reopened["documents"] == 10 and reopened["chunks"] == status["chunks"]
            checkpoint("reopen", reopened)
            cases = json.loads((Path(__file__).parent / "queries.json").read_text())
            for case in cases:
                item = dict(case)
                try:
                    start = len(calls.events)
                    result = await svc.search(case["query"], source_id=SOURCE, top_k=5)
                    item["result"] = result.to_dict()
                    async with svc.snapshot() as request:
                        item["citations"] = check_citations(
                            result, request.corpus, request.config, disk=True
                        )
                    assert result.retrieval_mode == "hybrid"
                    assert result.hits and any(h.vector_rank for h in result.hits)
                    assert any(e["kind"] == "rerank" and e.get("ok") for e in calls.events[start:])
                    assert all(h.rerank_score is not None for h in result.hits)
                    assert not result.degraded_reasons, result.degraded_reasons
                    scores = [h.rerank_score for h in result.hits]
                    assert scores == sorted(scores, reverse=True)
                    ranks = [
                        i
                        for i, h in enumerate(result.hits, 1)
                        if h.document.relative_path.startswith(case["topic"] + "_")
                    ]
                    item.update(
                        passed=True,
                        observed_topic_rank=min(ranks) if ranks else None,
                        bm25_present_in_final_hits=any(h.bm25_rank for h in result.hits),
                    )
                except Exception as exc:
                    item.update(passed=False, error_type=type(exc).__name__, error=str(exc))
                evidence["queries"].append(item)
                evidence["model_calls"] = calls.events
                save("real_chain.json", evidence)
                print(
                    json.dumps(
                        {
                            "topic": case["topic"],
                            "kind": case["kind"],
                            "passed": item["passed"],
                            "rank": item.get("observed_topic_rank"),
                            "error": item.get("error"),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            before = len(calls.events)
            filtered = []
            filter_cases: list[dict[str, Any]] = [
                {"source_ids": ()},
                {"source_id": "unknown-source"},
                {"service": "absent-service"},
                {"tags": ("absent-tag",)},
            ]
            for filters in filter_cases:
                result = await svc.search("ENOSPC", **filters)
                assert not result.hits and not result.context_blocks
                filtered.append(result.to_dict())
            assert len(calls.events) == before
            checkpoint(
                "filter_no_leak_no_model_calls",
                {"filtered": filtered},
            )
            tagged = await svc.search("ENOSPC", source_id=SOURCE, tags=("ops",))
            assert tagged.hits and all(h.document.source_id == SOURCE for h in tagged.hits)
            checkpoint("positive_source_tag", tagged.to_dict())
            checkpoint("final_status", await svc.status())
    except Exception as exc:
        checkpoint("fatal", {"type": type(exc).__name__, "message": str(exc)})
        raise
    finally:
        current = {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((ROOT / "docs/运维手册").glob("*.md"))
        }
        checkpoint("original_manual_hashes_unchanged", current == original)
        assert current == original, "Original manuals changed during verification"
    assert len(evidence["queries"]) == 20 and all(q["passed"] for q in evidence["queries"])


if __name__ == "__main__":
    asyncio.run(run())
