# TLS 证书过期与 HTTPS 握手失败排查

手册 ID：OPS-009  
适用范围：Linux 客户端到 HTTPS 服务的证书验证与 TLS 握手；适用于反向代理、负载均衡和应用入口。  
关键词：证书过期、证书链不完整、HTTPS 打不开、SSL certificate problem、certificate has expired、unable to get local issuer certificate、hostname mismatch、TLS handshake failure、SNI、SAN、mTLS。

## 症状与问题边界

HTTPS 访问依次经过名称解析、TCP 连接、TLS 握手和 HTTP 请求。`Could not resolve host` 首先检查 DNS；连接被拒绝首先检查监听与网络；`curl: (60) SSL certificate problem` 通常是证书验证失败；成功返回 HTTP 502 说明客户端到入口的 TLS 已经完成，应继续检查入口到上游的链路。

本文的 `api.example.com`、`192.0.2.10`、证书路径和健康检查路径均为示例，需替换为实际目标。命令未连接真实业务服务执行；所有检查应在出现问题的客户端或容器环境进行。

## 第一步：确认失败阶段与客户端时间

```bash
date -u
curl --verbose --connect-timeout 3 --max-time 10 https://api.example.com/health
```

查看 `curl` 是否先完成地址解析、连接到哪个 IP、是否收到服务端证书，以及具体的验证错误。终端输出可能包含请求头，排查时不要额外携带业务令牌。

错误的客户端时间会使正常证书看起来“尚未生效”或“已经过期”。先对照可信时间来源；若系统启用了 systemd 时间管理，可通过 `timedatectl status` 查看时钟与同步状态。修复时间服务后再判断证书是否需要更换。

## 第二步：读取目标实际发送的证书与验证结果

```bash
timeout 10s openssl s_client -connect api.example.com:443 -servername api.example.com -showcerts -verify_hostname api.example.com -verify_return_error </dev/null
```

`-servername` 发送 SNI，让多域名入口选择正确证书；`-verify_hostname` 明确检查证书名称；`-verify_return_error` 使验证错误终止握手。`s_client` 是否使用预期的信任根，取决于本机 OpenSSL 信任配置；内部 CA 场景应通过已配置的可信根或明确的 `-CAfile` 检查。

记录叶证书的主题、签发者、有效期、SAN、服务端发送的中间证书及验证返回码。不要只看命令是否建立 TCP 连接，也不要把未开启名称校验的握手成功视为域名匹配。

查看本地待部署证书时，可使用：

```bash
openssl x509 -in /etc/nginx/tls/example-fullchain.pem -noout -subject -issuer -dates -ext subjectAltName
```

该命令读取文件中的第一张证书，通常应是叶证书。磁盘上的证书已更新，不代表运行中的代理已经加载了它；最终仍以客户端实际收到的证书为准。

## 第三步：根据证据选择排查方向

| 证据 | 判断与下一步 |
| --- | --- |
| 叶证书 notAfter 已过期，客户端时间正确 | 更新对应站点证书，检查续期任务和部署/重载过程 |
| 证书尚未生效 | 核对系统时间和新证书的 notBefore，不能仅延长客户端超时 |
| SAN 不包含请求域名 | 检查域名拼写、SNI、入口虚拟主机与证书绑定 |
| unable to get local issuer certificate | 检查服务端中间链与客户端根 CA，不能只凭这一句认定某一端出错 |
| 浏览器正常，某容器或脚本失败 | 比较客户端 CA 包、信任库、OpenSSL/JVM 版本及实际访问地址 |
| certificate required / bad certificate | 检查是否启用双向 TLS，以及客户端证书、私钥和服务端信任策略 |
| protocol version / no shared cipher | 对照两端支持的协议和加密套件，检查近期策略变更 |

服务器通常应发送叶证书与必要中间证书，不依赖客户端自行下载中间链；根证书通常由客户端信任库提供。内部 CA 未加入客户端信任库时，应按组织的可信分发方式安装 CA，而不是永久跳过校验。

## 第四步：检查多入口或负载均衡证书不一致

同一域名对应多个入口时，逐个检查已知地址。下面保留请求域名、Host 和 SNI，只指定本次连接 IP：

```bash
curl --verbose --connect-timeout 3 --max-time 10 --resolve api.example.com:443:192.0.2.10 https://api.example.com/health
```

若仅某个入口返回过期证书，优先检查该节点的证书部署与重载。若所有入口正常而特定网络失败，核对实际出口代理、TLS 检查设备和该网络看到的 DNS 结果。直接用 IP 发起 HTTPS 可能选择默认站点，不能据此认定域名站点配置错误。

## 常见分支：证书续期成功，业务仍报过期

证书续期、文件分发、服务重载和客户端建立新连接是四个不同环节。先读取客户端收到的叶证书，再对照磁盘证书的有效期与序列身份：如果两者不同，排查入口进程是否加载了另一条路径、是否仍有旧节点提供流量，以及重载是否真正成功。不要仅根据续期任务退出成功就关闭告警。

服务端叶证书未过期，但验证链中的中间证书过期时，也可能产生过期错误。应根据验证输出中的具体证书层级定位，而不是无条件再次签发叶证书。同一个服务在不同客户端上的信任链选择可能不同，旧 CA 包、企业代理根证书或应用独立信任库都可能使结果不一致。

修复多入口证书时，先在单个节点验证新链、名称和健康响应，再逐步覆盖其他入口。若回滚证书部署，原证书必须仍有效且匹配当前域名；已经过期的旧证书不能成为恢复方案。应用使用自带信任库时，应更新其受控信任配置并重新验证实际请求，不以主机上的 curl 成功代替应用恢复。

## 处理方法与恢复验证

更新证书时保留原证书配置和部署版本，核对新证书的 SAN、有效期、私钥对应关系及完整链，再进行组件配置校验。以 Nginx 为例，`nginx -t` 成功后才按实际管理方式平滑重载；重载失败应恢复先前可工作的配置并检查日志，而不是反复重启入口。

私钥只应由必要的服务身份读取，不把私钥内容粘贴到日志或报告。对于协议不兼容，优先升级旧客户端或修正误配置，不以全局开启过时 TLS 协议作为默认处理。

恢复标准：普通 `curl` 在启用证书验证的情况下返回预期响应；证书名称、时间和链验证通过；所有实际入口一致；受影响的应用运行时也能访问。续期问题还需确认续期任务能够把新证书部署到运行节点，而不只是生成了本地文件。

## 易混淆情况

- `curl -k` 跳过证书验证，只会掩盖问题，不是恢复标准，也不应写入长期业务配置。
- 服务端磁盘文件更新和客户端实际收到新证书是两件事。
- 网关与上游之间还可能有独立 TLS；用户侧握手成功不能排除上游证书故障。
- mTLS 的客户端证书错误与普通服务端证书过期不同，要分别检查两个方向的身份验证。

## 官方参考

- [OpenSSL s_client 手册](https://docs.openssl.org/3.0/man1/openssl-s_client/)
- [OpenSSL x509 手册](https://docs.openssl.org/3.0/man1/openssl-x509/)
- [curl TLS 证书说明](https://curl.se/docs/sslcerts.html)
- [Nginx HTTPS 配置说明](https://nginx.org/en/docs/http/configuring_https_servers.html)
