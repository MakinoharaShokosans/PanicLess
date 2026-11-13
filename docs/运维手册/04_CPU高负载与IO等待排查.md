# CPU 使用率高、Load 高与 I/O 等待排查

手册编号：OPS-004  
适用范围：Linux 主机和运行在其上的应用进程；容器需同时考虑 CPU 配额与宿主机资源。  
检索关键词：服务器卡顿、命令执行慢、CPU 100%、Load Average 高、D 状态、iowait、wa 高、st 高、线程死循环、CPU throttling。

## 先区分 CPU 忙与系统负载高

Linux Load Average 反映一段时间内可运行任务和不可中断睡眠任务的平均数量，并不等于 CPU 使用率。大量任务等待磁盘或其他内核资源时，Load 可以很高，而 CPU 并没有执行很多用户态计算。

“进程 CPU 100%”的分母也需要确认。常见工具按单个逻辑 CPU 计进程使用率，多线程进程可能超过 100%；系统整体百分比通常是另一口径。不能直接把二者放在同一标尺上比较。

记录卡顿开始时间、近期发布或定时作业、业务流量变化。本文中的 PID 1234、服务名 `example-api.service` 和 cgroup 路径均为示例。

## 第一步：连续观察系统整体状态

```bash
uptime
nproc
vmstat 1 5
```

`uptime` 显示 1、5、15 分钟 Load，用于判断负载趋势。`nproc` 提供当前进程可见的处理器数量，但容器有效 CPU 能力还可能受 cgroup 配额限制。

`vmstat 1 5` 中第一行通常是自启动以来的统计概览，应主要观察后续间隔样本：

| 字段与现象 | 解释方向 |
| --- | --- |
| `r` 持续多、`us` 高、`id` 低 | 可运行任务竞争 CPU，继续定位计算热点 |
| `sy` 高 | 内核态活动多，结合系统调用、网络包量或内存管理情况分析 |
| `b` 多、进程 D 状态多 | 不可中断等待任务增加，优先查存储或相关内核等待 |
| `si/so` 持续非零 | 正在换页，卡顿可能与内存压力有关，参考 OPS-005 |
| `st` 持续偏高 | 虚拟机获得 CPU 的时间被宿主层抢占，需要结合宿主资源调查 |

`wa` 是诊断线索，不是单独的磁盘故障判据。多核、虚拟化和并发 I/O 情况下，应结合设备延迟及进程状态解释。

## 第二步：定位持续占用 CPU 的进程与线程

```bash
top -b -d 1 -n 3
ps -eo pid,ppid,stat,pcpu,pmem,etimes,comm --sort=-pcpu | head -n 16
```

`ps` 的 `%CPU` 常是进程生命周期内的平均使用情况，不能单独当作“刚才这一秒”。用 top 的后续刷新或支持间隔采样的工具确认持续热点，避免把历史高占用误判为当前瓶颈。

定位到示例 PID 后查看线程：

```bash
top -H -p 1234 -b -d 1 -n 3
ps -L -p 1234 -o pid,tid,stat,pcpu,comm
ps -p 1234 -o pid,ppid,lstart,etimes,args
```

如果安装了 sysstat，还可用 `pidstat -u -t -p 1234 1 5` 观察线程在采样窗口内的 CPU。后续栈分析应使用该语言运行时支持的工具，并了解其开销。高 CPU 可能是正常计算、流量增长、GC、正则回溯或死循环，进程名本身不能证明恶意程序。

## 第三步：高 Load、CPU 不忙时检查等待路径

```bash
ps -eo pid,ppid,stat,wchan:32,comm
journalctl -k --since '30 minutes ago' --no-pager -n 100
```

关注 STAT 以 D 开头的任务和 WCHAN。D 表示不可中断睡眠，不等于进程已经永久卡死，也不只由本地磁盘引起；NFS、块设备、某些驱动和内核同步路径都可能涉及。

如果安装了 sysstat，使用 `iostat -xz 1 5` 查看设备的吞吐、队列和平均请求延迟，优先解释后续间隔样本。现代并行设备的 `%util` 接近 100% 不一定等于无法处理更多 I/O，需与业务延迟、队列和设备能力一起判断。

将出现等待的进程与实际访问路径、挂载点和内核错误对应起来。内核 I/O error、网络文件系统超时以及设备延迟同时出现，才比单独一个高 Load 更能定位问题。

## 第四步：检查启动来源与 CPU 配额

```bash
pstree -aps 1234
cat /proc/1234/cgroup
systemctl show example-api.service -p ControlGroup -p CPUQuotaPerSecUSec
```

pstree 可帮助理解当前父子关系；若工具缺失，可逐级用 `ps` 查询 PPID。但父进程可能已退出或被重新托管，当前进程树不能证明该程序每分钟由 cron 拉起。周期性启动需要结合对应 timer/cron 配置、服务事件和时间记录。

在 cgroup v2 环境，根据实际 ControlGroup 路径读取 `cpu.max` 与 `cpu.stat`，隔一段时间再次采样。`nr_throttled`、`throttled_usec` 的累计值必须看增量；CPU 配额限流可能让应用变慢，即使宿主机还有空闲 CPU。不能把宿主机核数直接当作容器可用算力。

## 处理与恢复验证

先按证据分类处理：流量超过容量时限流或扩容；异常计算热点修复代码或回退已确认有问题的发布；换页压力转入内存排查；I/O 等待处理对应存储或依赖；配额不合理时按资源预算调整并记录原值。

不要因为 Load 高就批量杀进程。对 D 状态任务，信号也可能要等内核等待结束后才生效。若确认由重复启动造成资源争用，应修复启动来源与并发控制，而不是反复只杀子进程。

恢复后再次采集相同窗口的 vmstat、进程/线程样本和业务响应时间，确认排队、等待或限流计数的增长回到正常水平。Load Average 有时间平滑，不会在问题解除后立即归零；应结合即时采样和业务表现判断。

## 参考资料

- [proc_loadavg 手册](https://man7.org/linux/man-pages/man5/proc_loadavg.5.html)
- [procps vmstat 手册](https://man7.org/linux/man-pages/man8/vmstat.8.html)
- [Linux cgroup v2 CPU 控制器](https://docs.kernel.org/admin-guide/cgroup-v2.html#cpu)
