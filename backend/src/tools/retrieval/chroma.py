"""Chroma 本地进程适配；进程退出释放原生资源，不调用私有 System API。"""

from __future__ import annotations

import multiprocessing
from contextlib import suppress
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

from tools.retrieval.errors import RetrievalServiceError


def _worker(pipe: Connection, path: str) -> None:
    client: Any = None
    try:
        import chromadb
        from chromadb.config import Settings

        client = chromadb.PersistentClient(
            path=path, settings=Settings(anonymized_telemetry=False, allow_reset=False)
        )
        pipe.send((True, {"version": chromadb.__version__}))
        while True:
            operation, name, kwargs = pipe.recv()
            if operation == "close":
                break
            try:
                if operation == "create":
                    client.create_collection(
                        name=name,
                        embedding_function=None,
                        configuration={"hnsw": {"space": "cosine"}},
                    )
                    result: Any = None
                elif operation == "delete_collection":
                    client.delete_collection(name)
                    result = None
                elif operation == "list":
                    result = [
                        c.name if hasattr(c, "name") else str(c) for c in client.list_collections()
                    ]
                else:
                    collection = client.get_collection(name=name, embedding_function=None)
                    if operation not in {"upsert", "get", "query", "count"}:
                        raise ValueError("unsupported Chroma operation")
                    result = getattr(collection, operation)(**kwargs)
                pipe.send((True, result))
            except Exception as exc:
                pipe.send((False, type(exc).__name__))
    except EOFError:
        pass
    except Exception as exc:
        pipe.send((False, type(exc).__name__))
    finally:
        if client is not None:
            client.close()
        pipe.close()


class ChromaStore:
    """调用者串行使用；只接收显式向量，绝不传 query_texts/documents。"""

    def __init__(self, path: Path, timeout_sec: float = 30.0) -> None:
        context = multiprocessing.get_context("spawn")
        self._pipe, child = context.Pipe()
        self._timeout = timeout_sec
        self._process = context.Process(target=_worker, args=(child, str(path)), daemon=True)
        self._process.start()
        child.close()
        try:
            self.info = self._receive()
        except BaseException:
            self.close()
            raise

    def _receive(self) -> Any:
        if not self._pipe.poll(self._timeout):
            # 不再复用失去请求/响应对应关系的子进程。
            self.close()
            raise RetrievalServiceError("Chroma 操作超时，需重新打开本地存储")
        try:
            ok, result = self._pipe.recv()
        except (EOFError, OSError):
            raise RetrievalServiceError("Chroma 本地进程已退出") from None
        if not ok:
            raise RetrievalServiceError(f"Chroma 操作失败：{result}")
        return result

    def call(self, operation: str, name: str = "", **kwargs: Any) -> Any:
        try:
            self._pipe.send((operation, name, kwargs))
        except (OSError, EOFError):
            raise RetrievalServiceError("Chroma 本地进程不可用") from None
        return self._receive()

    def close(self) -> None:
        if self._process.is_alive():
            with suppress(OSError, EOFError):
                self._pipe.send(("close", "", {}))
            self._process.join(timeout=5)
            if self._process.is_alive():
                self._process.terminate()
                self._process.join(timeout=5)
        self._pipe.close()
