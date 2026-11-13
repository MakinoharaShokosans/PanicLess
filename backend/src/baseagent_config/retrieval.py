"""检索配置加载、不可变修订及冷/热字段合并；不依赖检索实现。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from baseagent_config.loader import ConfigError, load_settings
from baseagent_config.models import RetrievalConfig

COLD_FIELDS = (
    "persist_dir",
    "sqlite_path",
    "chroma_path",
    "embedding_revision",
    "preprocessing_version",
    "chunker_version",
    "chunk_chars",
    "overlap_chars",
)
EMBEDDING_IDENTITY = ("protocol", "endpoint_url", "model")


def revision(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def cold_values(config: RetrievalConfig) -> dict[str, Any]:
    data = config.model_dump(mode="json")
    return {
        **{k: data[k] for k in COLD_FIELDS},
        "embedding": {k: data["embedding"][k] for k in EMBEDDING_IDENTITY},
    }


def with_active_identity(desired: RetrievalConfig, active: dict[str, Any]) -> RetrievalConfig:
    data = desired.model_dump(mode="json")
    for name in COLD_FIELDS:
        data[name] = active[name]
    data["embedding"].update(active["embedding"])
    return RetrievalConfig.model_validate(data)


def pending_fields(config: RetrievalConfig, active: dict[str, Any]) -> list[str]:
    current = cold_values(config)
    fields = [k for k in COLD_FIELDS if current[k] != active[k]]
    fields += [
        f"embedding.{k}"
        for k in EMBEDDING_IDENTITY
        if current["embedding"][k] != active["embedding"][k]
    ]
    return fields


def model_fingerprint(config: RetrievalConfig) -> str:
    return revision(
        {
            "embedding": {k: getattr(config.embedding, k) for k in EMBEDDING_IDENTITY},
            "revision": config.embedding_revision,
            "preprocessing": config.preprocessing_version,
        }
    )


class RetrievalConfigSource:
    """每次读取复用项目配置校验；相对资料路径基于配置文件目录。"""

    def __init__(self, path: Path | None = None, *, home: Path | None = None) -> None:
        # 应用装配可显式绑定其已解析 home，保留启动时命令行覆盖的优先级。
        self._overrides = {"app": {"home": str(home)}} if home is not None else {}
        loaded = load_settings(path, self._overrides, create_home=False, require_models=False)
        self.path = loaded.config_path.resolve()
        self.home = loaded.home.resolve()
        # 控制文件不随 persist_dir 移动，用于重启后继续找到尚未迁移的活动库。
        identity = revision(str(self.path))[:16]
        self.control_path = self.home / "retrieval" / f"control-{identity}.json"

    def __call__(self) -> RetrievalConfig:
        loaded = load_settings(self.path, self._overrides, create_home=False, require_models=False)
        if loaded.home.resolve() != self.home:
            raise ConfigError(["检索运行期间不可变更应用 home；请先停止服务"], self.path)
        data = loaded.settings.retrieval.model_dump(mode="json")
        root = Path(data["persist_dir"]).expanduser()
        root = (root if root.is_absolute() else self.home / root).resolve()
        if not root.is_relative_to(self.home):
            raise ConfigError(["retrieval.persist_dir 必须位于应用数据目录内"], self.path)
        data["persist_dir"] = str(root)
        for key in ("sqlite_path", "chroma_path"):
            location = Path(data[key]).expanduser()
            location = (location if location.is_absolute() else root / location).resolve()
            if not location.is_relative_to(root) or location == root:
                raise ConfigError([f"retrieval.{key} 必须位于 persist_dir 内"], self.path)
            data[key] = str(location)
        if data["sqlite_path"] == data["chroma_path"]:
            raise ConfigError(["SQLite 与 Chroma 路径不可相同"], self.path)
        for source in data["sources"]:
            path = Path(source["root_path"]).expanduser()
            source["root_path"] = str(
                (path if path.is_absolute() else self.path.parent / path).resolve()
            )
        return RetrievalConfig.model_validate(data)
