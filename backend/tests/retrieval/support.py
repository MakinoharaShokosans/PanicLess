"""Shared assertions and model-call instrumentation for RAG verification only."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

import httpx

from tools.retrieval.client import EmbeddingClient, RerankClient

ROOT = Path(__file__).resolve().parents[3]
HOME = ROOT / "storage/rag-real-test"
RESULTS = HOME / "results"


def save(name, value):
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


class Calls:
    """Delegates to production clients; mock transports are explicit and optional."""

    def __init__(self, live=True):
        self.live = live
        self.events = []

    def embedding(self, config):
        owner = self

        class Counted(EmbeddingClient):
            async def embed(self, texts):
                event = {"kind": "embedding", "inputs": len(texts), "live": owner.live}
                owner.events.append(event)
                vectors = await super().embed(texts)
                event.update(ok=True, dimensions=sorted({len(v) for v in vectors}))
                return vectors

        return Counted(config, transport=None if self.live else httpx.MockTransport(self.reply))

    def rerank(self, config):
        owner = self

        class Counted(RerankClient):
            async def rerank(self, query, documents, top_n):
                event = {"kind": "rerank", "inputs": len(documents), "live": owner.live}
                owner.events.append(event)
                ranks = await super().rerank(query, documents, top_n)
                event.update(ok=True, output_count=len(ranks))
                return ranks

        return Counted(config, transport=None if self.live else httpx.MockTransport(self.reply))

    @staticmethod
    def reply(request):
        body = json.loads(request.content)
        if "input" in body:
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"index": i, "embedding": [1.0, (sum(map(ord, text)) % 97) / 97, 0.5]}
                        for i, text in enumerate(body["input"])
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": i, "relevance_score": 1.0 / (i + 1)} for i in range(body["top_n"])
                ]
            },
        )


def check_citations(result, corpus, config, *, disk=False):
    docs = {d.doc_id: d for d in corpus.documents}
    chunks = {c.chunk_id: c for c in corpus.chunks}
    blocks = {b.block_id: b for b in result.context_blocks}
    expanded = 0
    for hit in result.hits:
        chunk, doc = hit.chunk, docs[hit.chunk.doc_id]
        assert chunk.text == doc.text[chunk.char_start : chunk.char_end]
        assert chunk.line_start == doc.text.count("\n", 0, chunk.char_start) + 1
        assert chunk.line_end == doc.text.count("\n", 0, chunk.char_end - 1) + 1
        assert math.isclose(
            hit.rrf_score,
            sum(
                1 / (config.rrf_k + rank)
                for rank in (hit.vector_rank, hit.bm25_rank)
                if rank is not None
            ),
        )
        assert chunk.chunk_id in blocks[hit.context_block_id].anchor_chunk_ids
        if disk:
            text = (Path(doc.source_root) / doc.relative_path).read_text(encoding="utf-8-sig")
            assert text == doc.text
    assert sum(len(b.text) for b in blocks.values()) <= config.context_expansion.max_total_chars
    for block in blocks.values():
        doc = docs[block.doc_id]
        assert block.text == doc.text[block.char_start : block.char_end]
        assert len(block.text) <= config.context_expansion.max_chars_per_block
        assert block.line_start == doc.text.count("\n", 0, block.char_start) + 1
        if block.text:
            assert block.line_end == doc.text.count("\n", 0, block.char_end - 1) + 1
        for identity in block.included_chunk_ids:
            assert chunks[identity].doc_id == block.doc_id
        anchors = [chunks[i] for i in block.anchor_chunk_ids]
        if anchors and (
            block.char_start < min(a.char_start for a in anchors)
            or block.char_end > max(a.char_end for a in anchors)
        ):
            expanded += 1
    return {
        "hits_checked": len(result.hits),
        "blocks_checked": len(blocks),
        "expanded_blocks": expanded,
    }


def config_copy(destination, home, sources):
    text = (Path(__file__).parent / "fixtures/rag-test.toml").read_text()
    text = re.sub(r"^home\s*=.*$", "home = " + json.dumps(str(home)), text, flags=re.M)
    # The test config contains exactly one inline source declaration.
    source_text = (
        "sources = ["
        + ", ".join(
            "{ "
            + ", ".join(k + " = " + json.dumps(v, ensure_ascii=False) for k, v in source.items())
            + " }"
            for source in sources
        )
        + "]"
    )
    text = re.sub(r"^sources\s*=.*$", source_text, text, flags=re.M)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text)
    return text
