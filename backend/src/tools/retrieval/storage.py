"""SQLite 元数据/快照与 Chroma 代管理；单拥有者、暂存发布及按身份回收。"""

from __future__ import annotations

import fcntl
import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from baseagent_config.models import RetrievalConfig
from tools.retrieval.chroma import ChromaStore
from tools.retrieval.errors import RetrievalServiceError
from tools.retrieval.records import Chunk, Corpus, Document, Generation, Section


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


@dataclass
class _Lease:
    resource: Any
    references: int = 1

    def release(self) -> None:
        self.references -= 1
        if not self.references:
            self.resource.close()


class KnowledgeStore:
    """所有方法在服务的单个工作线程调用；进程间采用明确的独占维护模式。"""

    def __init__(self, config: RetrievalConfig, shared_with: KnowledgeStore | None = None) -> None:
        self.config = config
        self.cleanup_ids: set[str] = set()
        root = Path(config.persist_dir)
        root.mkdir(parents=True, exist_ok=True)
        if shared_with and shared_with.config.persist_dir == config.persist_dir:
            self._owner = shared_with._owner
            self._owner.references += 1
        else:
            lock = (root / ".owner.lock").open("a+b")
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                lock.close()
                raise RetrievalServiceError(
                    "检索库已被其他进程占用；请先关闭该进程再运行管理 CLI"
                ) from None
            self._owner = _Lease(lock)
        self.db: sqlite3.Connection | None = None
        self.chroma: ChromaStore | None = None
        self._chroma_lease: _Lease | None = None
        try:
            Path(config.sqlite_path).parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(config.sqlite_path, timeout=5)
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA foreign_keys=ON")
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, 1}:
                raise RetrievalServiceError("检索元数据库版本高于本程序")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS generations (
                    id TEXT PRIMARY KEY, workspace TEXT NOT NULL, data TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('staging','active','retired')));
                CREATE TABLE IF NOT EXISTS active (
                    workspace TEXT PRIMARY KEY,
                    generation TEXT NOT NULL REFERENCES generations(id));
                CREATE TABLE IF NOT EXISTS documents (
                    generation TEXT NOT NULL REFERENCES generations(id) ON DELETE CASCADE,
                    id TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(generation,id));
                CREATE TABLE IF NOT EXISTS chunks (
                    generation TEXT NOT NULL REFERENCES generations(id) ON DELETE CASCADE,
                    id TEXT NOT NULL, text_hash TEXT NOT NULL, data TEXT NOT NULL,
                    PRIMARY KEY(generation,id));
                CREATE TABLE IF NOT EXISTS vectors (
                    fingerprint TEXT NOT NULL, text_hash TEXT NOT NULL, vector TEXT NOT NULL,
                    PRIMARY KEY(fingerprint,text_hash));
                PRAGMA user_version=1;
            """)
            if shared_with and shared_with.config.chroma_path == config.chroma_path:
                self._chroma_lease = shared_with._chroma_lease
                assert self._chroma_lease is not None
                self._chroma_lease.references += 1
            else:
                self._chroma_lease = _Lease(ChromaStore(Path(config.chroma_path)))
            self.chroma = self._chroma_lease.resource
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self.chroma is not None:
            assert self._chroma_lease is not None
            self._chroma_lease.release()
            self.chroma = None
        if self.db is not None:
            self.db.close()
            self.db = None
        self._owner.release()

    def active(self, workspace: str) -> Corpus | None:
        assert self.db is not None
        row = self.db.execute(
            "SELECT generation FROM active WHERE workspace=?", (workspace,)
        ).fetchone()
        return self.snapshot(row[0]) if row else None

    def snapshot(self, generation: str) -> Corpus:
        assert self.db is not None
        row = self.db.execute("SELECT data FROM generations WHERE id=?", (generation,)).fetchone()
        if row is None:
            raise RetrievalServiceError("索引代不存在")
        docs = []
        for (raw,) in self.db.execute(
            "SELECT data FROM documents WHERE generation=?", (generation,)
        ):
            data = json.loads(raw)
            data["sections"] = tuple(
                Section(**{**s, "heading_path": tuple(s["heading_path"])}) for s in data["sections"]
            )
            docs.append(Document(**data))
        chunks = []
        for (raw,) in self.db.execute("SELECT data FROM chunks WHERE generation=?", (generation,)):
            data = json.loads(raw)
            data["heading_path"] = tuple(data["heading_path"])
            chunks.append(Chunk(**data))
        return Corpus(
            Generation(**json.loads(row[0])),
            tuple(sorted(docs, key=lambda d: d.doc_id)),
            tuple(sorted(chunks, key=lambda c: (c.doc_id, c.ordinal))),
        )

    def workspaces(self) -> list[str]:
        assert self.db is not None
        return [
            row[0] for row in self.db.execute("SELECT workspace FROM active ORDER BY workspace")
        ]

    def cached(self, fingerprint: str, hashes: list[str]) -> dict[str, list[float]]:
        assert self.db is not None
        result = {}
        for text_hash in set(hashes):
            row = self.db.execute(
                "SELECT vector FROM vectors WHERE fingerprint=? AND text_hash=?",
                (fingerprint, text_hash),
            ).fetchone()
            if row:
                result[text_hash] = json.loads(row[0])
        return result

    def stage(self, corpus: Corpus, vectors: dict[str, list[float]]) -> None:
        assert self.db is not None and self.chroma is not None
        generation = corpus.generation
        identity = generation.generation_id
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO generations VALUES (?,?,?,'staging')",
                (identity, generation.workspace_id, _json(asdict(generation))),
            )
            self.db.executemany(
                "INSERT OR IGNORE INTO documents VALUES (?,?,?)",
                [(identity, d.doc_id, _json(asdict(d))) for d in corpus.documents],
            )
            self.db.executemany(
                "INSERT OR IGNORE INTO chunks VALUES (?,?,?,?)",
                [(identity, c.chunk_id, c.text_hash, _json(asdict(c))) for c in corpus.chunks],
            )
            self.db.executemany(
                "INSERT OR IGNORE INTO vectors VALUES (?,?,?)",
                [(generation.fingerprint, h, _json(v)) for h, v in vectors.items()],
            )
        # SQLite staging 行先落盘，使中断后能按确切身份回收 collection。
        exists = identity in self.chroma.call("list")
        if exists:
            if self.chroma.call("count", identity) != len(corpus.chunks):
                raise RetrievalServiceError("已有 collection 与迁移快照不一致")
            return
        self.chroma.call("create", identity)
        docs = {d.doc_id: d for d in corpus.documents}
        for start in range(0, len(corpus.chunks), 128):
            batch = corpus.chunks[start : start + 128]
            self.chroma.call(
                "upsert",
                identity,
                ids=[c.chunk_id for c in batch],
                embeddings=[vectors[c.text_hash] for c in batch],
                metadatas=[
                    {
                        "workspace_id": generation.workspace_id,
                        "source_id": docs[c.doc_id].source_id,
                        "doc_id": c.doc_id,
                    }
                    for c in batch
                ],
            )
        if self.chroma.call("count", identity) != len(corpus.chunks):
            raise RetrievalServiceError("暂存代向量数量与片段数量不一致")
        if len({c.chunk_id for c in corpus.chunks}) != len(corpus.chunks):
            raise RetrievalServiceError("暂存代存在重复片段身份")

    def publish(self, generation: Generation) -> None:
        assert self.db is not None
        with self.db:
            self.db.execute(
                "UPDATE generations SET state='retired' WHERE id IN "
                "(SELECT generation FROM active WHERE workspace=?)",
                (generation.workspace_id,),
            )
            self.db.execute(
                "INSERT INTO active VALUES (?,?) ON CONFLICT(workspace) "
                "DO UPDATE SET generation=excluded.generation",
                (generation.workspace_id, generation.generation_id),
            )
            self.db.execute(
                "UPDATE generations SET state='active' WHERE id=?", (generation.generation_id,)
            )

    def vector_query(
        self, corpus: Corpus, vector: list[float], sources: list[str], limit: int
    ) -> list[tuple[str, float]]:
        assert self.chroma is not None
        if not sources or not corpus.chunks:
            return []
        result = self.chroma.call(
            "query",
            corpus.generation.generation_id,
            query_embeddings=[vector],
            n_results=limit,
            where={"source_id": {"$in": sources}},
            include=["distances"],
        )
        return list(zip(result["ids"][0], result["distances"][0], strict=True))

    def collect(self, pinned: set[str], keep_collections: set[str] | None = None) -> list[str]:
        assert self.db is not None and self.chroma is not None
        rows = self.db.execute("SELECT id FROM generations WHERE state!='active'").fetchall()
        removable = [row[0] for row in rows if row[0] not in pinned]
        collections = set(self.chroma.call("list")) if removable else set()
        removed = []
        for (identity,) in rows:
            if identity in pinned:
                continue
            if identity in collections and identity not in (keep_collections or set()):
                self.chroma.call("delete_collection", identity)
            with self.db:
                self.db.execute("DELETE FROM generations WHERE id=?", (identity,))
            removed.append(identity)
        # 仅回收本库登记且无任何代引用的缓存，不触碰其他 collection。
        referenced: set[tuple[str, str]] = set()
        for raw, text_hash in self.db.execute(
            "SELECT g.data,c.text_hash FROM generations g JOIN chunks c ON g.id=c.generation"
        ):
            referenced.add((json.loads(raw)["fingerprint"], text_hash))
        with self.db:
            for fingerprint, text_hash in self.db.execute(
                "SELECT fingerprint,text_hash FROM vectors"
            ).fetchall():
                if (fingerprint, text_hash) not in referenced:
                    self.db.execute(
                        "DELETE FROM vectors WHERE fingerprint=? AND text_hash=?",
                        (fingerprint, text_hash),
                    )
        return removed

    def generation_ids(self) -> set[str]:
        assert self.db is not None
        return {row[0] for row in self.db.execute("SELECT id FROM generations")}

    def cleanup_migrated(self, target: KnowledgeStore) -> None:
        """旧读者排空后按登记代清理，分别考虑共享 SQLite / Chroma 路径。"""
        assert self.chroma is not None
        if self.config.sqlite_path == target.config.sqlite_path:
            if self.config.chroma_path != target.config.chroma_path:
                existing = set(self.chroma.call("list"))
                for identity in (self.cleanup_ids or self.generation_ids()) & existing:
                    self.chroma.call("delete_collection", identity)
        else:
            keep = (
                target.generation_ids()
                if self.config.chroma_path == target.config.chroma_path
                else set()
            )
            self.retire_all()
            self.collect(set(), keep)

    def retire_all(self) -> None:
        """仅在新存储发布、旧请求排空后用于迁移清理。"""
        assert self.db is not None
        with self.db:
            self.db.execute("DELETE FROM active")
            self.db.execute("UPDATE generations SET state='retired'")

    def status(self, workspace: str) -> dict[str, Any]:
        corpus = self.active(workspace)
        return {
            "workspace_id": workspace,
            "index_generation": corpus.generation.generation_id if corpus else None,
            "fingerprint": corpus.generation.fingerprint if corpus else None,
            "dimension": corpus.generation.dimension if corpus else None,
            "documents": len(corpus.documents) if corpus else 0,
            "chunks": len(corpus.chunks) if corpus else 0,
            "chroma": self.chroma.info if self.chroma else None,
        }
