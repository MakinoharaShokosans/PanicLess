# DNS 解析失败与域名访问异常排查

手册 ID：OPS-008  
适用范围：Linux 客户端与服务端的 DNS、系统名称解析，以及应用访问域名失败的定位。  
关键词：域名打不开、找不到主机、解析超时、NXDOMAIN、SERVFAIL、Temporary failure in name resolution、Name or service not known、Could not resolve host、DNS 缓存、A、AAAA。

## 症状与问题边界

DNS 故障发生在应用把域名转换成地址的阶段。常见表现是同一域名在部分机器可访问、部分机器报解析失败，或者改用正确 IP 后能建立连接。`curl: (6) Could not resolve host` 通常指名称解析失败；`Connection refused` 表示已经尝试连接某个地址，更应检查监听端口；TLS 证书错误发生在连接建立之后。

本文中的 `api.example.com`、`example.com` 和 `192.0.2.10` 均为文档示例，不代表真实业务地址。执行排查前，将示例换成实际域名与已经核实的服务地址。以下命令用于现场排查，本文未连接实际业务系统执行验证。

先记录失败时间、应用所在主机或容器、访问域名和原始错误。必须在发生故障的执行环境内检查：宿主机解析正常，不代表容器内的 DNS 配置与网络也正常。

## 第一步：比较系统解析与直接 DNS 查询

```bash
getent ahosts api.example.com
dig api.example.com A +time=2 +tries=1
dig api.example.com AAAA +time=2 +tries=1
```

`getent` 走系统名称服务配置，可能使用 `/etc/hosts`、DNS 或其他 NSS 模块；`dig` 直接查询 DNS。两者结果不同，说明需要检查本地 hosts、NSS、搜索域或缓存，而不是立即修改权威 DNS。

查看 `dig` 的 `status`、`ANSWER SECTION`、TTL 和 `SERVER`。A 查询没有地址不代表域名不存在，目标可能只有 AAAA，或需要继续查看 CNAME 链。AAAA 返回地址但客户端没有可用 IPv6 路由，也可能造成“解析成功、访问很慢”。

| 观测结果 | 优先排查方向 |
| --- | --- |
| NXDOMAIN | 域名拼写、搜索域补全、权威记录是否存在；也可能是递归服务器缓存了此前的不存在结果 |
| NOERROR 但无目标类型答案 | 该记录类型不存在、CNAME 后续结果或查询类型不匹配 |
| SERVFAIL | 递归服务器故障、上游不可达、DNSSEC 验证或权威链问题 |
| 超时、no servers could be reached | 到解析服务器的网络、UDP/TCP 53、服务器负载与服务状态 |
| 地址与预期不同 | hosts 覆盖、分地域/内外网解析、缓存、旧记录或查询了不同解析服务器 |

## 第二步：确认实际使用的解析配置

```bash
cat /etc/resolv.conf
cat /etc/nsswitch.conf
cat /etc/hosts
```

检查 `nameserver`、`search`、`options`，以及 NSS 的 `hosts:` 行。`127.0.0.53` 等地址可能是本地 stub resolver，不是最终上游 DNS；容器中的环回地址则指向该容器自身。长搜索域列表或过大的 `ndots` 可能使一个名字经过多次失败查询才成功。

使用 systemd-resolved 的系统可继续读取：

```bash
resolvectl status
resolvectl query api.example.com
```

`resolvectl status` 展示按网络接口分配的 DNS、搜索域和路由域。VPN 启动后只有内部域名失败时，重点核对 VPN 接口的路由域与解析服务器。系统未使用 systemd-resolved 时，跳过这一步，检查实际网络管理器的配置。

## 第三步：分辨解析服务器和传输路径问题

向已经确认的内部解析服务器发起相同查询，比较系统默认结果。下面的地址仅为示例：

```bash
dig @192.0.2.53 api.example.com A +time=2 +tries=1
dig @192.0.2.53 api.example.com A +tcp +time=2 +tries=1
```

UDP 超时而 TCP 成功时，检查 UDP 53 的网络策略、丢包和响应大小；不能据此直接判断权威记录错误。一个递归服务器失败、另一个正常时，比较其上游、缓存和 DNSSEC 处理。内部域名可能只在内网 DNS 存在，换用公共 DNS 不构成有效修复。

需要检查权威来源时，先查询域的 NS，再向对应权威服务器查询记录。内外网分区解析应使用各自网络内的权威信息；公网结果不能覆盖内网记录的判断。

## 第四步：绕过 DNS，确认后续网络与应用是否正常

在已知正确服务 IP 的前提下，保留域名、HTTP Host 与 TLS SNI，只替换本次连接地址：

```bash
curl --connect-timeout 3 --max-time 10 --resolve api.example.com:443:192.0.2.10 https://api.example.com/health
```

如果普通域名访问解析失败，而 `--resolve` 返回预期健康响应，证据支持 DNS 路径存在问题。如果该命令也出现连接超时、证书错误或 HTTP 5xx，则继续对应网络、TLS 或应用排查。直接访问 `https://192.0.2.10` 会改变名称校验与虚拟主机选择，不是等价对照。

## 常见分支：记录已经修复，但部分应用仍失败

权威 DNS 的当前答案、递归服务器缓存与应用进程缓存属于不同层。新增记录前曾返回 NXDOMAIN 时，递归服务器可能缓存了否定结果；此时新记录已经存在，但客户端在缓存有效期内仍看到不存在。应比较权威查询与故障客户端使用的递归服务器，不要反复删除、重建同一条记录。

CNAME 链中任意目标缺失都可能导致最终地址获取失败。别名记录存在不代表最终 A/AAAA 记录可用；逐段核对别名目标、所属域和返回状态。切换服务地址后，还要考虑应用连接池继续复用旧连接，不能把所有旧地址流量都归因于 DNS 未刷新。

容器内只有部分服务失败时，检查短服务名是否依赖搜索域。用完整域名验证后，再核对容器内 `search` 配置和应用实际提交的名字。系统工具与应用结果不同时，记录应用运行时的缓存策略；不要直接重启全部业务进程。若确需重启单个实例以释放应用缓存，应通过已有滚动发布流程保持可用实例，并在该实例恢复后比较结果。

## 处理方法与恢复验证

记录缺失时修正对应 DNS 记录；解析服务器不可达时修复路由、服务或端口策略；VPN 内部域失败时修正路由域；hosts 错误时移除确认失效的覆盖项。每次变更保留原配置和变更时间，失败时恢复原记录或网络配置。

DNS 缓存清理只适用于已经修复上游、但仍保留旧结果的客户端。对于 systemd-resolved，确认使用该服务后可执行 `resolvectl flush-caches`；这不能修复权威记录缺失。由网络管理器生成的 `/etc/resolv.conf` 应修改其配置源，避免手工覆盖后被再次生成。

恢复标准是：故障执行环境内的 `getent` 与 DNS 查询得到预期地址，普通域名访问在预期时限内成功，应用原始解析错误不再新增。记录 TTL，并在缓存刷新后的时间点复查；不同地理位置或负载均衡返回不同地址本身并不是故障。

## 易混淆情况

- `ping` 失败可能只是 ICMP 被禁止，不能证明 DNS 或 HTTPS 不可用。
- 域名解析成功而 TCP 超时，应转查网络与监听；HTTPS 名称校验失败应转查证书。
- 应用可能使用自己的 DNS 缓存或连接池，系统命令恢复不等于运行中应用已经采用新地址。
- 单次查询成功不能解释间歇性故障，应保留失败时间、实际查询服务器与多次结果。

## 官方参考

- [BIND 9 dig 手册](https://bind9.readthedocs.io/en/latest/manpages.html#dig-dns-lookup-utility)
- [systemd resolvectl 手册](https://www.freedesktop.org/software/systemd/man/latest/resolvectl.html)
- [curl 命令手册](https://curl.se/docs/manpage.html)
