"""真实文件备份/恢复；临时目录内执行，不触碰系统资料。"""

from pathlib import Path

import pytest

from tools.builtin.backups import BackupConflictError, list_backups, restore_backup
from tools.builtin.files import EditFileTool, FileLimits, WriteFileTool

pytestmark = pytest.mark.unit
LIMITS = FileLimits(100, 4096, 4096)


@pytest.mark.parametrize("kind", [WriteFileTool, EditFileTool])
async def test_change_has_real_backup_and_restore_preserves_bytes_and_mode(tmp_path, kind):
    root = tmp_path / "workspace"
    root.mkdir()
    path = root / "file.txt"
    original = b"\xef\xbb\xbfhello\r\n"
    path.write_bytes(original)
    path.chmod(0o640)
    directory = tmp_path / "backups"
    args = {"path": "file.txt", "content": "changed\n"}
    if kind is EditFileTool:
        args = {"path": "file.txt", "old_string": "hello", "new_string": "changed"}
    result = await kind(root, LIMITS, backup_dir=directory).invoke(args)
    assert result.ok
    record = result.meta["backup"]
    assert Path(record["backup_path"]).read_bytes() == original  # noqa: ASYNC240
    assert record["path"] == str(path) and record["created_at"] > 0
    assert list_backups(directory) == [record]
    restored = restore_backup(directory, record["backup_id"], root, "workspace_write")
    assert restored["status"] == "restored"
    assert path.read_bytes() == original and path.stat().st_mode & 0o777 == 0o640
    assert (
        restore_backup(directory, record["backup_id"], root, "workspace_write")["status"]
        == "already_restored"
    )


async def test_each_change_keeps_its_backup_and_conflicts_do_not_overwrite(tmp_path):
    path = tmp_path / "file.txt"
    path.write_text("v1")
    directory = tmp_path / "backups"
    tool = WriteFileTool(tmp_path, LIMITS, backup_dir=directory)
    first = (await tool.invoke({"path": "file.txt", "content": "v2"})).meta["backup"]
    second = (await tool.invoke({"path": "file.txt", "content": "v3"})).meta["backup"]
    assert first["backup_id"] != second["backup_id"]
    with pytest.raises(BackupConflictError):
        restore_backup(directory, first["backup_id"], tmp_path, "workspace_write")
    assert path.read_text() == "v3"
    restore_backup(directory, second["backup_id"], tmp_path, "workspace_write")
    restore_backup(directory, first["backup_id"], tmp_path, "workspace_write")
    assert path.read_text() == "v1"


async def test_missing_tampered_backup_and_changed_target_are_errors(tmp_path):
    path = tmp_path / "file.txt"
    path.write_text("before")
    directory = tmp_path / "backups"
    result = await WriteFileTool(tmp_path, LIMITS, backup_dir=directory).invoke(
        {"path": "file.txt", "content": "after"}
    )
    record = result.meta["backup"]
    with pytest.raises(FileNotFoundError):
        restore_backup(directory, "0" * 32, tmp_path, "workspace_write")
    path.write_text("another change")
    with pytest.raises(BackupConflictError):
        restore_backup(directory, record["backup_id"], tmp_path, "workspace_write")
    assert path.read_text() == "another change"
    path.write_text("after")
    Path(record["backup_path"]).write_text("tampered")  # noqa: ASYNC240
    with pytest.raises(BackupConflictError):
        restore_backup(directory, record["backup_id"], tmp_path, "workspace_write")
    assert path.read_text() == "after"


async def test_read_only_and_outside_workspace_restore_are_rejected(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    path = tmp_path / "outside.txt"
    path.write_text("before")
    directory = tmp_path / "backups"
    result = await WriteFileTool(workspace, LIMITS, backup_dir=directory).invoke(
        {"path": str(path), "content": "after"}
    )
    identity = result.meta["backup"]["backup_id"]
    for level in ("read_only", "workspace_write"):
        with pytest.raises(PermissionError):
            restore_backup(directory, identity, workspace, level)
    assert path.read_text() == "after"
    assert (
        restore_backup(directory, identity, workspace, "full_permissions")["status"] == "restored"
    )


async def test_replaced_target_symlink_cannot_redirect_restore(tmp_path):
    path = tmp_path / "file.txt"
    path.write_text("before")
    directory = tmp_path / "backups"
    result = await WriteFileTool(tmp_path, LIMITS, backup_dir=directory).invoke(
        {"path": str(path), "content": "after"}
    )
    other = tmp_path / "other.txt"
    other.write_text("after")
    path.unlink()
    path.symlink_to(other)
    with pytest.raises(BackupConflictError):
        restore_backup(directory, result.meta["backup"]["backup_id"], tmp_path, "workspace_write")
    assert other.read_text() == "after"
