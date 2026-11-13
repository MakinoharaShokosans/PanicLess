# LinuxOpsPilot 后端

LinuxOpsPilot SRE 运行时、HTTP API 与两个独立安全 sidecar（`bash_shell`、`web_search`）。

> 📖 **快速定位与修改指南**：详见 [**`DIRECTORY_GUIDE.md`**](DIRECTORY_GUIDE.md)，包含三层架构拓扑、SRE 运维时序图以及按修改需求的秒查索引表。

## 目录与功能结构

```
src/
├── agent_runtime/     SRE 运行时核心：
│                      core / loop (ReAct 步进) / checkpoint (状态快照) /
│                      guardrails (权限矩阵与防自毁) / approval (人机协同审批) /
│                      context (提示词工程与观察值蒸馏) / memory (SQLite 会话持久化) /
│                      skills (SOP 技能扫描) / runtime / api (REST / SSE 接口)
├── baseagent_config/  配置加载与校验（pydantic，支持零工作区 workspaces_dir）
├── tools/             工具层：core、builtin，以及 retrieval（知识库入库与混合检索）
├── mcps/              MCP 外部工具扩展接入
├── services/          独立隔离进程：bash_shell (含 NETWORK_SUICIDE_RISK 审计)、web_search
├── skills/            6 个内置 SRE 技能包（含 Shell 规范）
└── evaluation/        评测框架（baseagent-bench）
tests/                 单元、集成与实际功能测试；retrieval/ 存放 RAG 测试
```

## 核心特性
- **Zero-Workspace**：会话自动在 `storage/workspaces/<session_id>` 建立独立沙盒，无需手动指定或切换工作区；
- **SRE 权限矩阵**：放行常用只读诊断命令（`top`, `df`, `ss`, `ip`, `systemctl status` 等），`sudo` 映射特权审批，拦截断网自毁命令；
- **系统路径解绑与核心禁区保护**：放行 `/etc/`、`/var/log/`、`/tmp/` 等合法路径，拦截 `/boot`、`/sys`、`/proc`、`/etc/shadow` 等核心系统禁区；
- **SRE 黄金工作法五项纪律**：提示词强制约束“先诊断后动刀、动前备份、语法前检、平滑重载优先、存活复核”。

## 开发与测试

以下命令从仓库根目录执行，测试状态以实际输出为准。

```bash
make install     # uv sync 同步依赖
make lint        # ruff check + pyright + import-linter 静态校验
make test        # 运行前后端离线测试（后端默认排除 real_llm 与 slow）
```

## 运维知识检索

终端执行、暂停/恢复、文件回滚、主机指标及操作设置接口，见[真实数据接入报告](../docs/后端真实数据接口接入报告.md)。

已实现模型 HTTP 接口、Chroma/SQLite、文档入库、BM25 与向量混合检索、重排和查询期上下文扩展。所有会话通过 `knowledge_search` 使用同一全局知识库，也可通过独立 CLI 管理。日志查询未包含在知识检索链路中。

配置、任务与验证证据见 [RAG 文档入口](../docs/rag/README.md)。

知识工具与运行时的直接测试命令见 [RAG 验证入口](../docs/rag/README.md#验证入口)，临时数据和真实模型验证约定见[测试产物清理](../docs/testing/测试产物清理.md)。
