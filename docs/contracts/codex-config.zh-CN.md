# `codex.yaml` 准入合同

文档角色：中文规范源。英文同步副本：`docs/contracts/codex-config.md`。

## 目的

`codex.yaml` 同时承载 app-server adapter、飞书 handler、Focus Web gateway
和本地管理客户端的实例默认值。这个边界上的拼写错误或 Python 隐式转换可能改变
安全默认值：例如 `bool("false")` 实际为真，拼错的 `approval_policy` 也可能在
过去静默回落成 `never`。

本文把 component config 准入定义成一个明确、fail-closed 的边界。它不定义
upstream `~/.codex/config.toml` 或 binding 持久化 runtime setting；Web
`WebNextTurnSettings` 的持久化与生效语义由 runtime-settings 合同定义，而不是由 YAML schema 推导。

## 权威源与投影

`bot.codex_config.CodexConfig` 是 `codex.yaml` 可接受键清单、默认值和类型解析的
权威事实源。

- `config/codex.yaml.example` 是这份 schema 的人类可读投影；回归测试要求其记录的
  键清单与 parser 清单完全相等。
- `CodexAppServerConfig` 只是已校验 `CodexConfig` 的窄运行时投影，不是第二份 YAML
  schema。
- `FocusRuntime`、`focus` / `fcodex` 和 runtime 管理 adapter 路径必须先校验完整
  component document，之后才能消费任一字段。

因此，新增设置必须同时修改 `CodexConfig` 字段/parser、example 投影和针对性 schema
测试。在 consumer 里新增一处 `dict.get()` 转换不构成正式支持。

## 准入规则

- 文档顶层必须是以字符串为键的 mapping。任何层级的重复 mapping key、未知顶层键、
  显式 null 和已移除键都拒绝；重复键不能以“最后一个值生效”解释。
- 字符串、布尔值、整数、数字、字符串列表是不同类型。尤其是 `"false"` 这样的
  字符串不是布尔值，布尔值不是整数，一个标量字符串也不是单元素列表。
- 字符串和 `source_kinds` 列表项在保存为 typed config 前统一去除首尾空白；内部
  空格不变。合同允许为空的字段（例如 model、service tier、effort）在去除空白后
  仍可为空；要求非空的字段则直接拒绝。
- 数字必须有限，并满足该字段声明的运行范围。只有合同明确赋予空值语义的字段才
  接受显式空值，例如表示自动选择的 model 或 effort。
- Focus service 始终拉起并拥有同机 app-server；`app_server_mode` 已从产品配置面
  删除。`app_server_url` 只是这个受管 child 的首选 listen 地址，必须符合上游
  `codex app-server --listen`：只接受带非零 port、无 path 的
  `ws://loopback-IP:port`，不接受 `wss`、主机名、非 loopback listener、内嵌凭据、
  query 或 fragment。
- `focusctl` 与 `focus` / `fcodex` 仍可连接本实例已经发布的 endpoint；这是从实例
  获得的内部 `attached_endpoint` client capability，不是第二种 deployment mode，
  也不能通过 `codex.yaml` 指向外部 app-server。runtime registry 只负责选择运行实例
  及其 data directory；唯一可拨号的 endpoint 来源是该实例经过认证的实时
  `service/status` 响应，且必须已经完成协议 READY 与 replacement-generation admission。
- Web Gateway 始终只接受 `127.0.0.1`、`localhost` 或 `::1` 作为 `web_host`；
  `web_session_ttl_seconds` 至少为 60 秒。它们是 schema 准入规则，Gateway 中的
  同值检查只是防御性运行时断言，不能成为另一份配置合同。
- `source_kinds` 必须是非空字符串组成的非空列表，绝不能把一个标量字符串按字符
  拆开。
- `approval_policy` 是封闭的本地枚举；唯一保留的旧值迁移是权限合同中记录的
  `on-failure` 到 `on-request`。
- `approvals_reviewer` 保持为 `user`，与 Focus 已审阅的用户交互
  路由一致。`thread/start` / `thread/resume` 必须显式发送该值，并校验响应仍为
  `user`；只检查 server requirements 不足以覆盖 thread 中持久化的旧 reviewer。
  不能只因为新版 app-server 暴露了 upstream auto-review 枚举，就在本项目中自动启用它。
- `personality` 与当前 app-server 枚举一致，只接受 `friendly`、`pragmatic` 或
  `none`；任意自定义字符串不能延迟到发起 thread/turn 时才失败。
- `permissions_profile_id` 是 `codex.yaml` 唯一接受的 permissions 键。binding store
  对旧 `sandbox` 的持久化兼容，不会让 `sandbox` 自动成为 component config 别名。
  其值只接受 `:read-only`、`:workspace`、`:danger-full-access` 三个 Focus 已建模
  built-in；尚未建立 cwd/availability 合同的 upstream custom profile 不接受自由字符串。

### Focus Web 部署显示名称

`web_display_name` 是 Focus Web 标签页使用的实例配置字符串：

- 未配置时固定默认为 `Focus Web`；不同 Focus instance 使用同一默认值，不从 instance name、操作系统
  hostname、浏览器访问 hostname 或 trusted-proxy origin 猜测替代值；
- 显式值按普通 non-empty string 准入：去除首尾空白后不能为空；
- service 启动时捕获该值，并通过 Focus Web meta 投影给当前浏览器；修改配置后需要重启 service 并重新加载页面，
  不建立 live reload。

该字段只是部署显示标签，不是静态完整页面标题。当前会话标题的选择、顺序和浏览器展示语义由
[Focus Web wire 合同](focus-web-wire.zh-CN.md) 定义。

### Trusted-proxy Web 配置

`web_trusted_proxy_origin` 与 `web_trusted_proxy_proof_sha256` 是 trusted-proxy mode 唯一新增的
canonical config scalar：

- 两者默认都为空字符串，表示禁用 trusted-proxy mode；必须同时为空或同时有值。
- 两者都是 exact string，parser 不会静默 trim 或改写大小写。
- `web_trusted_proxy_origin` 必须是
  `bot.network_contract.parse_trusted_proxy_external_origin` 接受的唯一 canonical HTTPS origin：
  host 只能是 lowercase ASCII DNS labels（末尾 label 不能是 WHATWG IPv4 number）或 strict canonical
  IPv4 / compressed IPv6 literal；默认 443 port 必须省略。尾点、Unicode/percent/backslash host、legacy IPv4、
  非压缩或 IPv4-mapped IPv6、`localhost` / `.localhost`、loopback/unspecified IP、wildcard、credential、
  path、query、fragment 与多个 origin 都拒绝。
- `web_trusted_proxy_proof_sha256` 必须是原始 32-byte random URL-safe proxy proof 的
  64 位小写十六进制 SHA-256 verifier。原始 proof 不是 Focus 配置值。
- 启用该模式还必须同时配置 `web_enabled: true` 与固定非零 `web_port`；`web_host`
  仍必须是 loopback。任一 cross-field 条件不成立都拒绝整份 component config，不回退到部分启用。

两个值只在 service start 时捕获一次；它们不进入 runtime discovery，也不建立 live reload
或双 verifier 过渡。部署、proxy proof、opaque label 与 session audience 语义只由
[Focus Web 自部署外部访问决策](../decisions/focus-web-external-access.zh-CN.md) 定义。

配置非法时，对应入口必须用包含字段名的诊断直接停止；不得回落默认值、部分启动
service，也不得静默改写值的含义。

## 与 runtime setting 事实源的关系

校验后的实例值按 `runtime-settings-fact-sources.zh-CN.md` 所述提供两类 seed：新飞书 binding 的
安全/runtime 初始值，以及 service start 时尚无 durable record 的 instance-wide `WebNextTurnSettings`。service
只在启动时捕获后者；Web read/turn 不重读或镜像配置，首次显式修改创建 record，后续重启 persisted record 优先。
binding 自己的值一旦落盘，下一轮事实源仍是该 binding。严格解析不会把实例配置合并进已有 binding 或已有 Web
record，也不会建立 Feishu、Web 与本地 TUI 之间的设置同步。

## 兼容性结果

过去依赖隐式转换的配置现在会在启动或 CLI 入口直接失败，包括拼错的键、加引号的
布尔值/数字、标量 `source_kinds`，以及旧 component 键 `permissions`、`sandbox` 或
`app_server_mode`。旧 remote deployment 配置不会被猜测迁移；删除该 mode 键后，
`app_server_url` 也必须改回本机 loopback listener。
修复方式是使用 `codex.yaml.example` 展示的类型和拼写；继续静默保留旧解释只会保留
本合同要消除的歧义。
