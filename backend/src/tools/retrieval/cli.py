"""知识库管理 CLI；复用项目配置规则，操作全局知识库和指定来源。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from baseagent_config.retrieval import RetrievalConfigSource
from tools.retrieval.chroma import ChromaStore
from tools.retrieval.service import RetrievalService


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="python -m tools.retrieval")
    root.add_argument("--config", type=Path, help="项目 config.toml；省略使用既有发现规则")
    commands = root.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("index", "显式扫描来源，入库新增/变化资料"),
        ("status", "索引、配置修订、待重建字段和错误状态"),
        ("search", "混合检索、可选重排和快照扩展"),
        ("delete", "删除指定文档及其索引记录，不删除源文件"),
        ("rebuild", "按当前待重建配置构建/迁移并发布知识库新代"),
        ("health", "本地 SQLite / Chroma 健康检查；不调用模型"),
    ):
        command = commands.add_parser(name, help=help_text)
        if name == "index":
            command.add_argument("--source-id", required=True)
            command.add_argument(
                "--prune", action="store_true", help="仅完整成功扫描后清除缺失文件的索引记录"
            )
        elif name == "search":
            command.add_argument("--query", required=True)
            command.add_argument("--top-k", type=int)
            command.add_argument("--source-id")
            command.add_argument("--service")
            command.add_argument("--tag", action="append", default=[], help="可重复，全部匹配")
        elif name == "delete":
            command.add_argument("--doc-id", required=True)
    return root


def health_probe(service: RetrievalService) -> dict[str, Any]:
    store = service._store
    assert store is not None and store.db is not None and store.chroma is not None
    check = store.db.execute("PRAGMA quick_check").fetchone()[0]
    if check != "ok":
        raise RuntimeError("SQLite quick_check failed")
    name = "plhealth_" + uuid.uuid4().hex
    client = store.chroma
    client.call("create", name)
    try:
        client.call(
            "upsert",
            name,
            ids=["temporary"],
            embeddings=[[1.0, 0.0, 0.0]],
            metadatas=[{"workspace_id": "health-only"}],
        )
        found = client.call("query", name, query_embeddings=[[1.0, 0.0, 0.0]], n_results=1)
        if found["ids"] != [["temporary"]]:
            raise RuntimeError("Chroma query failed")
        # CLI 独占库，没有服务内在途查询；公共 close + 重开验证真实持久化。
        client.close()
        client = ChromaStore(Path(store.config.chroma_path))
        store.chroma = client
        assert store._chroma_lease is not None
        store._chroma_lease.resource = client
        if client.call("get", name, ids=["temporary"])["ids"] != ["temporary"]:
            raise RuntimeError("Chroma reopen failed")
    finally:
        client.call("delete_collection", name)
    return {
        "ok": True,
        "sqlite": check,
        "chroma": client.info,
        "write_query_reopen_read": "passed",
        "temporary_collection_removed": name,
    }


async def execute(args: argparse.Namespace) -> dict[str, Any]:
    source = RetrievalConfigSource(args.config)
    async with RetrievalService(
        source(), control_path=source.control_path, reload_config=source
    ) as service:
        if args.command == "status":
            return {"ok": True, **await service.status()}
        if args.command == "health":
            return await service.run(health_probe, service)
        if args.command == "search":
            result = await service.search(
                args.query,
                top_k=args.top_k,
                source_id=args.source_id,
                service=args.service,
                tags=tuple(args.tag),
            )
            return {"ok": True, **result.to_dict()}
        if args.command == "delete":
            return await service.index(delete_id=args.doc_id)
        return await service.index(
            getattr(args, "source_id", None),
            prune=getattr(args, "prune", False),
            rebuild=args.command == "rebuild",
        )


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        result = asyncio.run(execute(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(
            json.dumps(
                {"ok": False, "error": {"type": type(exc).__name__, "message": str(exc)}},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
