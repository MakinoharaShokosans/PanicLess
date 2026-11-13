"""文件工具的真实备份记录及按内容校验的恢复；不执行任意客户端路径。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from tools.builtin.paths import resolve_within_root


class BackupConflictError(Exception):
    """备份或目标已变化，不能应用原变更的回滚。"""


@dataclass(frozen=True, slots=True)
class FileBackup:
    backup_id: str
    path: str
    backup_path: str
    created_at: float
    before_hash: str
    after_hash: str
    mode: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def create_backup(directory: Path, path: Path, after: bytes) -> FileBackup:
    """每次覆盖一个独立目录，历史备份不会被后续写入复用。"""
    identity = uuid.uuid4().hex
    folder = directory / identity
    folder.mkdir(parents=True)
    target = folder / "original"
    shutil.copy2(path, target)
    record = FileBackup(
        identity,
        str(path.resolve()),
        str(target.resolve()),
        time.time(),
        digest(target.read_bytes()),
        digest(after),
        stat.S_IMODE(target.stat().st_mode),
    )
    (folder / "record.json").write_text(json.dumps(record.to_dict()), encoding="utf-8")
    return record


def load_backup(directory: Path, backup_id: str) -> FileBackup:
    if len(backup_id) != 32 or any(c not in "0123456789abcdef" for c in backup_id):
        raise FileNotFoundError("没有该备份")
    folder = directory / backup_id
    if folder.is_symlink() or not folder.resolve().is_relative_to(directory.resolve()):
        raise BackupConflictError("备份目录已变化")
    try:
        record = FileBackup(**json.loads((folder / "record.json").read_text(encoding="utf-8")))
    except (ValueError, TypeError) as exc:
        raise BackupConflictError("备份记录不可读") from exc
    original = folder / "original"
    if (
        record.backup_id != backup_id
        or original.is_symlink()
        or str(original.resolve()) != record.backup_path
    ):
        raise BackupConflictError("备份记录与存储位置不匹配")
    return record


def list_backups(directory: Path) -> list[dict[str, object]]:
    if not directory.exists():
        return []
    records = [
        load_backup(directory, path.name)
        for path in directory.iterdir()
        if len(path.name) == 32 and (path / "record.json").is_file()
    ]
    return [r.to_dict() for r in sorted(records, key=lambda r: r.created_at)]


def restore_backup(
    directory: Path, backup_id: str, root: Path, permission: str
) -> dict[str, object]:
    record = load_backup(directory, backup_id)
    path = resolve_within_root(root, record.path)
    if str(path) != record.path:
        raise BackupConflictError("原文件路径已变化")
    if permission not in ("workspace_write", "full_permissions"):
        raise PermissionError("只读任务不能回滚文件")
    if not path.is_relative_to(root.resolve()) and permission != "full_permissions":
        raise PermissionError("工作区外的文件回滚需要 full_permissions")
    if path.is_symlink() or not path.is_file():
        raise BackupConflictError("原文件已删除或不再是普通文件")
    data = Path(record.backup_path).read_bytes()
    if digest(data) != record.before_hash:
        raise BackupConflictError("备份内容已变化")
    current = digest(path.read_bytes())
    if current == record.before_hash:
        return {
            "status": "already_restored",
            "path": str(path),
            "hash": current,
            "backup_id": backup_id,
        }
    if current != record.after_hash:
        raise BackupConflictError("文件在该次修改之后又发生变化，请刷新后处理")
    fd, name = tempfile.mkstemp(prefix=".baseagent-restore-", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        tmp.chmod(record.mode)
        # 写临时文件期间有人修改了目标时，不覆盖其新内容。
        if digest(path.read_bytes()) != record.after_hash:
            raise BackupConflictError("回滚期间文件发生变化")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return {
        "status": "restored",
        "path": str(path),
        "hash": record.before_hash,
        "backup_id": backup_id,
    }
