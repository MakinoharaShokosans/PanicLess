"""功能集成真实 SRE 运维场景集成测试（Phase 6）。

覆盖 LinuxOpsPilot 五大核心支柱在生产运维环境中的联动：
1. 真实配置安全热修全生命周期（探测 -> edit_file 局部修改 -> 自动备份 -> 校验）。
2. 系统核心敏感禁区与越界防御（/etc/shadow、/boot/vmlinuz 等拦截）。
3. SRE 权限分级矩阵与高危断网/自毁指令阻断（NETWORK_SUICIDE_RISK）。
4. 5 大 SRE 技能 SOP 动态挂载与标准化排障。
5. Compose / 配置即时 YAML 语法诊断。
6. 功能集成会话任务真实生命周期流转。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from agent_runtime.api import create_app
from agent_runtime.bootstrap import Application
from agent_runtime.guardrails.permissions import (
    PermissionPolicy,
    required_level_for,
)
from agent_runtime.llm import LLMRequest, LLMResponse
from agent_runtime.skills import SkillLimits, SkillRegistry
from agent_runtime.skills.registry import skill_roots
from baseagent_config import load_settings
from services.bash_shell.audit import audit
from tests.helpers.api_support import HEADERS, REPO_ROOT, TOKEN
from tests.helpers.gateway_support import call_response
from tools.builtin import (
    EditFileTool,
    FileLimits,
    LoadSkillTool,
    PathEscapeError,
    ViewFileTool,
    WriteFileTool,
    resolve_within_root,
)
from tools.builtin.diagnostics import check_syntax

V1 = "/api/v1"
BUILTIN_SKILLS_DIR = REPO_ROOT / "backend" / "src" / "skills"


# ==============================================================================
# 场景 1：配置安全热修全流程（探测、局部修改、会话沙盒自动备份）
# ==============================================================================


@pytest.mark.unit
async def test_ops_scenario_safe_config_change_lifecycle(tmp_path: Path) -> None:
    session_workspace = tmp_path / "storage" / "workspaces" / "sess_ops_001"
    session_workspace.mkdir(parents=True, exist_ok=True)
    backup_dir = session_workspace / "backups"

    mock_etc = tmp_path / "mock_system" / "etc" / "nginx" / "conf.d"
    mock_etc.mkdir(parents=True, exist_ok=True)
    conf_file = mock_etc / "app.conf"

    original_content = (
        "server {\n"
        "    listen 80;\n"
        "    server_name api.internal;\n"
        "    client_max_body_size 10M;\n"
        "    client_body_timeout 15s;\n"
        "}\n"
    )
    conf_file.write_text(original_content, encoding="utf-8")

    limits = FileLimits(
        view_max_lines=50,
        view_max_file_bytes=100_000,
        write_max_bytes=100_000,
    )

    # 1. 查看配置 (view_file)
    view_tool = ViewFileTool(session_workspace, limits)
    view_res = await view_tool.invoke({"path": str(conf_file)})
    assert view_res.ok
    assert "client_max_body_size 10M;" in view_res.content

    # 2. 局部热修配置 (edit_file) 并验证自动备份
    edit_tool = EditFileTool(session_workspace, limits, backup_dir=backup_dir)
    edit_res = await edit_tool.invoke(
        {
            "path": str(conf_file),
            "old_string": "    client_max_body_size 10M;\n",
            "new_string": "    client_max_body_size 50M;\n",
        }
    )
    assert edit_res.ok
    assert "替换 1 处" in edit_res.content
    assert "-    client_max_body_size 10M;" in edit_res.content
    assert "+    client_max_body_size 50M;" in edit_res.content

    # 验证磁盘生效
    updated_content = conf_file.read_text(encoding="utf-8")
    assert "client_max_body_size 50M;" in updated_content

    # 验证会话备份目录存在原始镜像快照
    backup = edit_res.meta["backup"]
    assert (
        await asyncio.to_thread(Path(backup["backup_path"]).read_text, encoding="utf-8")
        == original_content
    )


# ==============================================================================
# 场景 2：系统禁区防御（拦截 /etc/shadow、/boot、/sys 等非法访问）
# ==============================================================================


@pytest.mark.unit
async def test_ops_scenario_forbidden_system_zones_prevention(tmp_path: Path) -> None:
    session_workspace = tmp_path / "storage" / "workspaces" / "sess_ops_002"
    session_workspace.mkdir(parents=True, exist_ok=True)
    limits = FileLimits(
        view_max_lines=50,
        view_max_file_bytes=100_000,
        write_max_bytes=100_000,
    )

    for forbidden in (
        "/etc/shadow",
        "/etc/sudoers",
        "/boot/vmlinuz",
        "/sys/kernel/debug/x",
        "/proc/sys/kernel/core_pattern",
    ):
        with pytest.raises(PathEscapeError):
            resolve_within_root(session_workspace, forbidden)

        view_tool = ViewFileTool(session_workspace, limits)
        res = await view_tool.invoke({"path": forbidden})
        assert not res.ok
        assert "受保护路径" in (res.error or "") or "PathEscapeError" in (res.error or "")

        write_tool = WriteFileTool(session_workspace, limits)
        w_res = await write_tool.invoke({"path": forbidden, "content": "malicious"})
        assert not w_res.ok
        assert "受保护路径" in (w_res.error or "") or "PathEscapeError" in (w_res.error or "")


# ==============================================================================
# 场景 3：SRE 运维权限矩阵判定与断网自毁指令阻断
# ==============================================================================


@pytest.mark.unit
def test_ops_scenario_permissions_and_suicide_command_audit() -> None:
    # 1. 验证命令审计：断网与高危自毁命令硬拦截
    assert audit("ip link set eth0 down") is not None
    assert audit("ip link set eth0 down").rule == "NETWORK_SUICIDE_RISK"  # type: ignore[union-attr]
    assert audit("ifconfig eth0 down").rule == "NETWORK_SUICIDE_RISK"  # type: ignore[union-attr]
    assert audit("iptables -F").rule == "NETWORK_SUICIDE_RISK"  # type: ignore[union-attr]
    assert audit("iptables --flush").rule == "NETWORK_SUICIDE_RISK"  # type: ignore[union-attr]
    assert audit("nft flush ruleset").rule == "NETWORK_SUICIDE_RISK"  # type: ignore[union-attr]
    assert audit("rm -rf /").rule == "RM_RF_ROOT"  # type: ignore[union-attr]
    assert audit("systemctl reboot").rule == "POWER_CONTROL"  # type: ignore[union-attr]

    # 2. 验证权限策略：只读运维探测免审批，写操作受控，特权操作提权审批
    policy = PermissionPolicy(
        read_only_tools=frozenset({"view_file", "grep", "list_dir", "load_skill"}),
        workspace_write_tools=frozenset({"write_file", "edit_file"}),
        full_permission_tools=frozenset(),
        bash_tools=frozenset({"bash"}),
        full_permission_patterns=(
            r"^sudo\b",
            r"^systemctl\s+(restart|start|stop|daemon-reload|enable|disable|mask)\b",
            r"^docker\s+(run|stop|restart|rm|rmi|exec|compose\s+up|compose\s+down)\b",
            r"^(iptables|nft|ufw|firewall-cmd)\b",
            r"^kill\s+-9\b",
        ),
        workspace_write_patterns=(
            r"^systemctl\s+reload\b",
            r"^docker\s+compose\s+restart\b",
            r"^(mkdir|touch|cp|mv|rm|sed|awk|echo)\b",
        ),
        unknown_tool_level="workspace_write",
    )

    # 只读探测全放行 (read_only)
    for cmd in (
        "uptime",
        "top -b -n 1",
        "free -m",
        "df -h",
        "ss -tulpn",
        "ip -br addr",
        "systemctl status nginx",
        "docker ps -a",
        "journalctl -u nginx -n 50 --no-pager",
    ):
        lvl, act, _ = required_level_for(policy, "bash", {"command": cmd})
        assert lvl == "read_only", f"Expected read_only for: {cmd}"
        assert act == "read"

    # 平滑重载与受控写操作 (workspace_write)
    lvl, act, _ = required_level_for(policy, "bash", {"command": "systemctl reload nginx"})
    assert lvl == "workspace_write"
    assert act == "workspace_write"

    # sudo 与特权提权审批 (full_permissions)
    lvl, act, _ = required_level_for(policy, "bash", {"command": "sudo nginx -t"})
    assert lvl == "full_permissions"
    assert act == "privileged"

    lvl, act, _ = required_level_for(policy, "bash", {"command": "systemctl restart nginx"})
    assert lvl == "full_permissions"
    assert act == "global_env"


# ==============================================================================
# 场景 4：SRE 技能库 SOP 挂载与内容合规
# ==============================================================================


@pytest.mark.unit
async def test_ops_scenario_sre_skills_sop_loading() -> None:
    limits = SkillLimits(
        max_description_chars=200,
        max_triggers=12,
        high_privilege_tools=frozenset({"bash", "write_file", "edit_file"}),
        protect_trusted_names=True,
        scan_untrusted_body=True,
    )
    roots = skill_roots(
        builtin_dir=BUILTIN_SKILLS_DIR,
        global_dir=BUILTIN_SKILLS_DIR,
        workspace_root=BUILTIN_SKILLS_DIR,
        allow_builtin=True,
        allow_global=False,
        allow_workspace=False,
    )
    registry = SkillRegistry.scan(roots, limits)

    # 验证技能集合完全匹配 5 大 SRE 技能 + Shell 规范
    expected = {
        "shell_best_practices",
        "linux_troubleshooting",
        "service_management",
        "docker_container_ops",
        "safe_config_change",
        "log_analysis",
    }
    assert set(registry.names()) == expected
    assert registry.rejected == ()

    tool = LoadSkillTool(registry)

    # 验证 service_management 载入
    svc_res = await tool.invoke({"skill_name": "service_management"})
    assert svc_res.ok
    assert '<skill_sop name="service_management"' in svc_res.content
    assert "systemctl status" in svc_res.content
    assert "systemd-analyze verify" in svc_res.content

    # 验证 safe_config_change 载入
    cfg_res = await tool.invoke({"skill_name": "safe_config_change"})
    assert cfg_res.ok
    assert "动前备份" in cfg_res.content
    assert "nginx -t" in cfg_res.content

    # 验证 linux_troubleshooting 载入
    ts_res = await tool.invoke({"skill_name": "linux_troubleshooting"})
    assert ts_res.ok
    assert "top -b -n 1" in ts_res.content
    assert "free -m" in ts_res.content


# ==============================================================================
# 场景 5：YAML 配置文件语法即时诊断
# ==============================================================================


@pytest.mark.unit
def test_ops_scenario_yaml_in_process_diagnostics() -> None:
    # 损坏的 YAML
    broken_yaml = "version: '3.8'\nservices:\n  web:\n    image: [unclosed\n"
    prob = check_syntax("docker-compose.yml", broken_yaml)
    assert prob is not None
    assert "YAML 语法错误" in prob

    # 合法的 YAML
    valid_yaml = (
        "version: '3.8'\nservices:\n  web:\n    image: nginx:alpine\n    ports:\n      - '80:80'\n"
    )
    assert check_syntax("docker-compose.yaml", valid_yaml) is None
    loaded = yaml.safe_load(valid_yaml)
    assert loaded["services"]["web"]["image"] == "nginx:alpine"


# ==============================================================================
# 场景 6：功能集成真实运维任务生命周期（Zero-Workspace 会话 + 运维全流程）
# ==============================================================================


class _ScriptedGateway:
    def __init__(self) -> None:
        self.agent: list[LLMResponse | BaseException] = []
        self.requests: list[LLMRequest] = []

    async def invoke(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        item = self.agent.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


@pytest.mark.integration
async def test_ops_scenario_task_execution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-not-real-0000000000")
    loaded = load_settings(
        REPO_ROOT / "config" / "config.toml",
        {
            "app": {"home": str(tmp_path / "home")},
            "services": {"auto_start_sidecars": False},
        },
    )
    gateway = _ScriptedGateway()
    application = await Application.create(loaded, gateway=gateway)
    app = create_app(application.api_services(), token=TOKEN)

    import httpx

    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://127.0.0.1:8000",
    )

    try:
        # 1. 创建会话（去工作区，自动分配私有沙盒）
        s_res = await client.post(
            f"{V1}/sessions", json={"title": "Nginx超时故障排查"}, headers=HEADERS
        )
        assert s_res.status_code == 201
        sid = s_res.json()["session_id"]

        # 2. 准备系统服务配置
        cfg_dir = tmp_path / "mock_etc"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        conf_file = cfg_dir / "nginx.conf"
        conf_file.write_text("worker_connections 512;\n", encoding="utf-8")

        # 3. 编排 Agent 操作步骤：先 view_file 检查，再 edit_file 热修并自动备份
        gateway.agent = [
            # 第 1 步：模型调用 view_file
            call_response(
                "",
                [("c1", "view_file", {"path": str(conf_file)})],
            ),
            # 第 2 步：模型根据结果调用 edit_file
            call_response(
                "",
                [
                    (
                        "c2",
                        "edit_file",
                        {
                            "path": str(conf_file),
                            "old_string": "worker_connections 512;\n",
                            "new_string": "worker_connections 2048;\n",
                        },
                    )
                ],
            ),
            # 第 3 步：模型汇报交付
            call_response("已完成 Nginx 连接数热修并自动生成快照备份。"),
        ]

        # 4. 提交运维任务
        task_res = await client.post(
            f"{V1}/sessions/{sid}/tasks",
            json={"task_goal": "将 worker_connections 调优为 2048"},
            headers=HEADERS,
        )
        assert task_res.status_code == 202
        tid = task_res.json()["task_id"]

        # 5. 等待主循环完成
        await application.runner.wait(tid)

        # 6. 验证任务成功且文件已被修改
        t_info = (await client.get(f"{V1}/tasks/{tid}", headers=HEADERS)).json()
        assert t_info["status"] == "partial"
        assert "worker_connections 2048;\n" in conf_file.read_text(encoding="utf-8")

        # 7. 验证工作区已自动生成该配置的备份（位于 task 专属备份目录）
        backup_dir = loaded.home / "backups" / tid
        from tools.builtin.backups import list_backups

        backups = list_backups(backup_dir)
        assert len(backups) == 1
        assert "worker_connections 512;\n" in await asyncio.to_thread(
            Path(backups[0]["backup_path"]).read_text, encoding="utf-8"
        )
    finally:
        await client.aclose()
        await application.aclose()
