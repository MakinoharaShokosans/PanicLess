# systemd 服务启动失败与反复重启排查

手册 ID：OPS-007  
适用范围：使用 systemd 的 Linux 系统服务；用户服务需使用对应用户及 `--user` 查询。  
关键词：启动失败、服务起不来、反复重启、restart loop、Start request repeated too quickly、start-limit-hit、203/EXEC、217/USER、status=1/FAILURE、systemctl。

本文 `example-api.service` 是示例服务名，不代表项目实际部署。命令以只读查询为主，已核对语法与本地 systemd 手册，未在目标业务环境执行。应选择服务实际所在的系统或用户服务管理器，避免把同名服务混淆。

## 症状与排查顺序

常见表现包括 `systemctl start` 返回失败、服务短暂 active 后退出、Restart 机制不断拉起新 PID，或 systemd 最后报告启动次数过多。排查顺序是：确认状态和失败时间 → 读取完整时间窗口日志 → 检查有效 unit 配置 → 根据退出原因修复 → 有限次数恢复并验证。

`Start request repeated too quickly` 通常说明启动频率达到限制，它是连续失败后的结果，未必是最初原因。反复执行 start 或 reset-failed 不能修复程序路径、依赖或配置错误，还可能覆盖最有用的首个故障现场。

## 第一步：读取状态与退出信息

```bash
ops_service='example-api.service'
systemctl status "$ops_service" --no-pager --full
systemctl show "$ops_service" -p LoadState -p ActiveState -p SubState -p Result -p MainPID -p ExecMainCode -p ExecMainStatus -p NRestarts
journalctl -u "$ops_service" --since '-30 min' --no-pager -n 200
```

`systemctl status` 是当前状态摘要，只附带有限日志，不是完整故障历史。`journalctl` 按服务和时间定位启动前后的信息，先找首次失败，再观察后续重试。查询条数达到 200 时应缩小时间段或继续向前查，不把输出末尾当作全部证据。

记录服务失败时刻、退出代码、PID 变化及 NRestarts。NRestarts 表示自动重启相关计数，不是永久的完整重启审计。服务已恢复时，当前状态不能替代故障窗口日志。

## 第二步：查看 unit、覆盖配置与运行环境

```bash
systemctl cat "$ops_service"
systemctl show "$ops_service" -p FragmentPath -p DropInPaths -p NeedDaemonReload -p Type -p ExecStart -p WorkingDirectory -p User -p Group -p EnvironmentFiles -p Restart -p RestartUSec
```

`systemctl cat` 展示磁盘上的 unit 和 drop-in，`systemctl show` 展示管理器掌握的属性。磁盘文件刚被修改时两者可能不一致，应检查 NeedDaemonReload，并在确认变更正确后重新加载管理器配置。查看配置输出可能包含敏感启动参数，应在分享排障记录前移除凭据。

检查 ExecStart 指向的文件是否存在、是否可执行；脚本还要检查首行解释器、解释器路径和文件换行格式。WorkingDirectory 不存在或不可访问会导致启动阶段失败。systemd 的 ExecStart 默认不是交给交互 shell 解释，不能假定管道、重定向、通配符和交互 shell 的 PATH 会按终端中的方式工作。

服务运行用户与手动执行命令的用户可能不同。终端里能启动不证明 systemd 环境下可启动；还要对照工作目录、环境文件、文件访问权限、限制和安全上下文。不要通过把服务改成 root 来掩盖未定位的权限问题。

## 第三步：按失败类型定位

| 状态或日志证据 | 常见检查方向 |
| --- | --- |
| `203/EXEC` | 可执行文件、解释器、执行权限、挂载 noexec、安全策略 |
| `217/USER` | 配置用户是否存在、用户身份切换是否成功 |
| `status=1/FAILURE` | 应用自身错误；读取应用日志和配置解析错误 |
| `Address already in use` | 实际监听进程、绑定地址、重复启动的实例 |
| `Dependency failed` | 失败的前置 unit，以及依赖关系的实际语义 |
| `start-limit-hit` | 前面连续失败的首因，修复后再考虑复位限制 |
| `Result=timeout` | 启动过程耗时、Type/通知协议、外部依赖是否阻塞 |

排查端口与依赖可使用：

```bash
ss -ltnp
systemctl list-dependencies "$ops_service" --all --no-pager
systemctl show "$ops_service" -p After -p Requires -p Wants -p TimeoutStartUSec
```

`After=` 只规定启动顺序，不自动拉起另一个服务，也不保证数据库或网络已具备业务可用性。`Requires=`、`Wants=` 与排序依赖有不同含义。出现数据库暂未就绪时，应检查应用重试和依赖健康条件，而不是只增加 After。

对于 Type=notify 的服务，应确认应用确实发送就绪通知；对于 Type=forking，应核对进程行为与 PID 识别。不能只为消除超时而盲目增大 TimeoutStartSec，否则可能把永久阻塞变成更长等待。

## 修复与受控恢复

修改前保存原 unit/drop-in 或应用配置的副本和原值。先修复已经由日志证明的路径、用户、权限、参数或应用配置问题；不要同时修改多项不相关配置。能使用应用自己的配置检查命令时，先运行其只检查模式。

unit 文件修改后，可以使用 `systemd-analyze verify` 对实际 unit 文件路径进行校验；它有助于发现 unit 配置问题，但不替代应用配置检查或依赖服务验证。随后执行 daemon-reload 使管理器重新读取 unit；daemon-reload 本身不会重启业务，也不会自动让应用读取自己的配置文件。

已修复首因但服务仍被启动频率限制时，再 reset-failed 后执行一次受控 start/restart。是否重启取决于服务当前状态与中断影响。重启仍失败时保留新的退出信息，恢复已确认无效的配置变更，不进入无限重试。移除问题 drop-in 或恢复旧值后，同样需要重新加载 unit，再按服务状态恢复运行。

## 恢复验证与易混淆情况

恢复标准不是只有 `active`：确认服务保持预期运行状态、业务端口或健康接口可用、依赖访问成功，并在代表性观察窗口不再增加异常重启。oneshot 服务执行完成后可能正常处于 inactive；是否成功应结合 Type、Result 和业务结果。

服务的 reload、restart 和 daemon-reload 含义不同：reload 请求应用重新加载其配置，restart 重建运行进程，daemon-reload 更新 systemd 管理器的 unit 定义。仅改应用配置时不一定需要 daemon-reload，仅做 daemon-reload 也不能证明应用已采用新配置。

## 官方参考入口

- [systemd.service](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html)
- [systemd.exec](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html)
- [systemctl](https://www.freedesktop.org/software/systemd/man/latest/systemctl.html)
- [journalctl](https://www.freedesktop.org/software/systemd/man/latest/journalctl.html)

部分属性随版本不同而变化，以目标主机手册和实际 unit 类型为准。
