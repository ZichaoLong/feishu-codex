# `focusctl` 命令矩阵

文档角色：中文规范源。英文同步副本：`docs/contracts/focusctl-command-matrix.md`。

本文定义本地 `focusctl` 管理面的正式命令面。

它只回答：

- `focusctl` 管哪些资源
- 哪些命令只读，哪些命令改状态
- 线程目标如何选择
- 它和飞书命令面如何对应

## 1. 总原则

- `focusctl` 是 FOCUS 本地管理面，不是第二个 Codex 前端。
- 想继续 live thread，用 `focus` 或 `fcodex`。
- 想装服务、修服务、管理实例、看 binding / thread / service 当前状态，或做本地 thread-scoped 管理，都用 `focusctl`。

## 2. 实例与目标选择

- 除 `instance ...` 这类全局实例目录命令外，其余实例相关命令都可加 `--instance <name>`。
- 显式 `--instance` 始终优先。
- 这里使用的命名实例必须已经先通过 `focusctl instance create <name>` 创建；`focusctl` 不会隐式创建它。
- 省略时按 `preferred-running -> unique-running -> default-running -> current-instance-paths` 规则解析。
- 解析出实例名或本地路径不等于证明 backend 存活。`thread list` 与所有 `--thread-name` selector 都要求
  目标 Focus 实例正在运行，并发布属于当前 owned backend generation 的 endpoint。实例已停止，或虽有
  running entry 却没有这份发布证明时，必须在发送任何上游查询或 mutation 前 fail-closed。
  `codex.yaml.app_server_url` 与 instance registry 缓存的 URL 都不能作为 liveness fallback 直接连接；
  同一 loopback 端口此时可能已被无关进程占用。live control plane 是唯一可供拨号的 endpoint 事实源：
  只有当前 websocket generation 已完成协议握手并处于 READY，且 owned guardian 仍存活时，
  `service/status` 才发布 URL。`app_server_runtime.json` 只证明 lifecycle/cleanup authority，不证明协议
  readiness，不能作为 endpoint fallback。只有下文单项命令合同明确写出的 ID-only offline 行为才可在
  实例停止时执行。
- `thread status`、`thread bindings`、`thread goal`、`thread attach`、`thread detach` 必须二选一：
  - `--thread-id <id>`
  - `--thread-name <name>`
- `thread clear-archived-bindings` 必须且只能提供 `--thread-id <id>` 或 `--all`；它不接受 `--thread-name`，避免为了删除本地 binding 再依赖上游 thread name 解析。
  - `--thread-id` 只按给定 thread id 删除指向它的本地 binding，不验证上游 archived 状态。
  - `--all` 会先通过一个运行中的实例查询上游 archived thread 列表，再删除命中的本地 binding；没有可用运行实例时 fail-closed，不修改本地数据。
- `thread archive` 支持两种目标形式：
  - 单线程：`--thread-name <name>` 或 `--thread-id <id>`
  - 批量：重复提供 `--thread-id <id>`；每个目标 thread 都独立按现有单线程 archive 语义路由、归档并清理本地 bindings
- `thread unarchive` 只接受 ID，可重复提供 `--thread-id <id>` 批量恢复。
- `thread delete` 只接受且只允许一个 `--thread-id <id>`。
- `focusctl` 不是 child 管理绕过入口。对显式 thread target 做变更前，`thread goal`、
  `archive`、`unarchive`、`delete`、`attach` 与 `detach` 都会权威核验目标必须是直接的、
  非 `ThreadSpawn` 的 root。直接指定一个 parent-owned `ThreadSpawn` child 必须 fail-close
  拒绝；`--force` 永远不改变这条规则。
- `focusctl` 不是 main-turn writer 绕过入口。可能启动工作的 control mutation 在存在 exact
  submission/active-turn lease 时必须拒绝；无身份本地调用者也没有 interaction delivery path，不能启动
  autonomous goal turn。
- 无身份 maintenance 例外很窄：没有冲突 submission/active-turn lease 且该事实可可靠核对时，
  `thread goal set --status paused`（可带
  objective）和 `thread goal clear` 可以执行。它们不建立 writer、不 resume、不订阅交互，也不启动 turn；
  只要 lease 冲突或核对失败，就必须拒绝。`active`、省略 status 的 goal set 以及其他可能继续执行的变更，
  必须由可取得 blank submission lease 的已连接 Web、飞书或 `fcodex` 前端发起。
- 把 detached Feishu binding 改为 `attached` 的 attach 不只是本地推送状态改写：它可能调用上游
  `thread/resume` 来建立 service 的 app-server subscription。发送前必须检查 exact main-turn lease，
  并判断 persisted goal 是否可能自主继续。已经 attached 的目标只是 observer 侧 no-op：不会发送
  resume，也不需要 submission admission。
- `focusctl service|binding|thread attach` 是无身份的本地 control 调用。它不能冒充目标中写出的
  Feishu binding；存在冲突 submission/active-turn lease，或 resume 可能自主启动但没有 delivery owner 时，
  external-control gate 必须拒绝。飞书 `/attach` 只能为**当前发起命令的 binding**取得所需 blank lease。
  `service attach` 必须逐个候选 root 核验，不能让一个获准 root 为另一个 root 授权。

## 3. 资源层

`focusctl` 分这些资源：

- `config`
- `instance`
- `service`
- `binding`
- `prompt`
- `thread`
- `image`
- `web`
- `skill`
- `migrate`
- `uninstall`
- `purge`

其中：

- `binding` 是 chat-scoped 视角
- `thread` 是 thread-scoped 视角

两者不要混读。

## 4. 命令表

### 4.1 `migrate`

| 命令 | 作用 | 类型 | 飞书对应 |
| --- | --- | --- | --- |
| `focusctl migrate from-feishu-codex` | 一次性把旧 `feishu-codex` 本地安装转移到 FOCUS | 变更 | 无 |

合同：

- 这是唯一支持的旧命名迁移入口。FOCUS 主路径不会读取旧 `feishu-codex` 路径、env 文件、wrapper、completion、service 或数据根。
- 这个迁移入口会按旧实现读取 `FC_*` 路径环境变量作为旧安装事实源，包括 `FC_CONFIG_ROOT`、`FC_DATA_ROOT`、`FC_ENV_FILE`、`FC_BIN_DIR` 以及 shell completion 路径覆盖。这些变量不是 FOCUS 运行时 fallback。
- 迁移是 transfer，不是兼容 fallback。成功后，活跃安装面和本地持久状态只归 FOCUS 所有。
- 它先停止并禁用旧 `feishu-codex` service，再复制本地状态，随后刷新新的 FOCUS wrapper、completion 与 service definition。
- 目标 FOCUS 输出路径，包括 config/data 根目录、env 文件、wrapper 目录、completion 文件、shell profile hook 和 service definition 目录，不能与旧 `feishu-codex` config/data/scheduled 根目录重叠。若重叠，迁移会在 preflight 阶段失败，因为刷新新安装面之后会归档旧根目录。
- 它迁移配置和非运行态本地持久状态：
  - `system.yaml`、`codex.yaml`、`init.token`
  - `feishu-codex.env` 改名为 `focus.env`，包括命名实例 env 文件
  - bindings、configured settings、terminal result raw store、群状态/日志，以及其他非运行态本地 store
  - Linux scheduled prompt timers，将 `feishu-codex-scheduled-*` 转移为 `focus-scheduled-*`
- scheduled prompt 正文只做安全文本替换，例如把 `feishu-codexctl` 改成 `focusctl`；如果正文里仍含具体旧 helper 路径或旧根目录，迁移会输出 warning，供人工检查。
- 它不迁移运行态：
  - PID / 进程状态
  - service lease 文件
  - instance registry
  - thread runtime lease
  - interaction lease
  - backend URL discovery
  - websocket / capability token
  - 正在执行的 turn 或内存队列
  - 受管 virtualenv 和日志
- 它会在 `~/.local/share/focus/migration-backups/feishu-codex-.../` 或平台等价 FOCUS 数据根下创建备份。该备份不是运行时 fallback 路径。
- 它 fail-closed。如果目标 FOCUS config/data 根已经包含非安装生成状态，迁移会停止，而不是合并两套活跃事实。关键阶段失败时，不删除旧安装面。

`bash install.sh --migrate-from-feishu-codex` 会安装新 FOCUS 包，然后调用同一个迁移实现。

### 4.2 `instance`

| 命令 | 作用 | 类型 | 飞书对应 |
| --- | --- | --- | --- |
| `focusctl instance create <name>` | 创建命名实例并准备配置、数据目录与 service 定义 | 变更 | 无 |
| `focusctl instance list` | 列出本机已知实例、service 状态、runtime 可用性、app-server 摘要与本地目录 | 只读 | 无 |
| `focusctl instance remove <name>` | 删除命名实例及其实例级 service 注册材料；不能删除 `default` | 变更 | 无 |

### 4.3 `service`

| 命令 | 作用 | 类型 | 飞书对应 |
| --- | --- | --- | --- |
| `focusctl [--instance <name>] service start` | 启动目标实例后台 service | 变更 | 无 |
| `focusctl [--instance <name>] service stop` | 停止目标实例后台 service | 变更 | 无 |
| `focusctl [--instance <name>] service restart` | 重启目标实例后台 service | 变更 | 无 |
| `focusctl [--instance <name>] service status` | 查看目标实例 service manager 状态；若 service 正在运行，则附带 best-effort runtime 摘要 | 只读 | 无一条完全等价命令 |
| `focusctl [--instance <name>] service autostart enable\|disable\|status` | 管理目标实例登录后自动启动 | 变更 / 只读 | 无 |
| `focusctl [--instance <name>] service log [--lines <n>]` | 查看目标实例日志并持续跟随 | 只读 | 无 |
| `focusctl [--instance <name>] service reset-backend [--force]` | 为恢复而重置当前实例 backend，但不重启 FOCUS service | 变更 | 飞书 `/reset-backend` |
| `focusctl [--instance <name>] service attach` | 尝试恢复当前实例内 detached 的 Feishu 推送；每个实际改变的 binding 都可能发送 `thread/resume`，并逐 root 核验 | 变更 | 飞书 `/attach service`，以及 reset 结果卡里的“附着当前实例” |

`service status` 是 service manager 视图，可重复传入多个 `--instance`。
当平台 service 正在运行时，该命令还会 best-effort 查询 FOCUS runtime，
并输出 `runtime: available` 及 control-plane / app-server / Web Gateway / binding / thread
诊断，或输出 `runtime: unavailable` 与原因。若 service running 但 runtime
unavailable，含义是平台 service manager 看到进程还活着，但 FOCUS
control plane 不可达；命令不会把它改写成 `service: stopped`。
Web Gateway 行只投影 live runtime 当前发布的 loopback 监听 origin；配置关闭时显示
`disabled`，配置启用但没有 active endpoint、或 runtime 整体不可读时显示
`unavailable`。该行不会输出 bootstrap credential 或 configured trusted-proxy
external origin。

`service reset-backend` 要求 live control plane。它的 `--force` 只接受当前运行中 backend generation 内的中断风险；
不能启动被 unresolved `app_server_runtime.json` 阻塞的 service，不能退休该 record，也不能补造缺失的 cleanup receipt。
在Focus自有control wire与飞书卡片action上，`force`缺失表示`false`；存在时必须是exact JSON boolean。truthy string、
integer、`null`、array与object都会在任何reset effect前被拒绝。

control response 报告 `ok: true` 时，`service reset-backend` 必须先验证完整七字段 reset result，之后才会输出
`backend reset: ok`。字段缺失、额外、错型或与请求不一致时，mutation outcome 为 unknown：命令不输出成功内容，返回退出码 `3`，
且不自动重试。再次决定操作前，应保留同一个实例选择并运行 `focusctl --instance <同一实例名> service status`；
该检查不能对账或证明上一笔请求的结果。

`service list` 有意不是命令；本机实例总览统一使用 `focusctl instance list`。

### 4.4 `binding`

| 命令 | 作用 | 类型 | 飞书对应 |
| --- | --- | --- | --- |
| `focusctl [--instance <name>] binding list [--refresh-names]` | 列出当前实例可见 binding，并显示缓存中的 chat 名称和存在时的权威 raw thread name；`--refresh-names` 显式刷新 chat display-name 缓存 | 默认只读；`--refresh-names` 可能调用飞书 / 联系人 API 并更新本地内存名称缓存 | 无 |
| `focusctl [--instance <name>] binding status <binding_id>` | 查看单个 binding 的 chat、thread、推送状态、next prompt、当前实例 interaction-owner/lease 诊断（只是派生的本地检查，不是 active-main-turn 权限）、会话设置 | 只读 | 飞书 `/status`、`/preflight` 的底层诊断面 |
| `focusctl [--instance <name>] binding attach <binding_id>` | 恢复单个 binding 的飞书推送；从 detached 变为 attached 时可能发送 `thread/resume` | 变更 | 飞书 `/attach binding` |
| `focusctl [--instance <name>] binding detach <binding_id>` | 暂停单个 binding 的飞书推送，但保留 binding 记录 | 变更 | 飞书 `/detach` 的 binding 级对应 |
| `focusctl [--instance <name>] binding clear <binding_id>` | 删除单个本地 binding 记录 | 变更 | 无 |
| `focusctl [--instance <name>] binding clear-all` | 删除当前实例下全部本地 binding 记录 | 变更 | 无 |
| `focusctl [--instance <name>] binding clear-stale [--dry-run]` | 删除指向已不可验证为可恢复 thread 的 stale binding 记录；默认扫描所有运行中实例和已知非运行实例，显式 `--instance` 时只作用于该实例 | 变更 | 无；这是本地 binding 记录修复 / 运维入口 |

`binding clear` / `clear-all` / `clear-stale` 不是 `detach`：

- `clear` 删除的是本地 binding 记录，包括其中保存的 thread 指向和 binding-local 设置
- `detach` 清的是当前飞书推送附着状态

`binding list` 是紧凑 inventory 视图。默认情况下，`CHAT` 列只使用本地
display-name 缓存；缓存未命中时回退短 id，不在默认 inventory 路径里调用飞书 /
联系人 API。若存在未命中，CLI 会提示可执行
`focusctl binding list --refresh-names` 手动刷新。

`--refresh-names` 会显式刷新这些缓存。单次请求内会按 chat/user 目标去重，然后
best-effort 调用对应飞书 / 联系人 API。群名读取要求飞书应用具备
`im:chat:readonly` 或等价 chat 读取权限；缺少时 group binding 会按预期显示短
chat id。

CLI 等待时间中的“刷新目标”指唯一外部查询目标：group binding 使用
`group:<chat_id>`，direct-chat binding 使用 `p2p:<sender_id>`。多个 binding
共享同一目标时只计一次。发起刷新请求前，CLI 会先取一次 cache-only 快照估算目标数，
再根据系统配置里的飞书 `request_timeout_seconds` 和少量本地余量计算 control-plane
等待预算。

`THREAD` 列显示短 thread id，并在存在时追加 app-server 的 raw `thread.name`；它不会回退到
binding-local `current_thread_title`，也不会显示 thread preview。

`binding clear-stale` 是保留逻辑，事实源是 cleanup 专用的 thread 可操作性检查，而不是普通状态展示：

- 它先通过运行中的 app-server 对 binding 指向的 `current_thread_id` 做 metadata-only `thread/read` presence check，不加载完整 turns/history。
- metadata-only `thread/read` 成功的 thread 视为保留对象；即使普通状态是 `notLoaded`，只要可读出 thread metadata，也不是 stale。
- 明确不可读、未加载且无持久 metadata、或只剩不可恢复 metadata 的 thread 视为 stale，删除对应本地 binding 记录。
- 查询失败、超时、协议错误或无法判断时 fail-closed：保留 binding 并在输出中列为 unknown。
- 运行中实例通过各自 service control plane 清理；已知但未运行的实例直接通过本项目的 binding store API 清理。
- archived thread 的精准清理由 `thread clear-archived-bindings` 负责；`binding clear-stale` 不把 unstable 路径字符串当作 archived 事实源。

### 4.5 `prompt`

| 命令 | 作用 | 类型 | 飞书对应 |
| --- | --- | --- | --- |
| `focusctl [--instance <name>] prompt send --binding-id <binding_id> (--text <text> \| --text-file <file>) [--synthetic-source <label>] [--display-mode silent\|announce]` | 通过目标实例的 control plane，向某个 binding 合成发起一轮新的 prompt turn | 变更 | 无；这是本地 control-plane synthetic prompt 入口 |

说明：

- `prompt send` 是 **binding-scoped**，不是 thread-scoped。
- 真正执行仍会经过当前服务内的 running-turn / attach / interaction 等保护。
- 目标 binding 当前不可写时，命令必须 fail-closed 返回拒绝原因，而不是静默排队。

### 4.6 `thread`

| 命令 | 作用 | 类型 | 飞书对应 |
| --- | --- | --- | --- |
| `focusctl [--instance <name>] thread list [--scope cwd\|global] [--cwd <path>] [--archived]` | 浏览 persisted thread；默认按当前目录过滤，`--archived` 改为浏览归档线程 | 只读 | 飞书 `/threads` 的目标发现面；飞书当前不提供 archived inventory |
| `focusctl [--instance <name>] thread status (--thread-id <id> \| --thread-name <name>)` | 查看某个 thread 的 backend 状态、live runtime owner / holders、bound / attached / detached bindings | 只读 | 无一条完全等价命令 |
| `focusctl [--instance <name>] thread bindings (--thread-id <id> \| --thread-name <name>)` | 查看某个 thread 当前关联的 binding 列表 | 只读 | 无 |
| `focusctl [--instance <name>] thread goal (--thread-id <id> \| --thread-name <name>)` | 查看某个 thread 当前 goal；这是默认 show 形态 | 只读 | 飞书 `/goal` |
| `focusctl [--instance <name>] thread goal set (--thread-id <id> \| --thread-name <name>) [--objective <text>] [--status active\|paused]` | 至少提供 `--objective` 或 `--status` 之一。结果为 `active` 或省略 status 时可启动 idle loaded turn，因此无身份控制拒绝；只有显式 `--status paused`（可带 objective）在 exact lease 核对后可作为 idle、不继续执行的 maintenance mutation。 | 变更 | 飞书 `/goal set <objective>` 有 binding 语义；会启动执行的本地 `active` 没有原始等价入口 |
| `focusctl [--instance <name>] thread goal clear (--thread-id <id> \| --thread-name <name>)` | 只在 exact direct-target 与 submission/active-turn lease 核对后清除 goal；这是不会继续执行的无身份 maintenance mutation。 | 变更 | 飞书 `/goal clear` |
| `focusctl [--instance <name>] thread archive (--thread-id <id> [--thread-id <id> ...] \| --thread-name <name>)` | 归档一个或多个目标 thread；归档成功后清理当前目标实例、其他可达运行实例，以及已知非运行实例里仍指向它的本地 bindings | 变更 | 飞书 `/archive` 的本地运维对应；批量和跨实例本地 binding 清理能力仅本地 CLI 提供 |
| `focusctl [--instance <name>] thread unarchive --thread-id <id> [--thread-id <id> ...]` | 逐项调用上游恢复一个或多个 archived thread；执行前拒绝仍被本机已知 Focus binding 引用的目标，成功后不创建 binding | 变更 | 无 |
| `focusctl [--instance <name>] thread delete --thread-id <id> [--force]` | 调用上游永久删除 root thread；上游可能级联删除 spawned descendants，Focus 不承诺完整预览该集合 | 破坏性变更 | 无 |
| `focusctl [--instance <name>] thread clear-archived-bindings (--thread-id <id> \| --all) [--dry-run]` | 删除已归档 thread 残留的本地 binding 记录；不调用上游 archive；`--thread-id` 删除指向指定 thread 的 binding，`--all` 先查询上游 archived 列表再删除命中的 binding；默认扫描所有运行中实例和已知非运行实例，显式 `--instance` 时只作用于该实例 | 变更 | 无；这是本地 binding 记录修复 / 运维入口 |
| `focusctl [--instance <name>] thread attach (--thread-id <id> \| --thread-name <name>)` | 恢复某个 thread 当前所有 detached bindings 的飞书推送；实际重新附着时可能发送 `thread/resume` | 变更 | 飞书 `/attach thread`，以及 reset 结果卡里的“附着当前线程” |
| `focusctl [--instance <name>] thread detach (--thread-id <id> \| --thread-name <name>)` | 暂停某个 thread 的飞书推送，同时保留 thread 与 binding 关系 | 变更 | 飞书 thread-scoped 的 detach 管理动作 |

说明：

- 本地 `thread detach` 走的是正在运行的 FOCUS 服务控制面。
- 底层实现仍可能调用上游 `thread/unsubscribe`，但这属于内部协议，不再作为用户命令名。
- `service attach`、`binding attach` 与 `thread attach` 在实际重新附着 detached binding 时会调用
  `thread/resume`，因此不是无条件 subscription 旁路。无身份的本地 attach 必须逐 root 通过 exact lease
  与 goal-continuation 核对，不能冒充目标 binding 或启动 autonomous work。等价的飞书 `/attach` 路径
  可为当前 binding 取得 blank submission lease，但不能 resume Web 或 `fcodex` 持有的 active turn。见
  [`root-operation-owner.zh-CN.md`](./root-operation-owner.zh-CN.md)。
- 如果请求中的 binding 全部已经 attached，attach 就是 no-op：不会发送 `thread/resume`，也不会取得
  writer authority。这与 Web 已定义的 observer-only read/subscription 路径不同。
- 本地 `thread archive` 的上游 Codex archive 只执行一次；archive 成功后，binding 清理分两层：
  - 运行中的其他实例走各自 service control plane，只清理本地 binding，不再次调用上游 archive。
  - 已知但未运行的实例直接通过本项目的 binding store API 删除同 `thread_id` 的 binding 记录；不直接手写 `chat_bindings.json`。
- 如果某个运行实例的本地清理因 running turn、pending request 或 control plane 不可达而失败，archive 已完成但命令返回非零，并在输出里列出 cleanup warning。
- `archive`、`unarchive` 与 `delete` 的 mutation 结果采用两个独立维度：
  - `upstream_outcome=success|error|unknown` 只表示是否拿到了上游 RPC 的明确结果；`success` 不表示 Focus 独立验证了完整 spawned subtree。
  - `focus_cleanup=complete|incomplete|skipped` 只描述本次命令发现的本机 Focus 状态；不代表其它机器或裸 Codex 前端。
  - 控制请求发送后发生 timeout、EOF、连接重置或损坏响应时返回退出码 `3`，不自动重试，也不清理 binding。
  - websocket 发送前发现的本地校验或 JSON 序列化错误属于明确的本地错误，不标记为 `unknown`。
  - 目标 lifecycle mutation 发送前发生的 app-server 连接或 `initialize` 失败同样属于明确的 pre-send 本地错误。
  - 控制请求已被接受后返回畸形 lifecycle result 时按 `unknown` 处理，并返回退出码 `3`。
  - JSON-RPC response envelope 必须是对象，并且只能包含 `result` 或结构合法的 `error` 之一；畸形 envelope 统一视为 protocol error，lifecycle mutation 按 `unknown` 处理。
  - lifecycle mutation 的 control timeout 会覆盖一次有界 startup 预算和操作的正常内部 RPC 链：unarchive 计两次（本实例 loaded inventory 加 mutation），按 id archive/delete 计三次；跨实例 loaded preflight 使用一个有界总 control-plane 预算，不会按实例数累加完整 timeout。
  - `thread archive --thread-name` 会先通过现有只读 app-server 分页列表解析唯一 thread id；只有解析后的 id 才进入 lifecycle 控制面，因此查询 timeout 或中断可以安全重试，不会留下仍在 service 中执行的 archive mutation。
  - 上游明确错误或上游成功但 Focus cleanup 不完整时返回退出码 `1`；明确成功且 cleanup 完成时返回 `0`。
- `thread unarchive` 与 `thread delete` 只接受 `--thread-id`，不提供 name selector，避免 archived/active 同名与 source 可见性产生歧义。
- `thread unarchive` 可重复提供 `--thread-id`；各目标逐项独立执行，普通失败计入汇总并继续，结果为 `unknown` 时立即停止，已成功项不回滚。
- `thread delete` 只允许一个 `--thread-id`；重复提供会在目标解析或 mutation 前报错，永久删除仍要求逐项确认。
- lifecycle mutation 会对 root thread 执行本机已知 Focus 实例的 loaded preflight：archive/delete 允许目标实例自身 loaded，但拒绝其他实例 loaded 或状态不可确认；unarchive 要求目标实例自身和其他已知实例都已确认 not-loaded。
- `thread unarchive` 的全机 binding/loaded 检查是 fail-closed 的 Focus 本地预检；它不会建立跨前端事务，也不会阻止裸 Codex 在检查后并发操作。
- `thread delete` 的确认对象只有 root ID。上游可能依据自身 persisted/live agent graph 级联删除 descendants；Focus 不把不完整的 descendant 查询当成确认集合。成功后只清理 root 的已知本地 bindings，遗漏项由 `binding clear-stale --dry-run` 检查。
- `thread archive` 与 `thread delete` 都会拒绝机器级 runtime lease 中仍有 `fcodex` 等非 service holder 的 root；delete 还会拒绝已知为 `active` 或 backend 状态无法可靠读取的 root。冲突 submission/active-turn lease 或不可读的 admission fact 同样拒绝 lifecycle mutation。
- 若上游已成功，但 interaction lease 无法释放，Focus 会保留 binding 记录作为明确的重试标记，并报告 `focus_cleanup=incomplete`。
- 若 binding 删除本身失败或无法确认完成，Focus 也会保留 service runtime lease，不会削弱跨实例所有权保护。
- `thread delete --force` 只跳过交互确认，不绕过 loaded、running、pending 或 unknown 等安全检查。
- 这些命令是上游公开 lifecycle API 的薄封装，只协调本机已登记的 Focus/fcodex runtime；不检测或锁定裸 Codex、IDE、独立 app-server 或其他机器，也不提供 rollout 导入/迁移、自动回滚、文件系统 scanner 或跨机器一致性保证。
- `thread clear-archived-bindings` 复用同一套本地 binding 清理逻辑，但不执行 archive。它用于补救旧版本残留、外部归档后的残留，或服务重启后无 live owner 时归档路由到其它实例造成的残留。
  - `--thread-id` 是显式修复入口；命令不会为了确认 archived 状态再查询上游。
  - `--all` 是 archived-aware sweep：先通过运行中的 app-server 调用上游 `thread/list archived=true` 收集 archived thread id，然后逐个复用本地清理逻辑。省略 `--instance` 时优先用运行中的 `default` 实例查询，若没有则按实例名选一个运行实例查询，并清理所有可见实例；显式 `--instance` 时该实例必须正在运行，且只清理该实例。
- 对已停止实例的直接 binding-store 修改会先取得与 service 共用的 maintenance ownership；若 service 正在启动或运行则拒绝修改。Handler 构造期只做只读 hydration，取得 service ownership 后再从磁盘替换式加载，避免启动与离线维护互相覆盖。

### 4.7 `image`

| 命令 | 作用 | 类型 | 飞书对应 |
| --- | --- | --- | --- |
| `focusctl [--instance <name>] image send --path <file> [--thread-id <id> \| --thread-name <name>]` | 把一张本地图片发送到目标 thread 当前所有 attached 的 Feishu bindings | 变更 | 无；这是本地控制面显式动作 |

### 4.8 `web`

| 命令 | 作用 | 类型 | 飞书对应 |
| --- | --- | --- | --- |
| `focusctl [--instance <name>] web open [--no-browser]` | 读取运行中目标实例发布的 loopback Gateway 状态，输出一次性浏览器引导 URL，并默认用系统浏览器打开 | 本地 UI 入口 | 无；这是所选 Focus 实例的 Web 前端入口 |

说明：

- `web open` 不负责启动 service 或 Gateway；目标实例必须正在运行，并配置
  `web_enabled: true`。
- 显式 `--instance` 只选择该实例；省略时沿用普通目标实例解析规则。
- `--no-browser` 只输出 URL，不调用系统浏览器。
- `web open` 只读取 runtime discovery 中的 loopback local endpoint；它不读取、选择、
  输出或打开 configured trusted-proxy external origin。
- Local bootstrap credential 放在 URL fragment 中，换取短期同源浏览器 session，
  使用后立即轮换。它不能经 external audience 消费。
- 浏览器 session cookie 按 canonical Focus instance name 隔离。在同一 loopback
  hostname 的不同端口依次认证多个本地实例，不得让后一实例替换前一实例的已认证
  session；bootstrap credential 仍是各实例独立的一次性凭据。
- 发布的 discovery endpoint 必须精确使用 canonical
  `http://127.0.0.1:<port>` 或 `http://[::1]:<port>` origin，不能带凭据、
  path、query 或 fragment；record 还必须保存非空且与当前 owner process
  精确匹配的 process incarnation。
- endpoint 畸形、incarnation 缺失、当前 incarnation unknown、owner liveness
  无法确认或 identity mismatch 时都不能输出 URL 或调用浏览器。identity 或
  liveness unknown 时保留 record 供诊断；只有已证明的 incarnation mismatch
  才可以把它作为 stale record 退休。
- Gateway 始终只监听 loopback。把 loopback port 直接 SSH 转发到浏览器仍是
  local mode；trusted-proxy mode 可由同机 HTTPS proxy，或 B 机 HTTPS proxy 经受保护的持久
  SSH tunnel 连到 A 机 loopback Gateway。
- Trusted-proxy 用户直接书签 configured HTTPS origin，不需要 `web open` 或 fragment token。
  Proxy proof 由 proxy 在完成自己的认证/ACL 后注入，永不交给浏览器。完整合同见
  [Focus Web 自部署外部访问决策](../decisions/focus-web-external-access.zh-CN.md)。
- Web 选择一个当前没有 subscriber 的 persisted thread 时，可能通过目标
  service 调用 `thread/resume` 建立 app-server subscription。封闭 preflight 证明没有 continuing goal 时，
  这是 observer-only，不会 claim writer；若 resume 可能继续 goal，只有 exact connected Web document 可
  取得 blank submission lease，其他 tab/device 会被拒绝，不能借 resume takeover。它们仍可读取已经 current
  的 projection。
- Web mutation 与其它 Focus 前端共用 RuntimeLoop、loaded gate、runtime lease 和 method-specific
  admission。普通 Web prompt 不读取或取得 main-turn lease；只有 lease-bearing exclusive/autonomous
  control mutation 才使用相应的 submission/active-turn admission。浏览器不会获得 app-server capability
  token，也不会直连 app-server。

### 4.9 `uninstall` 与 `purge`

| 命令 | 作用 | 类型 | 飞书对应 |
| --- | --- | --- | --- |
| `focusctl uninstall` | 停止并卸载所有已知实例的 service 定义、自启动注册、wrapper、completion 与受管 `.venv`；保留配置和其他数据 | 变更 | 无 |
| `focusctl purge` | 在完成同一卸载流程后，删除经验证的机器级 FOCUS config/data 根目录 | 破坏性变更 | 无 |

安全合同：

- install、uninstall 与 purge 由同一机器级 lock owner 串行化。uninstall/purge 对运行实例执行与 installer 相同的 idle-only
  admission；active、pending 或状态 unknown 时在卸载任何 service 前拒绝，没有 force，也不会替用户中断工作。
- 安装和 repair 会在 config/data 根分别原子写入私有的
  `.focus-managed-root`。marker 记录自己的 `role` 与 canonical target；它是
  “这棵叶目录由 FOCUS 安装布局管理”的身份事实，不是仅凭目录名进行的猜测。
- `purge` 只接受实例布局解析出的两个独立受管叶目录。它在任何 service、wrapper
  或数据变更前解析 canonical path，并拒绝文件系统根、用户主目录、当前工作目录、
  FOCUS 源码仓库及其父目录、过宽的浅层目录、符号链接根，以及相同或互为父子的
  config/data 根。只有至少位于文件系统根下两层、且浅层叶目录名明确包含 `focus`
  （或位于更深层布局中）的自定义根才进入 marker 核验。
- 目标目录不存在时按幂等 no-op 处理；目标已经存在时，marker 必须存在、不能是
  symlink，并且 `role` 与 canonical target 必须完全匹配。旧安装若没有 marker，先从
  当前源码运行 `bash install.sh` 或 `./install.ps1` repair；`uninstall` 删除 `.venv` 前也要求现有 data root
  带 matching marker，二者都不会把一个无 marker 的现有目录自动认作可删目录。
- 任一实例的 service uninstall、共享 service 定义卸载、卸载后状态核验或停止后
  offline-maintenance 所有权核验失败时，命令立即失败，不删除 wrapper、配置或数据。该锁核验与运行中
  service 使用同一份实例锁，避免把“uninstall 调用返回”误当成“service 已经退出”。
- config/data 删除不使用 `ignore_errors`。任一 `rmtree` 失败都会返回非零并报告已经
  删除了哪些根，绝不输出完整成功；两个目录的删除不是跨文件系统事务，前一个已经
  成功的删除不会回滚。
- `uninstall` 把固定 `.venv` 视为程序安装面并删除，但保留其余 config/data，以便未来重装恢复用户状态；`purge`
  才删除两个受管根。二者都不遍历源码仓库、上游 Codex 数据或各工作区 skills。
- Windows 不能由 `.venv` 内正在运行的 Python 证明删除自身。该平台把 exact canonical target 交给临时
  PowerShell helper。父进程仍持主锁时先把同一 lock owner 的 handoff barrier 移交给 helper；helper 返回 matching
  armed proof 后，父进程才释放主锁并退出，因而并发 install 不能抢进“父进程退出—helper 删除”空窗。helper 随后删除
  并写入逐 target 的 `deleted` / `missing` / `failed` result。父进程成功只表示 handoff 已可靠提交，会输出 helper PID
  与 exact result path；不能把 armed proof 写成删除已经完成。

### 4.10 `config`

| 命令 | 作用 | 类型 | 飞书对应 |
| --- | --- | --- | --- |
| `focusctl [--instance <name>] config` | 打印目标实例的 `system` / `codex` / `init-token` 路径与机器级 `env` 路径 | 只读 | 无 |
| `focusctl [--instance <name>] config system\|codex\|init-token [--open]` | 打印一个实例级配置文件路径；`--open` 用本地编辑器打开 | 只读 / 显式编辑入口 | 无 |
| `focusctl config env [--open]` | 打印共享 `focus.env` 路径；`--open` 用本地编辑器打开 | 只读 / 显式编辑入口 | 无 |

合同：

- `system`、`codex` 与 `init-token` 是实例级目标；命名实例必须已创建。
- `env` 属于机器级共享配置，不会因 `--instance` 而产生实例副本。
- `--open` 只是显式进入用户选定的本地编辑器；`focusctl` 本身不改写配置内容。

### 4.11 `skill`

| 命令 | 作用 | 类型 | 飞书对应 |
| --- | --- | --- | --- |
| `focusctl skill install` | 把 FOCUS 打包的 workspace skills 安装到当前目录的 `.agents/skills` | 变更 | 无 |
| `focusctl skill uninstall` | 从当前目录删除 FOCUS 明确标记为受管安装的 workspace skills | 变更 | 无 |

合同：

- `skill` 是 current-working-directory scoped，不接受顶层 `--instance`。
- 安装不会覆盖内容不同的非 FOCUS 受管目录；卸载也不会删除未带 FOCUS
  受管 marker 的 skill。
- 机器级 `focusctl uninstall` / `purge` 不遍历、不删除各工作区中已安装的 skills。

## 5. 与飞书命令面的对应关系

| 本地命令 | 飞书侧最接近入口 | 关键差异 |
| --- | --- | --- |
| `service reset-backend` | `/reset-backend` | 都是实例级 backend 管理；一个是 CLI，一个是飞书卡片流 |
| `service attach` | `/attach service` | 两者都会为实际改变的 detached binding 发送 resume；本地控制面没有 binding 连续性证明，必须逐 root 过 gate，飞书只能证明当前发起 binding |
| `binding status <binding_id>` | `/status`、`/preflight` | 本地输出更底层，带 binding id、reason code 与当前实例 interaction-owner/lease 详情；它不是 active-main-turn 权限报告 |
| `binding attach <binding_id>` | `/attach binding` | 两者都可能调用 `thread/resume`；本地虽可按任意 id 定位，但两个入口都不能绕过既有 blank/active main-turn lease，飞书默认只作用于当前 binding |
| `binding detach <binding_id>` | `/detach` | 飞书 `/detach` 只作用于当前 chat；本地可直接按任意 binding id 定位 |
| `prompt send --binding-id <binding_id>` | 无 | 本地可以从 service control plane 合成一条未来或系统触发的 prompt；飞书侧当前没有等价 slash 命令 |
| `thread attach --thread-id/--thread-name` | `/attach thread` | 两者都可能调用 `thread/resume`；飞书范围限于当前 binding 的 thread，本地无身份控制可定位任意 direct root，但不能取得或替换另一 surface 的 blank/active main-turn lease |
| `thread detach --thread-id/--thread-name` | 无一条完全等价的飞书命令 | 飞书 `/detach` 是当前 chat binding 级；本地 thread 级动作会批量影响该 thread 当前所有 attached bindings |
| `thread goal --thread-id/--thread-name` | `/goal` | 飞书只作用于当前 chat 当前 thread；本地 CLI 是 thread-scoped 调试 / 运维面，可直接读取任意目标 thread 的 goal |
| `thread goal set/clear` | `/goal set`、`/goal clear` | 飞书命令有 binding 语义，本地 CLI 定位显式 thread。goal set 结果为 `active` 或省略 status 时可继续 idle loaded turn，因而必须取得普通 blank main-turn lease；显式 paused set/clear 是同步 control mutation，不创建 main-turn writer |
| `thread list --scope cwd` | `/threads` | 飞书是聊天入口；本地只是线程发现面 |
| `thread status` | `/status`、`/preflight`、`/attach`/`/detach` 的底层诊断 | 本地是 thread-scoped 调试面 |
| `web open` | 无 | 打开所选实例的 loopback Web 前端；它不是飞书命令，也不是 app-server 直连入口 |
| `migrate from-feishu-codex` | 无 | 只做一次性本地安装 / 数据转移，不是运行时命令 |

## 6. 边界

下列期待当前不成立：

- 不能把 `focusctl` 理解成飞书 `/threads` 的本地 UI
- 不能期待 `focusctl` 进入 Codex TUI
- 不能把 `focusctl migrate from-feishu-codex` 理解成持续兼容层
- 不能把 `binding clear` 理解成 “停掉当前线程推送”
- 不能把 `thread goal set` 理解成有 binding 语义的 runtime 恢复 / 暂停命令，它不承诺 settings sync；结果为 `active` 或省略 status 时**可能**立即继续一个 idle loaded turn，因此必须取得普通 blank main-turn lease；显式 `--status paused` 只在不存在 blank/active main-turn lease 且 direct-target/runtime 检查通过时，才是唯一无身份 non-continuing maintenance exception
- 本地 goal/settings 控制请求不能绕过 Web、飞书或 `fcodex` 的 exact submission/active-turn lease；只有 idle 时显式 paused goal set 或 clear 是安全的无身份 maintenance exception

如果新增、删除、改名任何 `focusctl` 子命令，或改变参数约束、实例解析规则、与飞书面的对应关系，必须同步更新本文。
