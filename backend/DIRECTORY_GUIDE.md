# LinuxOpsPilot 后端目录结构与快速定位指南 (Backend Directory Guide)

> **定位宗旨**：无需每次跨层检索或猜测代码位置。本指南按**“架构拓扑图”**、**“数据流转生命周期”**与**“SRE 运维功能场景”**三向索引，助你在 5 秒内精准定位到后端目标文件。

---

## 1. 架构拓扑与层级全景图 (Architecture Topology)

```
                     ┌──────────────────────────────────────────────┐
                     │          HTTP / SSE 客户端 (SRE 仪表盘)       │
                     └──────────────────────┬───────────────────────┘
                                            │
                                            ▼
┌───────────────────────────────────────────────────────────────────────────────────────────┐
│ 1. API 接入层 (agent_runtime.api)                                                          │
│    routes/ (tasks, sessions, workspaces, settings, prompts, skills, mcp, system, files)    │
│    app.py (FastAPI 入口, SPA 静态托管) ── security.py / token.py (认证鉴权)                  │
│    fsguard.py (Web 目录安全围栏) ── sessions.py (Zero-Workspace 会话私有沙盒自动绑定)       │
└───────────────────────────────────────────┬───────────────────────────────────────────────┘
                                            │
                                            ▼
┌───────────────────────────────────────────────────────────────────────────────────────────┐
│ 2. 运行时编排层 (agent_runtime.runtime)                                                    │
│    runner.py (任务主协程) ── service.py (TaskService) ── active.py (活跃任务集合)           │
│    events.py (运行时事件构建) ── memory_flow.py (记忆提取流) ── factory.py (工具装配)     │
└───────────────────────────────────────────┬───────────────────────────────────────────────┘
                                            │
                      ┌─────────────────────┼─────────────────────┐
                      ▼                     ▼                     ▼
┌───────────────────────────┐ ┌───────────────────────────┐ ┌─────────────────────────────┐
│ 3. 循环内核               │ │ 4. 上下文与提示词工程     │ │ 5. 安全护栏与审批           │
│    (agent_runtime.loop)   │ │    (agent_runtime.context)│ │    (guardrails / approval)  │
│    kernel.py (ReAct 步进) │ │    prompt.py (SRE系统提示)│ │    permissions.py (SRE矩阵) │
│    tools.py (工具执行绑定)│ │    observation.py (裁剪)  │ │    binding.py (审批决策绑定)│
│    scratch.py (草稿与笔记)│ │    task_compaction.py(压缩│ │    audit.py (防自毁硬拦截)  │
└─────────────┬─────────────┘ └─────────────┬─────────────┘ └─────────────┬───────────────┘
              │                             │                             │
              └─────────────────────┬───────┴─────────────────────────────┘
                                    │
                      ┌─────────────┴─────────────┐
                      ▼                           ▼
┌───────────────────────────────────────────┐ ┌─────────────────────────────────────────────┐
│ 6. 模型网关 (agent_runtime.llm)           │ │ 7. 工具与 Sidecar (tools / services / mcps) │
│    gateway.py (多端点路由与熔断降级)      │ │    tools/builtin/ (shell, files, search...) │
│    wire.py (流式协议与 reasoning 提取)    │ │    services/bash_shell/ (独立沙箱执行终端)  │
│    structured.py (结构化输出与工具 Schema)│ │    mcps/ (MCP 管理器、协议转换与工具索引)   │
└───────────────────────────────────────────┘ └─────────────────────────────────────────────┘
                                    │
                                    ▼
┌───────────────────────────────────────────────────────────────────────────────────────────┐
│ 8. 持久化存储与总线 (memory / checkpoint / observability)                                 │
│    memory/store.py (SQLite 持久化: 会话/任务/记忆) ── checkpoint/store.py (状态快照)      │
│    observability/hub.py (EventHub 全局与单任务事件流) ── trajectory.py (因果日志 JSONL)   │
└───────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. 按“改动需求”秒查索引表 (Task-to-File Lookup)

当你需要修改后端的某项具体功能时，查阅下表直达对应文件：

| 需求场景 | 目标文件 | 关键类 / 函数 / 逻辑点 |
|---|---|---|
| **会话去工作区 (Zero-Workspace)** | [`src/agent_runtime/api/routes/sessions.py`](src/agent_runtime/api/routes/sessions.py)<br>[`src/baseagent_config/models.py`](src/baseagent_config/models.py) | `create_session()`, `workspaces_dir` 会话沙盒自动生成与目录挂载 |
| **SRE 运维命令权限矩阵判定** | [`src/agent_runtime/guardrails/permissions.py`](src/agent_runtime/guardrails/permissions.py) | `_classify_bash()`, `required_level_for()`, 只读命令免审批放行与 sudo 提权审批 |
| **防断网自毁命令审计拦截** | [`src/services/bash_shell/audit.py`](src/services/bash_shell/audit.py) | `audit()`, `_NET_DOWN`, `_FLUSH_RULES`, `NETWORK_SUICIDE_RISK` 阻断 |
| **系统路径解绑与核心禁区防御** | [`src/tools/builtin/paths.py`](src/tools/builtin/paths.py) | `resolve_within_root()`, `_FORBIDDEN_PREFIXES` (`/boot`, `/sys`, `/proc`), `_FORBIDDEN_EXACT` (`/etc/shadow`) |
| **配置文件 YAML 即时语法诊断** | [`src/tools/builtin/diagnostics.py`](src/tools/builtin/diagnostics.py) | `check_syntax()`, AST / JSON / TOML / PyYAML 解析与错误定位 |
| **SRE 生产级 SOP 技能包** | [`src/skills/`](src/skills/) | 6 个技能包： `linux_troubleshooting`, `service_management`, `docker_container_ops`, `safe_config_change`, `log_analysis`, `shell_best_practices` |
| **SRE 黄金工作法系统提示词** | [`src/agent_runtime/prompts/system.md`](src/agent_runtime/prompts/system.md) | 先诊断后动刀、动前备份、语法前检、平滑重载优先、存活探针复核 |
| **生产配置热修与自动快照备份** | [`src/tools/builtin/files.py`](src/tools/builtin/files.py) | `EditFileTool._backup()`, `WriteFileTool._backup()`, 自动镜像生成 |
| **功能集成真实 SRE 运维测试套件** | [`tests/scenarios/test_ops_scenarios.py`](tests/scenarios/test_ops_scenarios.py) | 6 大真实生产场景：配置热修、禁区防御、防断网审计、SOP 挂载、YAML 自检、功能集成全链路 |
| **Shell 命令行执行与沙箱 Sidecar** | [`src/tools/builtin/shell.py`](src/tools/builtin/shell.py)<br>[`src/services/bash_shell/`](src/services/bash_shell/) | `BashTool`, `services/bash_shell/runner.py` (独立子进程、非交互约束、日志截断) |
| **新增/修改 API 接口路由** | [`src/agent_runtime/api/routes/`](src/agent_runtime/api/routes/) | `tasks.py`, `sessions.py`, `settings.py`, `prompts.py`, `skills.py`, `mcp.py` |
| **SSE 实时事件流与全局广播** | [`src/agent_runtime/observability/hub.py`](src/agent_runtime/observability/hub.py)<br>[`src/agent_runtime/api/routes/system.py`](src/agent_runtime/api/routes/system.py) | `EventHub.publish`, `subscribe_global`, `/events` 与 `/tasks/{id}/stream` |
| **大模型网关与降级熔断** | [`src/agent_runtime/llm/gateway.py`](src/agent_runtime/llm/gateway.py)<br>[`src/agent_runtime/llm/wire.py`](src/agent_runtime/llm/wire.py) | `LlmGateway.chat()`, `chat_stream()`, 思维链解析 |
| **人机协同审批 (HITL) 决策绑定** | [`src/agent_runtime/approval/binding.py`](src/agent_runtime/approval/binding.py) | `compute_approval_id()`, `ApprovalStore` |

---

## 3. 完整目录树与各包职责 (Package Map)

```
backend/src/
├── agent_runtime/                    # 【核心 SRE Agent 运行时】
│   ├── api/                          # Web 接入层（FastAPI）
│   │   ├── app.py                    # FastAPI 实例创建、CORS、SPA 静态前端托管挂载
│   │   ├── deps.py                   # FastAPI 依赖注入（store, runner, hub 等实例）
│   │   ├── errors.py                 # 全局异常捕获与标准化 JSON 错误响应转换
│   │   ├── schemas.py                # Pydantic DTO 模型
│   │   ├── security.py / token.py    # Bearer 令牌鉴权与权限校验
│   │   ├── fsguard.py                # 前端文件浏览沙箱围栏
│   │   └── routes/                   # 各领域 REST 路由（tasks, sessions, settings, skills...）
│   │
│   ├── runtime/                      # 任务运行主流程
│   │   ├── runner.py                 # TaskRunner：单任务协程编排与生命周期管理
│   │   ├── service.py                # TaskService：提供对外统一接口
│   │   ├── events.py                 # 任务事件包装（tool.output, task.finished 等）
│   │   ├── active.py                 # 全局活跃中任务实例跟踪（取消、暂停句柄）
│   │   ├── memory_flow.py            # 任务生命周期中的记忆沉淀流动
│   │   └── factory.py                # SRE 工具装配中心与专属快照备份目录绑定
│   │
│   ├── loop/                         # Agent 主循环内核（纯净 ReAct）
│   │   ├── kernel.py                 # ReAct 循环主体：组装上下文 -> 调 LLM -> 调工具 -> 产出观察
│   │   ├── tools.py                  # 工具执行调度与超时拦截
│   │   └── scratch.py                # 运维排障规划、笔记与临时结论管理
│   │
│   ├── context/                      # 上下文工程
│   │   ├── prompt.py                 # 组合 SRE 系统提示词、工具描述、系统规则
│   │   ├── observation.py            # 工具大体积输出裁剪（保留头尾，生成持久化句柄）
│   │   └── task_compaction.py        # 历史回合压缩（折叠早前诊断日志）
│   │
│   ├── guardrails/                   # 安全护栏（确定性纯函数）
│   │   ├── permissions.py            # SRE 运维命令权限矩阵判定（只读/写/特权审批）
│   │   ├── loops.py                  # 死循环与重复调用指纹检测
│   │   └── injection.py              # 提示词注入攻击静态扫描
│   │
│   ├── approval/                     # 人工协同审批 (Human-in-the-Loop)
│   │   └── binding.py                # 生成防篡改的 approval_id 并绑定决策
│   │
│   ├── memory/                       # 记忆系统与持久化底层
│   │   └── store.py                  # SQLiteStore：核心本地数据库，存储会话、任务、消息、记忆
│   │
│   ├── observability/                # 可观测性
│   │   ├── hub.py                    # EventHub：多播事件总线（任务级与跨任务全局广播）
│   │   ├── trajectory.py             # 结构化轨迹日志（JSONL），支持后续评测回放
│   │   └── redaction.py              # 敏感信息（API Key / 密码 / 金丝雀）脱敏过滤
│   │
│   └── skills/                       # 技能注册
│       └── registry.py               # 扫描指定目录下的 SKILL.md 并导出规范
│
├── baseagent_config/                 # 【配置中心】
│   ├── models.py                     # 全局 Pydantic 配置模型（包含 workspaces_dir）
│   ├── loader.py                     # TOML 与环境变量合并加载器
│   └── retrieval.py                  # 检索配置快照、热更新与待重建管理
│
├── tools/                            # 【工具实现层】
│   ├── core/                         # 工具底座（注册表、派发器、Sidecar 客户端）
│   ├── retrieval/                    # 模型接口、Chroma/SQLite、入库、混合检索、重排、扩展与 CLI
│   └── builtin/                      # 原生内置工具集
│       ├── shell.py                  # bash 工具（向 bash_shell 服务发起执行请求）
│       ├── files.py                  # view_file, edit_file, write_file（支持系统绝对路径与自动备份）
│       ├── search.py                 # grep, list_dir（自适应上下文检索）
│       ├── paths.py                  # resolve_within_root 与核心系统禁区拦截
│       ├── diagnostics.py            # check_syntax 进程内即时语法诊断（Python/JSON/TOML/YAML）
│       └── skills.py                 # load_skill 动态挂载 SRE SOP
│
├── services/                         # 【独立 Sidecar 微服务】
│   ├── bash_shell/                   # 隔离沙箱终端服务（含 NETWORK_SUICIDE_RISK 审计拦截）
│   └── web_search/                   # 独立技术文档与 CVE 检索服务
│
├── skills/                           # 【6 个 SRE 运维技能包】
│   ├── linux_troubleshooting/        # 系统瓶颈排障（CPU/Load/内存/OOM/磁盘/网络）
│   ├── service_management/           # Systemd 守护进程治理与平滑重载
│   ├── docker_container_ops/         # Docker 容器退出诊断与 Compose 治理
│   ├── safe_config_change/           # 生产配置安全热修与语法预飞检查
│   ├── log_analysis/                 # journalctl 与 syslog 时序分析
│   └── shell_best_practices/         # 非交互与安全命令行规范
```

测试目录位于 `backend/tests/`，与 `src/` 同级：

```
backend/tests/                         # 单元、集成与实际功能测试
    ├── retrieval/                    # RAG 单元、集成与真实接口测试
    ├── scenarios/                   # 跨模块与实际运维场景
    ├── helpers/                     # 跨模块共用测试辅助代码
    └── ...                           # 各核心模块测试用例
```

---

## 4. 验证命令清单

```bash
# 1. 静态风格与语法检查
cd backend && uv run ruff check

# 2. 类型检查
uv run pyright src/

# 3. 运行默认测试（排除 real_llm 与 slow）
uv run pytest
```

RAG 使用范围、真实接口测试任务和后端交付证据见 [RAG 文档入口](../docs/rag/README.md)。
