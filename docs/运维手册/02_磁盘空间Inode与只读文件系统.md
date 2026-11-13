# 磁盘空间不足、Inode 耗尽与只读文件系统排查

手册编号：OPS-002  
适用范围：Linux 本地文件系统的写入失败诊断；容器、配额、网络文件系统需结合其存储边界。  
检索关键词：磁盘满、无法写日志、无法创建文件、No space left on device、ENOSPC、Disk quota exceeded、EDQUOT、Read-only file system、EROFS、inode 用完。

## 症状与错误分类

“写不进去”至少有四类常见原因：数据块空间不足、inode 不足、用户/项目配额限制、挂载只读。应用显示统一的“保存失败”时，先取到系统调用或应用日志里的原始错误。

`No space left on device` 常对应空间或 inode 耗尽；`Disk quota exceeded` 指向配额；`Read-only file system` 指向挂载/文件系统状态。`Permission denied` 则优先检查权限，不能通过清理磁盘解决。

本文使用 `/var/log` 作为示例故障目录。应将命令中的路径替换为应用实际写入位置，路径不存在时选择已存在的最近父目录。

## 第一步：把故障路径映射到正确挂载点

```bash
findmnt -T /var/log -o TARGET,SOURCE,FSTYPE,OPTIONS
df -hT /var/log
df -i /var/log
```

`findmnt` 给出目标路径所属挂载点、设备、文件系统和挂载选项。`df -hT` 查看块空间，`df -i` 查看 inode。容量有余但 inode 接近耗尽时，优先寻找大量小文件；inode 充足但块空间不足时，优先找大文件或隐藏占用。

只检查 `/` 的剩余空间可能检查错分区：应用数据可能在 `/data`、独立日志盘或容器可写层。容器中看到的路径也不一定等于宿主机同名路径，需要在应用的挂载视图中确认。

## 第二步：根据空间与 inode 结果缩小目录范围

先获得实际挂载点，再只统计该文件系统的一层目录：

```bash
ops_mount=$(findmnt -n -o TARGET -T /var/log)
sudo du -xhd1 -- "$ops_mount"
```

这里的 `-x` 不跨入其他文件系统，`-d1` 先看一层，避免一开始输出整个目录树。对显著偏大的子目录继续逐层统计。生产大盘上 `du` 仍可能产生较多元数据 I/O，应缩小到问题相关目录。

如怀疑大量小文件，在已确认的候选目录计数，而不是扫描整个服务器：

```bash
sudo find /var/log -xdev -type f -printf '%h\n' | sort | uniq -c | sort -nr | head -n 20
```

该命令按父目录统计普通文件数量，只是缩小排查范围，不能直接证明这些文件可删除。队列积压、会话文件、缓存和监控碎片数据都可能造成 inode 耗尽。

## 第三步：区分 df 与 du 差异

`df` 统计文件系统空间，`du` 统计可遍历路径所对应文件的占用。两者不是完全相同的口径。

如果 `df` 很满而 `du` 找不到相应文件，检查：

- 已删除但仍被进程打开的文件，处理方法参考 OPS-003。
- `du` 没有权限读取、扫描不完整，或统计了不同挂载点。
- 挂载覆盖下的旧文件、文件系统快照、元数据及保留空间。
- 稀疏文件、压缩和共享块导致的逻辑大小与实际占用差异。

```bash
sudo lsof -nP +L1
```

`lsof +L1` 显示链接数小于 1 的已打开文件；结合设备与实际块占用判断是否与目标分区有关。不要将每行的 SIZE/OFF 简单相加后当作可释放空间。

如果空间与 inode 均有余而仍报 EDQUOT，检查文件所属用户或项目的配额。具备对应工具和权限时可用 `quota -s` 查看当前用户配额；XFS 项目配额等需要使用对应管理工具，不能用 `df` 替代配额检查。

## 第四步：只读文件系统走独立分支

```bash
findmnt -T /var/log -o TARGET,SOURCE,FSTYPE,OPTIONS
journalctl -k --since '30 minutes ago' --no-pager -n 150
```

挂载选项中的 `ro` 表示只读。内核日志如果同时出现 I/O error、文件系统错误或 remount-read-only，应进一步排查设备、宿主机存储和文件系统；也可能只是部署明确使用了只读挂载，需核对预期配置。

只读错误不一定意味着磁盘满。发现文件系统异常后，不把强制 remount 为 rw 当作通用修复，也不对仍挂载的文件系统直接运行修复操作。应先确定数据保护和停机维护方式，再按文件系统工具要求处理。

## 处理方法与适用条件

空间不足时，优先处理已有保留策略允许清理的旧日志、可再生成缓存或过期归档。先确认拥有者、保留周期和是否仍被应用使用，再通过对应组件的管理方式清理；数据库文件不能按普通日志处理。

inode 耗尽时，找到异常增长的小文件来源，修复产生机制后再分批清理过期项。单纯扩容字节容量未必增加现有文件系统可用 inode，方案需要依据文件系统类型确定。

配额不足时，应核实业务配额和实际使用量，再调整配额或减少数据。扩容或配额修改前记录原值，修改后验证实际可用量；不要把文件系统保留空间随意调到零。

如果问题源于持续日志风暴，释放空间只是临时恢复，还需修复重复错误或补充轮转。已删除但仍被打开的文件，应通过持有进程释放引用，不能再次删除原路径解决。

## 恢复验证与关闭条件

重新检查故障路径的 `df -hT`、`df -i`、挂载选项和配额。通过应用自身的正常写入功能验证恢复，确认没有新增 ENOSPC、EDQUOT 或 EROFS，并观察空间/文件数量增长速度回到预期。

记录根因属于块空间、inode、配额、只读还是隐藏占用，并列出实际释放量或扩容量。没有确认分类时，不将“重启后暂时能写”作为完整结论。

## 参考资料

- [GNU df 文档](https://www.gnu.org/software/coreutils/manual/html_node/df-invocation.html)
- [GNU du 文档](https://www.gnu.org/software/coreutils/manual/html_node/du-invocation.html)
- [findmnt 手册](https://man7.org/linux/man-pages/man8/findmnt.8.html)
