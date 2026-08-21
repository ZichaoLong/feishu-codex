# Focus Web 自部署外部访问

文档角色：中文规范源。英文同步副本：`docs/decisions/focus-web-external-access.md`。

> 状态：已采纳。Focus Web 默认仍只提供 loopback local mode；只有完整、显式的
> trusted-proxy 配置才启用本文定义的 external audience。Gateway 在两种模式下都不监听
> non-loopback address。

## 1. 要解决的问题

部署者需要从手机或其他机器访问仍只监听固定 loopback port 的 Focus Gateway。HTTPS proxy
可以与 Focus 同机，也可以在 B 机器终止 HTTPS，再通过受保护的持久 SSH tunnel 连接 A
机器的 loopback Gateway。这个需求不能通过暴露 Codex app-server、放开 Gateway listener，
或盲信浏览器 header 完成。

## 2. 部署与 authority

- `web_host` 始终是 loopback；trusted-proxy mode 要求显式固定的非零 `web_port`。
- 每个实例只接受一个使用 non-loopback host 的 canonical HTTPS external origin，不接受
  `localhost` / `.localhost`、loopback IP、wildcard、path-based instance routing 或多条候选
  authority。host 可以是 canonical DNS、IPv4 或 compressed IPv6 literal。Focus session cookie
  按 instance name 命名，但 external authority 仍按 external host
  而不是 port 划分；需要同时访问多个实例时必须使用不同 DNS name 或 IP literal，不能只给同一
  host 配不同 port。
- Proxy 到 Gateway 的 hop 必须是同机 loopback，或是两端都只暴露 loopback 的加密 SSH
  tunnel。Gateway 不根据 source IP 或网卡位置猜测 proxy。
- 代理产品、外部网络和用户认证方式由部署者选择；TLS/certificate 以及 Basic Auth、OIDC、mTLS
  或私网 ACL 等认证边界都由 proxy/network 拥有。Focus 不假设任何特定产品。
- app-server 继续是 Focus-owned loopback/capability-token 后端；浏览器只连接 Gateway。

External authority 只来自已校验的 canonical config、exact `Host` / HTTPS `Origin` 和
Focus-specific proxy proof。`request.remote`、source=localhost、`Forwarded`、全部
`X-Forwarded-*` 以及 proxy 产品专属 header 一律不参与准入或 cookie 决策。

## 3. Proxy proof、label 与信任域

部署者为每个 Focus instance 生成独立的 32-byte random URL-safe proof。原始 proof 只由
proxy 的 secret manager 保存；Focus 配置只保存其 64 位小写 SHA-256 verifier。原始值不得
进入 runtime discovery、URL、cookie、HTTP/WebSocket response、event、日志或 durable
runtime store。

Proxy 只有在完成自己的认证/ACL 后才可注入准入 header。它必须先删除客户端传入的同名
proof 与 label header，再为每个 HTTP request 和 WebSocket upgrade 注入 exact proof 与
一个非空、长度受限的 opaque proxy label。浏览器不能自报 label。

该窄 wire 只有两个 header：`X-Focus-Trusted-Proxy-Proof` 必须是 32-byte random proof 的
43 位 URL-safe no-padding 编码；`X-Focus-Trusted-Proxy-Identity` 只是上述 opaque label，必须匹配
`[A-Za-z0-9][A-Za-z0-9._:@/+~-]{0,127}`。历史命名中的 `Identity` 不把 label 提升为 Focus
身份事实。

Focus 只证明“配置的 proxy 对该 opaque label 作出了声明”。Label 不是 Focus 验证的真实
姓名、email、device identity、administrator role、per-user ACL 或完整审计事实。同一 proxy
信任域的已准入主体都是本 Focus instance 的完全信任协作者；若需要用户间隔离，应使用独立
instance、OS account 或 container，而不是在 label 上补一层虚假 ACL。

## 4. Session audience 与浏览器准入

Local bootstrap 保持 local-only。External 首次认证复用现有
`POST /api/client/register`：只有 exact external `Host`、exact HTTPS `Origin`、
constant-time proof 校验与 bounded label 同时成立时，request admission 才为该 request
临时签发 external session。Handler 成功后才设置
`Secure; HttpOnly; SameSite=Strict; Path=/` cookie；handler 失败立即 revoke，不留下可用
session。

Session audience 只保存 local/external 边界、exact external origin 和 opaque proxy label。
后续 external HTTP request 与 WebSocket upgrade 必须重新通过 proof 与 label 校验，并与
session audience 精确一致。Local/external、不同 origin 或不同 label 的 session 都不能
互用。Proof 本身不是 writer、interaction-response、backend 或 control-plane credential；
已注册 Web document 仍遵守现有 RuntimeLoop、document 与 root-operation 准入。

External 页面 URL 是可书签化的 configured origin，不含 fragment token。
`focusctl web open` 和 runtime discovery 仍只发布 loopback local endpoint，不发布 external
origin、proof 或 label。运行中 session 失效或 service 重启后，当前合同只保证用户显式
刷新页面或重新打开书签即可经 proxy 重新注册；不保证无操作 silent refresh，也不自动重放
mutation、steer、approval 或其他可能已有 effect 的请求。

认证失效表面的显式恢复动作必须 reload 整个 document，不能在已完成 identity registration
的旧 document 内再次调用普通 load。该表面同时诚实说明：external 用户刷新/重开书签；
local 或 SSH local-forward 用户运行 `focusctl [--instance <name>] web open` 并使用新 URL。
页面不根据 hostname 猜当前部署模式，也不把 reload 变成 effect replay。

## 5. 容量、撤销与失败边界

Proxy 拥有未认证 edge 的 auth-attempt、request、connection 与 WebSocket
rate/concurrency 限制。Gateway 保留现有 request body、upload、WebSocket message 和 event
queue 硬上限，并对 proof-driven external session 与 socket 签发应用 code-defined 固定
上限与 `429`：每进程至多 128 个 external session，每 external session 至多 8 个
WebSocket，且新 external socket 只在当前 Gateway socket 总数小于 128 时才准入。Focus 不再建立
per-user token bucket、timer、可调限流 schema 或 durable counter。

Service 只在启动时捕获 trusted-proxy config snapshot。Rotate/revoke 通过替换 verifier 并
计划内重启完成；这会撤销进程内 session，并可能带来短暂不可用。Focus 不为零停机引入双
credential slot、live reload、watcher 或 recovery state。

Tunnel 或 proxy 不可用时，external 访问直接失败；Focus runtime 和 local Web 不回退到明文
网络，也不自动放开 listener。若 B 的 tunnel 错接到另一实例，该实例不同的 proof verifier
必须在任何 session 或 handler effect 前拒绝请求。

## 6. 明确非目标

- non-loopback bind 或 Focus-owned direct TLS listener
- 明文公网 HTTP 或无可验证 TLS 的 external audience
- device bearer、durable pairing、silent device refresh 或浏览器长期 credential
- Focus 内建密码/PIN、per-user ACL、administrator role 或用户身份审计
- source-IP trust、arbitrary forwarded-header trust 或 proxy-vendor 特例
- app-server、thread、turn、writer、approval 或 interaction authority 的新合同

## 7. 最小部署清单

1. 为该实例生成独立 proof 与 verifier。下面的跨平台 Python 命令只把原始 proof 与其
   SHA-256 打印到当前终端；把原始值写入 proxy 的 secret 管理，把 verifier 写入
   `codex.yaml`，不要把原始值写入 Focus 配置、URL 或仓库：

   ```text
   python -c "import hashlib,secrets; p=secrets.token_urlsafe(32); print('proof='+p); print('sha256='+hashlib.sha256(p.encode('ascii')).hexdigest())"
   ```

2. 在实例配置中设置 `web_enabled: true`、loopback `web_host`、固定非零 `web_port`、exact
   `web_trusted_proxy_origin`（canonical DNS、IPv4 或 compressed IPv6 HTTPS origin）与
   `web_trusted_proxy_proof_sha256: <verifier>`，然后重启该实例。
   `focusctl web open` 仍只用于 local mode，不会输出 external URL。
3. HTTPS proxy 必须先完成部署者选择的认证/ACL，再删除客户端传入的
   `X-Focus-Trusted-Proxy-Proof` 与 `X-Focus-Trusted-Proxy-Identity`，并为每个 HTTP request
   与 WebSocket upgrade 注入 secret proof 和一个固定、有界 opaque label。Proxy 保留浏览器
   的 external `Host` / `Origin`，把流量转发到该实例的 loopback port；不得用 forwarded
   header 代替这两个 Focus header。
4. Proxy 与 Focus 同机时直接连接 loopback。Proxy 位于 B、Focus 位于 A 时，先建立由服务
   管理器维持并固定 host key 的 B-loopback → SSH → A-loopback tunnel，再让 B 的 proxy
   只连接该 B-loopback 端点；两端都不得开放 Gateway port。
5. 直接书签 external HTTPS origin。长期登录、简单密码/PIN、OIDC、私网成员与用户撤销由
   proxy/私网拥有；Focus 不复制这些 device 或 ACL 事实。页面若报告 session 失效，显式刷新
   即可，不需要重新取得 Focus bootstrap token。

## 8. 部署检查与诊断

- 修改 trusted-proxy 配置或 verifier 后重启目标实例，再运行
  `focusctl --instance <name> service status`，确认 runtime 可用且 Web Gateway 正在配置的固定
  loopback port 上监听。该状态面刻意不显示 external origin、proof 或 label。
- 从外部只打开 configured HTTPS origin；不要把 `focusctl web open` 生成的 loopback bootstrap
  URL 交给 proxy，也不要期待该命令输出 external URL。
- `400 Invalid external Host header` 表示请求的 `Host` 未精确匹配已加载的 external authority；
  `403 Invalid trusted proxy proof/identity` 或 cross-origin 拒绝表示 proof、label 或浏览器
  `Origin` 未通过准入；已有页面后续收到 `401` 时，按第 4 节显式 reload 整个 document。
- 排障时按顺序核对：proxy upstream 指向正确实例的固定 loopback port；外部 `Host` 保持 exact；
  浏览器实际发送 `Origin` 时原样转发；客户端同名 Focus header 已删除；两个 Focus header 已在
  每个 HTTP request 和 WebSocket upgrade 上重新注入；raw proof 的 SHA-256 与 Focus verifier
  一致。`Forwarded` / `X-Forwarded-*` 不能补救任一项准入失败。
- 不要启用会记录 request header、proxy secret 或完整 upstream request 的诊断日志。若必须定位
  proof 问题，只比较 verifier，并在任何疑似泄露后 rotate proof、替换 verifier，再计划内重启
  目标实例。
