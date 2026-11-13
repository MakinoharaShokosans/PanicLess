# 内存压力、进程退出与 OOM 排查

手册 ID：OPS-005  
适用范围：Linux 主机、systemd 服务，以及使用 cgroup v2 的容器或服务。  
关键词：内存不足、进程突然消失、OOM、Out of memory、Killed process、exit code 137、Cannot allocate memory、Swap、memory.events。

本文的 `example-api.service`、进程名称和路径均为示例，不代表 PanicLess 的实际部署。命令用于现场只读检查；已按命令语法和手册核对，未在目标业务环境执行。应在服务所在主机或正确的容器命名空间运行。

## 症状与问题边界

内存压力可能表现为请求延迟增加、频繁换页、服务突然退出、容器显示 OOMKilled，或应用报告 `Cannot allocate memory`。同一种现象可以来自主机内存紧张、服务自己的 cgroup 上限、应用地址空间限制或应用分配失败，不能全部归为主机 OOM。

退出码 137 常见于以 `128 + 信号编号` 表示退出状态的环境，对应 SIGKILL。人工终止、超时管理器和 OOM Killer 都可能发送 SIGKILL；137 本身不能证明 OOM。systemd 的退出记录应直接查看 Code、Status 与 Result，而不是机械换算成 shell 退出码。

## 第一步：确定故障发生时间和退出证据

先把服务退出时间与用户报告时间对齐，再查同一时间窗口的服务日志和内核日志：

```bash
ops_service='example-api.service'
systemctl show "$ops_service" -p ActiveState -p SubState -p Result -p ExecMainCode -p ExecMainStatus -p MainPID
journalctl -u "$ops_service" --since '-30 min' --no-pager -n 200
journalctl -k --since '-30 min' --no-pager | rg -i 'out of memory|oom-kill|killed process'
```

`Result=oom-kill` 或同一时刻明确记录的 OOM 事件是强线索。内核日志中的被杀进程、PID 和 cgroup 路径应与目标服务关联；仅在日志里看到另一个时间的 OOM 不能解释本次退出。读取内核日志可能需要额外权限，日志轮转或容器隔离也可能使记录不可见；未查到记录与确认没有发生 OOM 是两种结论。

## 第二步：区分主机压力与服务限额

```bash
free -h
vmstat 1 5
ps -eo pid,ppid,comm,rss,vsz --sort=-rss | head -n 15
systemctl show "$ops_service" -p ControlGroup -p MemoryCurrent -p MemoryHigh -p MemoryMax
```

`free` 的 available 比单独看 free 更适合判断可用内存，因为一部分缓存可以回收。`vmstat` 第一行通常是启动以来的平均值，后续行才是采样间隔；持续的 si/so、任务阻塞和延迟变化共同支持换页压力判断。Swap 已用很多但 si/so 接近零，可能只是历史页尚未换回。

`ps` 的 RSS 单位通常为 KiB，表示当前驻留内存；VSZ 是虚拟地址空间，不能把巨大 VSZ 直接当作物理内存泄漏。共享页也可能在多个进程的 RSS 中重复计入。服务内存低于主机余量仍可能触发较小的 `MemoryMax`，应检查服务实际所属 cgroup。

## 第三步：读取 cgroup v2 事件与用量

以下命令适用于宿主机统一 cgroup v2 挂载在 `/sys/fs/cgroup` 的情况：

```bash
ops_cgroup=$(systemctl show "$ops_service" -p ControlGroup --value)
if [ -n "$ops_cgroup" ] && [ -r "/sys/fs/cgroup${ops_cgroup}/memory.events" ]; then
  cat "/sys/fs/cgroup${ops_cgroup}/memory.events"
  cat "/sys/fs/cgroup${ops_cgroup}/memory.current"
  cat "/sys/fs/cgroup${ops_cgroup}/memory.max"
  cat "/sys/fs/cgroup${ops_cgroup}/memory.stat"
fi
```

`memory.max` 为 `max` 表示该层没有配置数值硬上限，但祖先 cgroup 仍可能有限制。`memory.events` 的 high、max、oom、oom_kill 是累计计数；应保存两次带时间的观测并比较增量，不能把历史累计值当作本次事件。该文件可汇总子层级事件，需要进一步定位时查看相应子 cgroup，或在支持时查看 `memory.events.local`。

`memory.stat` 可帮助区分匿名内存与文件缓存。服务重启或 cgroup 重建会影响计数连续性，应同时记录服务启动时间。内存泄漏需要在可比较负载下观察持续增长；单次高占用只能证明当时占用高。

## 证据判断与处理分支

| 证据组合 | 更可能的方向 | 下一步 |
| --- | --- | --- |
| 同时刻 OOM 事件指向服务，相关 oom_kill 计数增加 | 服务确实受到 OOM 终止 | 核对服务限额、并发、缓存及故障前内存采样 |
| 主机 available 很低，持续换页，多服务同时变慢 | 全局内存压力 | 定位持续增长的工作负载，检查批任务或发布变化 |
| 主机有余量，服务频繁到达 cgroup 上限 | 服务配额或服务内部用量问题 | 对照容量需求检查 MemoryMax 与应用内存预算 |
| 只有 137，没有可关联的内存事件 | SIGKILL 原因未定 | 查人工操作、任务超时、编排器事件和审计记录 |

处理内存压力时，优先降低造成压力的请求并发、暂停已确认的非关键批任务，或回滚与增长相关的应用版本。调整服务内存上限前先确认宿主机容量；提高上限会把服务内压力转移到整机，不能作为泄漏修复。

确需重启恢复服务时，先保存故障时间、日志、用量与配额证据，并确认可接受的中断范围。应用或 systemd 配置修改应保留旧值，采用一次受控变更；效果不符时恢复原值或原版本。不要把 drop_caches、反复重启或添加 Swap 作为所有 OOM 的固定处方。

## 恢复验证与常见误判

恢复标准包括：服务健康检查成功、正常负载下内存没有继续单调增长、同一观测窗口不再产生新的 OOM 事件、延迟与换页活动恢复到可接受水平。短暂启动成功不足以证明恢复，应覆盖此前触发故障的代表性负载。

当前内存 Top 进程不一定是过去 OOM 的肇因：被杀进程已经消失，内存已经释放；被杀对象也不一定是唯一压力来源。需用故障前后采样与同一 cgroup 的事件建立时间关系，再讨论“谁导致了内存不足”。

## 官方参考入口

- [Linux cgroup v2 内存控制器](https://docs.kernel.org/admin-guide/cgroup-v2.html)
- [systemd.resource-control](https://www.freedesktop.org/software/systemd/man/latest/systemd.resource-control.html)
- [GNU Bash 退出状态](https://www.gnu.org/software/bash/manual/html_node/Exit-Status.html)

不同发行版和 systemd 版本支持的字段可能不同，以目标主机的本地手册为准。
