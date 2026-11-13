# PostgreSQL 连接耗尽与应用连接池超时排查

手册 ID：OPS-010  
适用范围：PostgreSQL 14 及以上版本、连接 PostgreSQL 的应用连接池；版本新增配置项应以现场 pg_settings 为准。  
关键词：数据库连不上、连接数满、连接池耗尽、获取连接超时、too many clients already、remaining connection slots are reserved、SQLSTATE 53300、Connection is not available、connection pool timeout、idle in transaction、锁等待。

## 症状与问题边界

应用连接池超时不等于 PostgreSQL 的连接上限已满。应用可能没有归还连接，也可能因慢查询或锁等待长时间占用池中连接；数据库仍有剩余连接槽位。反过来，PostgreSQL 拒绝新会话也可能来自所有应用实例连接池总量超过数据库预算。

记录异常发生时间、应用实例数量、连接池 active/idle/pending 指标、池上限以及数据库原始报错。以下 `db.example.com`、`appdb`、`ops_reader` 都是示例，不代表项目实际部署。SQL 为只读排查语句；本手册未连接实际数据库执行验证。

## 第一步：分辨网络、认证与连接容量

已有授权管理会话时优先复用。需要新会话时，通过已有凭据管理或受保护的 `.pgpass` 提供认证，不把口令写进命令行：

```bash
psql "host=db.example.com port=5432 dbname=appdb user=ops_reader connect_timeout=5 sslmode=verify-full"
```

`verify-full` 要求客户端配置正确的 CA 信任并验证域名。连接失败时先读取错误：名称解析失败转查 DNS；Connection refused/timeout 转查监听和网络；password authentication failed 转查认证；SQLSTATE 53300 或连接槽位保留提示才指向连接容量。

数据库连接已经耗尽时，不要持续创建诊断会话加重连接风暴；使用已有管理会话或按授权流程进入保留管理通道。

## 第二步：读取连接限制与客户端分布

```sql
SELECT name, setting, unit
FROM pg_settings
WHERE name IN (
  'max_connections',
  'superuser_reserved_connections',
  'reserved_connections'
)
ORDER BY name;

SELECT datname, usename, application_name, client_addr, state,
       count(*) AS connections
FROM pg_stat_activity
WHERE backend_type = 'client backend'
GROUP BY datname, usename, application_name, client_addr, state
ORDER BY connections DESC;
```

较旧版本没有 `reserved_connections`，对应查询不返回该行是正常现象。普通应用不能把全部 `max_connections` 都当作可用额度，还要考虑保留槽位和其他客户端。数据库内部后台进程不应混入应用连接池的占用统计。

普通角色对其他会话部分字段的可见性有限。看到空值或被隐藏的查询信息时，使用已授权的监控角色，例如具备适当 `pg_read_all_stats` 能力的角色；不要把“看不到”解释为“没有连接”。

如果某个应用的连接数突然增加，结合发布、扩容和连接池设置计算总上限：实例数 × 每实例连接池上限，再加定时任务、迁移工具和管理连接。池上限的总和应留有合理余量，而不是每个实例都按整个数据库容量配置。

## 第三步：查看长事务、活跃查询与锁等待

```sql
SELECT pid, datname, application_name, state,
       now() - xact_start AS transaction_age,
       now() - query_start AS query_age,
       wait_event_type, wait_event,
       pg_blocking_pids(pid) AS blocking_pids
FROM pg_stat_activity
WHERE backend_type = 'client backend'
  AND pid <> pg_backend_pid()
ORDER BY xact_start NULLS LAST
LIMIT 30;
```

`query_age` 在非 active 会话中可能反映上一次查询开始以来的时间，不代表查询还在执行。判断正在执行的慢查询需结合 `state='active'`；判断长事务需结合 `xact_start` 和事务状态。

| 观测结果 | 判断与下一步 |
| --- | --- |
| 应用池 active 达上限、pending 持续增加，数据库连接数不高 | 检查应用连接归还、事务生命周期和长时间占用，不先提高 PostgreSQL 上限 |
| 大量 active 会话等待 Lock，blocking_pids 非空 | 查阻塞源及其事务，先处理阻塞链，不把等待连接视为 CPU 忙 |
| 大量 idle in transaction | 客户端事务未结束，可能持续持锁并阻碍清理；定位对应应用路径 |
| 大量普通 idle 会话 | 可能是连接池正常保留，也可能池规模过大；结合流量、实例数和池配置判断 |
| SQLSTATE 53300 且客户端连接接近允许额度 | 核对实例扩容和连接预算，减少非必要连接并处理异常占用 |

`pg_blocking_pids` 可帮助查锁阻塞，但不要以极高频率全库轮询。短时样本应与应用报错时间对齐，当前正常状态不能直接否定此前发生过连接耗尽。

## 第四步：从应用侧定位占用原因

在应用侧比较连接获取等待时间、SQL 执行时间、事务持续时间和池中连接数。获取连接慢但 SQL 执行时间正常，优先检查池容量与归还；SQL 执行本身变慢，则继续查看锁、执行计划、磁盘和数据库负载。

连接泄漏通常表现为流量下降后 active 仍不回落、等待者增加，且数据库会话持续被应用占用。单次看到普通 idle 不能证明泄漏。若通过 PgBouncer 等代理连接，还应区分应用到代理的客户端连接与代理到 PostgreSQL 的服务端连接，不能把两层上限混为一个数。

## 常见分支：发布扩容后突然连接耗尽

假设数据库上限为 200，某应用原有 8 个实例，每个实例池上限为 20，仅该应用理论最大连接数就是 160。扩容到 12 个实例后，这个数变成 240，尚未计入其他应用与管理保留连接。这个示例说明：实例变多不一定提高吞吐，可能先耗尽数据库入口容量。

应同时查看池的最大连接数、最小空闲数和连接创建速率。高最小空闲数会在扩容或滚动启动时集中建立连接，重试又可能形成连接风暴。处理这类问题时，先降低启动并发或不必要的池保留量，并给连接获取失败设置合理退避，再依据数据库承载能力分配各应用预算。

如果连接总数未到上限，但某个业务账号始终无法新建会话，还要核对角色或数据库自身的连接限制。资源不足可能只影响特定角色，不能用另一个管理账号连接成功来排除。完成配置调整后，记录每类应用的连接预算及实例上限，让下一次扩容继续沿用同一容量约束。

## 处理方法与恢复验证

优先修复未关闭连接和未结束事务，检查异常路径是否执行归还；针对真实慢查询或阻塞源修正 SQL、索引或事务范围。临时降低应用并发或缩减异常实例连接池时，记录原值，观察业务吞吐和排队变化，必要时回退。

不要无差别终止数据库会话。需要取消阻塞查询时，先确认会话归属、事务影响和业务恢复方式；取消查询与终止连接的影响不同。提高 `max_connections` 会增加资源需求，且通常需要重启，不能替代连接预算与泄漏修复。

恢复标准是：应用能在预期时间内取得连接，pending 不再持续积压；数据库连接数保有余量；错误码不再新增；长事务和锁等待恢复到业务正常水平。恢复后还需检查请求重试是否造成重复提交，不能仅以一次 `SELECT 1` 成功宣布连接池故障解决。

## 官方参考

- [PostgreSQL 连接设置](https://www.postgresql.org/docs/current/runtime-config-connection.html)
- [PostgreSQL pg_stat_activity](https://www.postgresql.org/docs/current/monitoring-stats.html#MONITORING-PG-STAT-ACTIVITY-VIEW)
- [PostgreSQL 锁信息函数](https://www.postgresql.org/docs/current/functions-info.html#FUNCTIONS-INFO-SESSION)
- [libpq 连接参数](https://www.postgresql.org/docs/current/libpq-connect.html#LIBPQ-PARAMKEYWORDS)
