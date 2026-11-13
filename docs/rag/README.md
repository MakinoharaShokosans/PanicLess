# 运维知识库

知识库供所有会话的 Agent 使用，不区分工作区、公共或私有资料。UTF-8 Markdown/TXT 资料经切块、Embedding 后写入本地 Chroma，文档元数据与原文快照保存在 SQLite。查询依次经过向量与 BM25 召回、RRF 融合、可选 Rerank 和上下文扩展。

## 配置

编辑 [config/config.toml](../../config/config.toml) 的现有配置段。默认 `knowledge_enabled = false`，Embedding/Rerank 也默认关闭，来源列表为空。常用默认值如下：

| 配置 | 默认值 |
| --- | --- |
| 数据目录 | `retrieval/store`，相对应用 home |
| SQLite / Chroma | `metadata.sqlite3` / `chroma`，相对数据目录 |
| 切片 / 重叠 | 1200 / 150 字符 |
| Embedding 批次 | 16 |
| 向量 / BM25 召回 | 各 20 条 |
| RRF k / 重排候选 | 60 / 20 |
| 返回数量 / 上限 | 5 / 10 |
| 扩展 | 同章节前后各 1 片段，单块 3600、总正文 12000 字符 |
| 查询 / 入库超时 | 15 / 600 秒 |

启用时，在现有 `[retrieval]` 中设置：

```toml
knowledge_enabled = true
sources = [{ source_id = "ops-manuals", root_path = "../docs/运维手册" }]
```

资料路径相对配置文件所在目录解析。将现有 `[retrieval.embedding]` 的 `enabled` 改为 `true`；需要重排时也开启 `[retrieval.rerank]`。两段均已有正式 HTTP 客户端配置：

- Embedding：`https://api.openlux.ai/v1/embeddings`，模型 `text-embedding-3-large`。
- Rerank：`https://api.openlux.ai/v1/rerank`，模型 `qwen3-rerank`。
- 两者默认读取环境变量 `PANICLESS_RAG_API_KEY`；可分别修改 `endpoint_url`、`model`、`api_key_env`。

应用启动命令会读取仓库根目录的 `.env`。独立知识管理 CLI 不自动读取 `.env`，运行入库或模型检索前，需要在其进程环境中设置 `PANICLESS_RAG_API_KEY`。

RAG 已启用但缺少 Key 时，配置加载只告警，不阻止应用启动。模型客户端调用前检查凭据；已有索引可降级到 BM25，重排不可用时保留混合检索结果并说明原因。入库仍需要可用的 Embedding 服务，失败不会写入假向量或替换旧活动索引。聊天模型的启动校验独立于 RAG，缺聊天模型凭据仍可能阻止启动。

## 配置变更

查询前刷新配置。召回、重排、扩展、限额及开关等普通参数下一次请求生效，在途请求保持自己的配置和索引快照；非法变更保留旧值并记录错误。

以下字段修改后进入待重建状态，通过 `status` 查看，`rebuild` 成功后切换：

- `persist_dir`、`sqlite_path`、`chroma_path`。
- `embedding_revision`、`preprocessing_version`、`chunker_version`。
- `chunk_chars`、`overlap_chars`。
- `embedding.protocol`、`embedding.endpoint_url`、`embedding.model`。

Rerank 全部配置支持热更新。来源变更不自动扫描文件，需要执行 `index`；旧工作区版本的来源配置应移除 `workspace_id`，旧索引需从原资料执行一次 `rebuild` 建立全局索引。

## 启动与管理

以下命令在项目根目录执行，使用已安装依赖的 `backend/.venv`：

```bash
# 启动后端及 sidecar
PYTHONPATH=backend/src backend/.venv/bin/python -m agent_runtime.cli --config config/config.toml start --no-frontend

# 查看索引及待重建状态
PYTHONPATH=backend/src backend/.venv/bin/python -m tools.retrieval --config config/config.toml status

# 入库 / 更新指定来源；内容未变化时跳过
PYTHONPATH=backend/src backend/.venv/bin/python -m tools.retrieval --config config/config.toml index --source-id ops-manuals

# 完整检索，可省略 top-k 和 source-id
PYTHONPATH=backend/src backend/.venv/bin/python -m tools.retrieval --config config/config.toml search --query "磁盘空间不足如何排查" --top-k 5 --source-id ops-manuals

# 完整扫描成功后清理源目录中已缺失文件的索引记录
PYTHONPATH=backend/src backend/.venv/bin/python -m tools.retrieval --config config/config.toml index --source-id ops-manuals --prune

# 替换为检索结果中的实际 doc_id；只删索引，不删源文件
PYTHONPATH=backend/src backend/.venv/bin/python -m tools.retrieval --config config/config.toml delete --doc-id '<doc_id>'

# 重建全部配置来源，成功后发布新索引代
PYTHONPATH=backend/src backend/.venv/bin/python -m tools.retrieval --config config/config.toml rebuild

# 本地 SQLite 与 Chroma 写入、查询、重开检查，无模型请求
PYTHONPATH=backend/src backend/.venv/bin/python -m tools.retrieval --config config/config.toml health
```

本地库目前由单进程独占。应用首次检索打开库后，独立管理 CLI 需等应用关闭并释放资源；尚无应用进程内的知识管理 API。删除索引后再扫描仍存在的原文件，会重新入库。

## Agent 使用

已接入 `knowledge_search`，内部完成整个查询链路。参数只有：

- `query`：必填的问题或故障现象。
- `source_ids`：可选来源 ID 数组；省略搜索所有配置来源，空数组表示不选来源。
- `top_k`：可选原始命中数量，默认 5，上限由 `max_top_k` 控制。

结果含扩展正文、文档/章节/片段 ID、来源行号与引用，以及 `ok`、`degraded`、`not_indexed`、`no_results` 或错误状态。全部检索通道失败会报告失败，不当作无命中。Agent 不可调整模型配置或执行入库、删除、重建。知识用于参考排查方法，服务器状态和日志仍由实时工具取得。

## 验证入口

已有直接覆盖 [KnowledgeSearchTool](../../backend/tests/retrieval/test_knowledge_tool.py) 和 [RuntimeRetrieval](../../backend/tests/runtime/test_runtime_retrieval.py) 的离线测试，涵盖参数、引用与状态、延迟打开、共享实例、重试、关闭和配置刷新。

```bash
PYTHONDONTWRITEBYTECODE=1 backend/.venv/bin/python -m pytest -q -p no:cacheprovider backend/tests/config backend/tests/retrieval/test_knowledge_tool.py backend/tests/runtime/test_runtime_retrieval.py
```

[全局知识集成测试](../../backend/tests/retrieval/test_global_knowledge.py) 使用本地真实 Chroma/SQLite 与模拟模型响应，覆盖跨会话使用同一知识、缺 Key 降级、删除和失败保留活动索引：

```bash
PYTHONDONTWRITEBYTECODE=1 backend/.venv/bin/python -m pytest -q -p no:cacheprovider backend/tests/retrieval/test_global_knowledge.py backend/tests/retrieval/test_service_lifecycle.py
```

以上命令无需真实模型 Key。历史执行结果与环境限制见[后端执行报告](../运维RAG后端执行报告.md)，真实接口验证入口见[测试说明](../testing/测试产物清理.md)。模拟通过不代表真实模型联调或检索质量评估已完成。
