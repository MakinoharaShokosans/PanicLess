"""Small real-service compatibility probes; credentials come only from the environment."""

from __future__ import annotations

import asyncio
import json
import math
import os
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from baseagent_config.retrieval import RetrievalConfigSource
from tools.retrieval.adapters import IndexedRerankAdapter, OpenAIEmbeddingsAdapter

ROOT = Path(__file__).resolve().parents[3]
OUTPUT = ROOT / "storage/rag-real-test/results"


async def probe() -> dict:
    config = RetrievalConfigSource(Path(__file__).parent / "fixtures/rag-test.toml")()
    results: dict[str, Any] = {}
    async with httpx.AsyncClient(timeout=30, trust_env=False, follow_redirects=False) as client:
        for name, model, payload in (
            (
                "embedding",
                config.embedding,
                OpenAIEmbeddingsAdapter.request(config.embedding.model, ["磁盘空间不足 ENOSPC"]),
            ),
            (
                "rerank",
                config.rerank,
                IndexedRerankAdapter.request(
                    config.rerank.model,
                    "磁盘空间不足",
                    ["检查 df -h 与 df -i", "检查 TLS 证书到期时间"],
                    2,
                ),
            ),
        ):
            item: dict[str, Any] = {"endpoint": model.endpoint_url, "model": model.model}
            try:
                key = os.environ[model.api_key_env].strip()
                response = await client.post(
                    model.endpoint_url,
                    json=payload,
                    headers={"Authorization": "Bearer " + key},
                )
                item["http_status"] = response.status_code
                # Do not store arbitrary error text from a credentialed remote service.
                if response.is_success:
                    body = response.json()
                    item["response_keys"] = sorted(body) if isinstance(body, dict) else []
                    if name == "embedding":
                        vectors = OpenAIEmbeddingsAdapter.parse(body, 1)
                        assert all(math.isfinite(v) for v in vectors[0])
                        item.update(dimension=len(vectors[0]), nonzero=any(vectors[0]))
                        assert item["nonzero"]
                    else:
                        ranks = IndexedRerankAdapter.parse(body, 2, 2)
                        item["ranking"] = [asdict(rank) for rank in ranks]
                    item["passed"] = True
                else:
                    item["passed"] = False
                    try:
                        error = response.json().get("error", {})
                        if isinstance(error, dict):
                            item["remote_error"] = {
                                field: str(error[field]).replace(key, "[REDACTED]")[:400]
                                for field in ("code", "type", "message")
                                if field in error
                            }
                    except (ValueError, AttributeError):
                        pass
            except Exception as exc:
                item.update(passed=False, error_type=type(exc).__name__)
            results[name] = item
    OUTPUT.mkdir(parents=True, exist_ok=True)
    history_path = OUTPUT / "probe_history.json"
    history = json.loads(history_path.read_text()) if history_path.exists() else []
    history.append({"time_utc": datetime.now(UTC).isoformat(), "results": results})
    history_path.write_text(json.dumps(history, ensure_ascii=False, indent=2))
    (OUTPUT / "probe.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
    print(json.dumps(results, ensure_ascii=False), flush=True)
    return results


if __name__ == "__main__":
    asyncio.run(probe())
