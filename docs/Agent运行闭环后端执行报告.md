# Agent 运行闭环后端执行报告

日期：2026-11-13。范围：后端运行时、存储、接口、测试及配置说明。未修改前端。

## 完成情况

| 问题 | 本轮实现 |
| --- | --- |
| 结束不等于完成 | 增加显式任务结论；不调用工具只代表本轮结束，未报告完成情况时记为 `partial`。未完成计划、未验证改动不能声明全部完成。 |
| 验证过于粗糙 | 增加目标、检查内容、结果、证据调用 ID 和改动版本记录。普通 Bash 成功不再自动通过验证；修改文件、已识别的 Bash 写入/服务操作会使旧验证失效。 |
| 运行中纠正方向 | 增加持久消息收件箱，在下一次模型调用前合入；保留原任务 ID 和目标，支持暂停中、等待审批时提交。 |
| 子 Agent 一次性委派 | `spawn_subagent` 返回 `subtask_id`，支持同一子任务续问；逐条保存原始观察，超时、提炼失败返回中间发现和全文引用。 |
| 压缩丢失关键证据 | 目标、事实、排除假设、计划、验证及产物位置独立于摘要保留；新增原始消息回读工具，摘要失败后仍能核对反证。 |
| 长期记忆只有最近记录 | SQLite 独立归档历史案例，按当前问题的文本相关性检索，跨会话使用；稳定经验与历史现场观察分开呈现，并带时间与来源。 |
| 恢复粒度粗 | 新增逐调用 SQLite 日志，记录 `queued/running/completed`。恢复复用已完成结果，只执行明确尚未开始的调用；取消也保留真实结果。 |

七项后端代码均已实现，没有核心逻辑占位。没有新增依赖、审查 Agent、私有/公共知识库划分或外部服务。

## 任务结果与验证契约

新增模型工具：

- `report_outcome(status, summary, remaining)`：`status` 为 `completed / partial / needs_input / blocked`。
- `record_verification(goal, check, result, summary, evidence_call_ids)`：`result` 为 `passed / failed / inconclusive`。

任务 API 的 `status` 保留既有 `succeeded` 表示明确完成，并新增 `partial / needs_input / blocked`。`failed` 仍表示运行错误，`cancelled`、`terminated` 保持原语义。

`GET /api/v1/tasks/{task_id}` 与 `task.finished` 事件增加：

```json
{
  "status": "succeeded",
  "conclusion": {
    "status": "completed",
    "summary": "文件已写入并回读验证",
    "remaining": []
  },
  "verification_records": [{
    "goal": "创建内容为 ready 的文件",
    "check": "回读 verified.txt",
    "result": "passed",
    "summary": "文件内容为 ready",
    "evidence_call_ids": ["read"],
    "revision": 1
  }]
}
```

运行时会检查证据调用确实存在、来自当前改动版本、不是修改动作本身；失败调用不能作为通过证据。当前版本同一目标的最新检查决定该目标验证状态。再次修改后必须补新检查。

运行时核对结构和证据关联，检查是否足以证明业务目标仍由主 Agent 判断；没有新增语义裁判。Bash 是否会修改状态沿用现有权限分类规则，不声称能静态识别任意脚本的所有副作用。

未调用 `report_outcome`、仅回复“无法判断”或普通结束，均不会默认成功。`partial/needs_input/blocked` 是本轮终态；后续可在同一会话提交新任务继承上下文。既有 `/resume` 继续用于运行错误/进程中断，不用于重启已结束轮次。

## 运行中消息

```http
POST /api/v1/tasks/{task_id}/messages
Content-Type: application/json

{"content":"刚才端口说错了，是 8081；先不要重启"}
```

返回 HTTP 202，包含 `task_id/message_id/content/created_at/status=queued`。

`GET /api/v1/tasks/{task_id}/messages` 返回消息列表，每条状态为 `queued` 或 `consumed`。空消息/超长消息沿用输入校验；不存在的任务返回 404，已结束任务返回 409。

当前正在执行的工具不会被强行中断；在下一次模型调用前消费消息。消费游标与任务状态一起提交，重启不丢失、不重复合入。最终回复期间提交的消息也会触发下一轮，入队与任务收尾互斥。补充消息不会跳过审批。

## 子任务与原始证据

新建子任务仍使用原工具：

```json
{"role":"reader","goal":"检查 nginx 配置","assigned_tools":["view_file","grep"]}
```

同一父任务中续问：

```json
{"subtask_id":"sub_返回的ID","goal":"结合刚才发现，再核对 upstream 配置"}
```

续问保留历史消息、访问证据及原授权工具。每个子任务保存 JSON 状态和独立 `evidence.txt`，位于配置的产物目录下 `{parent_task_id}/subagents/`；通过返回的 `artifact_id` 使用 `read_artifact` 回读。证据文件不包含系统提示词和 reasoning 载荷。

超时或报告提炼失败时返回部分成果和未解决项。中断后无法确认完成的操作不会自动重跑。子任务记录由父任务目录管理；当前不提供跨父任务续问或后台异步子 Agent 调度。

主 Agent 新增：

```json
{"start":0,"limit":10,"query":"connection refused","call_id":"可选调用ID"}
```

上述参数传给 `read_task_record`。支持 `offset/max_chars` 正文分页；可按调用 ID 或关键词核对未压缩原始记录。工具输出原来被裁剪时，通过记录中的 `artifact://` 引用读全文。

## 记忆与恢复

- `note(kind="experience", text=...)` 用于明确、可复用的稳定经验；普通 `fact` 视为历史观察。
- 历史案例按当前目标检索，默认返回最多 5 条。采用本地词项相关性评分，支持英文词项和中文二元词项，无新增模型请求。
- 案例独立于会话摘要的数量淘汰；删除所属会话时随之清理。历史记录不会被当成当前服务器状态。
- 普通事实时间标注为任务收尾记录时间，现场采样时间以原始工具证据为准；压缩器提炼的事实明确标注提炼时间及原采样时间未知，不伪装成新采集的数据。
- `AgentState` 升至 schema 5；新增字段使用简单迁移填充。旧的“Bash 成功即验证通过”状态不会作为新验证证据继承。
- 收件箱保存在原检查点库；逐调用日志位于 `<checkpoint_db>.calls.sqlite3`。
- 完成结果包含完整 `ToolResult` 并立即提交。进程在副作用发生后、结果落盘前崩溃时，该调用仍是“结果未知”；无法把跨进程副作用与本地 SQLite 合成一个原子事务。其他已完成调用的结果不会因此丢失。
- 删除任务同步删除收件箱及逐调用日志；本轮无须清空已有数据库。测试资料、数据库与产物均使用临时目录，没有向正式知识库导入模拟资料。

## 主要修改文件

- `backend/src/agent_runtime/core/{state,codec}.py`：结论、验证、观察、消息游标与版本迁移。
- `backend/src/agent_runtime/loop/{kernel,tools,scratch,conclusion}.py`：任务结果、显式验证、逐调用日志接线与经验笔记。
- `backend/src/agent_runtime/checkpoint/{store,recovery,tool_journal}.py`：收件箱、执行状态、恢复及取消结果。
- `backend/src/agent_runtime/runtime/{runner,service,resume,factory,events,memory_flow}.py`：生命周期、API 投影、模型边界与记忆接线。
- `backend/src/agent_runtime/api/{schemas.py,routes/tasks.py}`：新增字段及消息入口。
- `backend/src/agent_runtime/subagents/delegate/{tool,runner,store}.py`：子任务持久化、续问、中间成果。
- `backend/src/agent_runtime/context/{prompt,task_compaction}.py`、`prompts/task_compactor.md`：独立证据账本和压缩提示。
- `backend/src/tools/builtin/task_record.py`：原始记录回读。
- `backend/src/agent_runtime/memory/{models,deposit,store,experience}.py`：经验分类、时间、历史案例检索。
- `backend/src/baseagent_config/models.py`、`config/config.toml`：新增只读工具与子 Agent 工具边界默认配置保持一致。
- `backend/src/evaluation/agent_bench/{cases,runner}.py`：识别新增终态，避免轮询已经结束的任务。
- `.env.example`：补齐已有 RAG 凭据变量的说明，未填写任何密钥。
- 相关 API、运行时、子 Agent、记忆、场景测试：新增回归及更新旧的自动成功预期；备份场景改为读取真实备份元数据。

## 验证

模型均使用脚本或 mock；SQLite、文件写入/回读、检查点重开、应用装配和 ASGI 接口实际运行。未请求真实模型，未重启当前业务进程。

当前执行环境的 asyncio 跨线程唤醒存在停滞；独立测试进程临时使用 1ms 唤醒计时器，未修改生产事件循环。测试辅助入口为 `/tmp/panicless_runtime_checks.py`；硬退出子进程使用 `/tmp/panicless-test-wakeup/sitecustomize.py` 继承同一辅助逻辑。

实际执行：

```bash
PYTHONDONTWRITEBYTECODE=1 backend/.venv/bin/python /tmp/panicless_runtime_checks.py \
  -q -p no:cacheprovider \
  backend/tests/api backend/tests/core backend/tests/runtime backend/tests/memory \
  backend/tests/context backend/tests/subagents backend/tests/observability \
  backend/tests/config backend/tests/tools/test_task_record.py \
  -k 'not hard_process_kill' --tb=short

PYTHONDONTWRITEBYTECODE=1 backend/.venv/bin/python /tmp/panicless_runtime_checks.py \
  -q -p no:cacheprovider backend/tests/scenarios backend/tests/evaluation \
  -k 'not mcp and not deferred' --tb=short

PYTHONPATH=/tmp/panicless-test-wakeup:/home/Skualeilu/Projects/PanicLess/backend/src \
  PYTHONDONTWRITEBYTECODE=1 timeout 20s backend/.venv/bin/python -m pytest \
  -q -p no:cacheprovider backend/tests/runtime/test_checkpoint_store.py \
  -k hard_process_kill --tb=short
```

在正常运行环境中，以上辅助入口可以直接替换为 `backend/.venv/bin/python -m pytest`，其余参数相同。

以下最终验证共覆盖 1051 个不同用例；额外的小范围重跑未重复计数。

| 检查 | 结果 |
| --- | --- |
| API/core/runtime/memory/context/subagents/observability/config/回读工具 | 916 passed，1 deselected，34.50s，退出码 0 |
| 本地场景与评测框架 | 134 passed，7 deselected，17.45s，退出码 0 |
| 检查点硬退出重开 | 1 passed，0.27s，退出码 0 |
| 生产文件 Pyright | 0 errors，0 warnings |
| Ruff / 格式检查 | 通过；91 个受检 Python 文件格式一致 |
| 模块依赖契约 | 7 kept，0 broken |
| `git diff --check` | 通过 |

包含 MCP 子进程的扩展场景检查在本环境达到 100 秒限时（退出码 124），未形成完整结果，因此不声称整仓测试全部通过。上述本地场景检查明确排除了 MCP/deferred 相关项；本轮未修改 MCP 协议实现。

## 接入与后续

重启后端后新任务生效；数据库自动补齐新表/字段，不需要手工清库。未新增必须填写的配置或凭据。

在项目根目录启动后端及 sidecar（不启动前端）：

```bash
backend/.venv/bin/baseagent --config config/config.toml start --no-frontend
```

若 sidecar 已由其他方式管理，仅启动 API：

```bash
backend/.venv/bin/baseagent-server --config config/config.toml
```

API 请求沿用现有 Bearer Token 认证。

前端 Session 需消费新增终态、结论、验证列表和收件箱 API；不能继续把所有 `task.finished` 事件画成成功。前端目录本轮未修改。

没有已知的本轮核心代码缺口。真实模型是否遵守新的结论/验证工具契约，仍需在用户提供凭据后做实际联调；本轮证明的是运行时约束、持久化和调用链，不宣称模型诊断质量已得到评估。
