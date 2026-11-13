# 运维 RAG 后端修正报告

日期：2026-11-13。

## 本轮结果

按最新指令，知识库统一为全局知识库，供所有 Agent 使用。

- 移除来源配置的 `workspace_id`，不引入公共/私有、visibility 或共享授权。
- Agent、运行时和管理 CLI 使用同一个全局索引；新会话不需要绑定资料工作区。
- 保持原有单索引快照、向量 + BM25、RRF、一次 Rerank、动态扩展链路。
- 撤销本轮中途添加的多工作区快照、多向量空间融合和多索引代返回字段。
- 启用 RAG 但缺少其 Key 时，应用启动只告警。配置仍可热更新；正式模型客户端在调用前检查凭据。
- 已有索引在 Embedding 不可用时降级为 BM25，Rerank 不可用时保留融合结果，返回明确降级原因。
- 新资料入库仍需要可用的 Embedding 服务，失败不会发布假向量或破坏旧活动索引。
- 修复模型客户端关闭异常导致快照计数未释放，以及取消本地存储打开时可能遗失资源的问题。

## 主要修改

| 文件 | 用途 |
| --- | --- |
| `backend/src/baseagent_config/models.py` | 全局来源配置，仅要求唯一 source_id；保留正式模型默认值 |
| `backend/src/baseagent_config/loader.py`、`retrieval.py` | 缺 RAG Key 只告警，允许配置加载和刷新 |
| `backend/src/tools/retrieval/service.py` | 全局入库/检索/管理入口，快照和取消资源释放修复 |
| `backend/src/tools/retrieval/chunking.py`、`indexing.py`、`records.py` | 统一使用固定全局索引键，保留现有数据库 schema |
| `backend/src/tools/retrieval/search.py`、`tool.py` | 移除工作区来源过滤，所有 Agent 使用全局来源 |
| `backend/src/tools/retrieval/cli.py` | 管理命令不再要求 --workspace-id |
| `backend/src/agent_runtime/runtime/retrieval.py`、`factory.py` | 实际 Agent 装配不再传知识工作区 |
| `config/config.toml` | 更新全局来源示例及缺 Key 行为说明 |
| `backend/tests/retrieval/`、`backend/tests/runtime/test_runtime_retrieval.py` | 全局调用链、工具、生命周期、配置降级覆盖；既有回归适配新签名 |

本轮没有新增依赖，未修改前端或聊天模型网关。

## 配置与使用

配置位置为 `config/config.toml`。模型默认值仍为现有配置中的 OpenLux 地址、`text-embedding-3-large`、`qwen3-rerank`，凭据变量为 `PANICLESS_RAG_API_KEY`。默认关闭，填写真实凭据后按需启用 Embedding/Rerank；缺凭据不会阻止应用启动，但模型通道不可用。

在 `[retrieval]` 中设置 `knowledge_enabled = true`，将 `sources = []` 改为：

```toml
sources = [{ source_id = "ops-manuals", root_path = "../docs/运维手册" }]
```

无需 workspace_id 或其他可见性配置。所有会话自动使用这些来源。Agent 工具参数仍只有 `query`、可选 `source_ids`、可选 `top_k`。

以下命令从项目根目录执行：

```bash
# 启动后端及 sidecar，不启动前端
PYTHONPATH=backend/src backend/.venv/bin/python -m agent_runtime.cli --config config/config.toml start --no-frontend

# 本地管理命令
PYTHONPATH=backend/src backend/.venv/bin/python -m tools.retrieval --config config/config.toml index --source-id ops-manuals
PYTHONPATH=backend/src backend/.venv/bin/python -m tools.retrieval --config config/config.toml status
PYTHONPATH=backend/src backend/.venv/bin/python -m tools.retrieval --config config/config.toml search --query "磁盘空间不足如何排查"
PYTHONPATH=backend/src backend/.venv/bin/python -m tools.retrieval --config config/config.toml delete --doc-id '<search 命中的 doc_id>'
PYTHONPATH=backend/src backend/.venv/bin/python -m tools.retrieval --config config/config.toml rebuild
PYTHONPATH=backend/src backend/.venv/bin/python -m tools.retrieval --config config/config.toml health
```

delete 的文档 ID 取自 search 命中。现有本地存储的进程独占规则仍然有效：应用首次打开检索库后，独立管理 CLI 需等该进程关闭释放资源。

历史配置和索引：旧来源配置应删除 workspace_id，旧 CLI 命令应删除 --workspace-id。旧按工作区生成的索引没有被删除或自动合并；需要用现有原始资料执行一次 rebuild 建立全局索引。重建需要真实 Embedding 凭据，本轮未对正式资料执行此操作。数据库原有 workspace 列仅作为兼容存储字段保留，全局运行固定使用 `global`，不再提供知识隔离或共享机制。

## 实际验证

模型响应全部来自独立 `httpx.MockTransport`，没有真实模型请求；集成验证实际运行本地 Chroma 和 SQLite。

| 命令/检查 | 结果 |
| --- | --- |
| `PYTHONDONTWRITEBYTECODE=1 backend/.venv/bin/python -m pytest -q -p no:cacheprovider backend/tests/config backend/tests/retrieval/test_knowledge_tool.py backend/tests/runtime/test_runtime_retrieval.py backend/tests/retrieval/test_service_lifecycle.py` | 退出 0，150 passed |
| `PYTHONDONTWRITEBYTECODE=1 backend/.venv/bin/python -m pytest -q -p no:cacheprovider backend/tests/retrieval/test_client_faults.py` | 退出 0，59 passed |
| `PYTHONDONTWRITEBYTECODE=1 timeout 100s backend/.venv/bin/python /tmp/panicless_global_checks.py -q -p no:cacheprovider backend/tests/retrieval/test_global_knowledge.py backend/tests/retrieval/test_service_boundaries.py` | 退出 0，9 passed，2 个真实模型测试未运行 |
| 增强为实际创建两个工作区/会话并装配工具后，重跑 `test_global_knowledge.py` | 退出 0，4 passed |
| CLI 主帮助及 search 帮助 | 退出 0，无 --workspace-id 参数 |
| Ruff、Pyright、`git diff --check` | 通过；Pyright 0 errors、0 warnings |
| 后端 `PYTHONDONTWRITEBYTECODE=1 .venv/bin/lint-imports --no-cache` | 退出 0，7 contracts kept |

独立 runner 的必要性：此执行环境中，普通异步集成命令在跨线程唤醒处挂起，分别在 50/45 秒超时退出 124。`/tmp/panicless_global_checks.py` 仅给验证进程的事件循环添加 20ms 定时唤醒，未改生产事件循环或正式测试逻辑。Pyright 使用 `/tmp/panicless_global_pyright.json` 显式解析当前后端虚拟环境依赖。

验证涵盖：启用 RAG 无 Key 的真实 Application 创建/关闭；本地入库和重开；两个工作区会话的实际工具装配读取同一文档；重排与引用；缺凭据 BM25 降级且不发 HTTP；入库失败保留旧代；热更新；精确删除后无命中；关闭异常和打开取消释放资源。

## 清理与剩余事项

新增全局集成测试只使用 pytest 临时目录，结束前精确删除其文档索引记录并确认文档数为零，fixture 清理临时配置、原文和存储。没有 reset 正式库，也没有改动原有运维手册或正式索引。

当前全局知识库与缺 Key 修正已实现并验证。以下原有缺口不在本次修正中假称完成：交互终端接管、文件版本回滚、看板实时数据接口、应用进程内知识管理 API/UI，以及仓库其他失效文档链接。真实模型联调和旧正式资料的全局重建也未执行。

## 配置体验与验证文档补齐（2026-11-13）

再次核对问题 5、6 后，缺 RAG Key 阻止启动的问题在当前代码中已修复；本轮没有重复改写加载器，也没有改变接口结构。`KnowledgeSearchTool` 与 `RuntimeRetrieval` 的直接用例已存在，本轮重新执行验证。

本轮修改：

- `backend/tests/config/test_config_loader.py`：补充缺失、空字符串、空白 Key 与模型通道开关的组合验证；确认启用通道只告警、填入凭据后告警消失、非法配置仍报错。
- `backend/tests/api/test_api_files_system.py`：补充 `/api/v1/models` 空列表原样返回断言，确认后端不补入预设模型。
- 新增 `docs/INDEX.md`、`docs/rag/README.md`，补齐根 README 已引用的入口；说明实际默认配置、凭据位置、CLI、全局知识工具、冷热更新与测试命令。
- 修正 `backend/README.md` 中“Agent 搜索未接入”的过期说明及失效测试链接。没有恢复此前已删除的旧设计文档。

本轮实际验证：

```bash
PYTHONDONTWRITEBYTECODE=1 timeout 55s backend/.venv/bin/python -m pytest -q -p no:cacheprovider backend/tests/config backend/tests/retrieval/test_knowledge_tool.py backend/tests/runtime/test_runtime_retrieval.py backend/tests/retrieval/test_service_lifecycle.py
# 退出 0：157 passed

PYTHONDONTWRITEBYTECODE=1 timeout 55s backend/.venv/bin/python -m pytest -q -p no:cacheprovider backend/tests/retrieval/test_global_knowledge.py backend/tests/api/test_api_files_system.py::test_health_dependencies_and_introspection
# 退出 124：两个同步用例后异步等待超时

PYTHONDONTWRITEBYTECODE=1 timeout 100s backend/.venv/bin/python /tmp/panicless_global_checks.py -q -p no:cacheprovider backend/tests/retrieval/test_global_knowledge.py backend/tests/api/test_api_files_system.py::test_health_dependencies_and_introspection
# 使用前述临时定时唤醒 runner，退出 0：5 passed
```

合计 162 个定向用例通过，其中异步集成用例需要前述环境 runner，不声称普通集成命令已直接通过。覆盖真实 Application 无 RAG Key 创建/关闭、全局知识入库/跨会话查询/降级/删除，以及模型列表 API。模型响应均为显式模拟，Chroma/SQLite 为本地真实运行，没有真实模型请求。Ruff 检查、格式检查及 `git diff --check` 均通过；新增导航和 RAG 文档的本地链接目标已检查。

超时用例遗留的 `/tmp/pytest-of-Skualeilu/pytest-14/test_application_starts_with_e0` 已核对专用模拟凭据变量和临时手册内容后清理。成功集成用例自行清理其资料和索引；正式资料与数据库未修改。

仍需前端处理：`frontend/src/features/settings/sections/Models.tsx` 使用 `FALLBACK_MODELS`，空列表及加载中都会展示预设模型；该页面也仍只有展示能力。本 Session 遵守后端边界，未修改前端。后端 `/api/v1/models` 当前是实际已装配聊天模型的只读列表，不是模型配置写入接口；本轮没有新增未经约定的模型编辑 API。前端可先移除预设列表，区分加载、空列表、请求错误；配置编辑交互及写入契约由决策/前端 Session 另行协调。
