# `focusctl` 命令矩阵

英文原文：`docs/contracts/focusctl-command-matrix.md`

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

## 3. 资源层

`focusctl` 分这些资源：

- `config`
- `instance`
- `service`
- `binding`
- `prompt`
- `thread`
- `image`
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
| `focusctl [--instance <name>] service attach` | 恢复当前实例内所有可恢复的 detached Feishu 推送 | 变更 | 飞书 `/attach service`，以及 reset 结果卡里的“附着当前实例” |

`service status` 是 service manager 视图，可重复传入多个 `--instance`。
当平台 service 正在运行时，该命令还会 best-effort 查询 FOCUS runtime，
并输出 `runtime: available` 及 control-plane / app-server / binding / thread
诊断，或输出 `runtime: unavailable` 与原因。若 service running 但 runtime
unavailable，含义是平台 service manager 看到进程还活着，但 FOCUS
control plane 不可达；命令不会把它改写成 `service: stopped`。

`service list` 有意不是命令；本机实例总览统一使用 `focusctl instance list`。

### 4.4 `binding`

| 命令 | 作用 | 类型 | 飞书对应 |
| --- | --- | --- | --- |
| `focusctl [--instance <name>] binding list [--refresh-names]` | 列出当前实例可见 binding，并显示缓存中的 chat 名称和存在时的权威 raw thread name；`--refresh-names` 显式刷新 chat display-name 缓存 | 默认只读；`--refresh-names` 可能调用飞书 / 联系人 API 并更新本地内存名称缓存 | 无 |
| `focusctl [--instance <name>] binding status <binding_id>` | 查看单个 binding 的 chat、thread、推送状态、next prompt、当前实例 interaction owner、会话设置 | 只读 | 飞书 `/status`、`/preflight` 的底层诊断面 |
| `focusctl [--instance <name>] binding attach <binding_id>` | 恢复单个 binding 的飞书推送 | 变更 | 飞书 `/attach binding` |
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
| `focusctl [--instance <name>] thread goal set (--thread-id <id> \| --thread-name <name>) [--objective <text>] [--status active\|paused]` | 对某个 thread goal 执行原始 persisted 状态改写，供调试或运维使用；至少提供 `--objective` 或 `--status` 之一 | 变更 | 写 objective 时最接近飞书 `/goal set <objective>`；原始 `--status active\|paused` 改写没有精确飞书等价物 |
| `focusctl [--instance <name>] thread goal clear (--thread-id <id> \| --thread-name <name>)` | 清除某个 thread 当前 goal | 变更 | 飞书 `/goal clear` |
| `focusctl [--instance <name>] thread archive (--thread-id <id> [--thread-id <id> ...] \| --thread-name <name>)` | 归档一个或多个目标 thread；归档成功后清理当前目标实例、其他可达运行实例，以及已知非运行实例里仍指向它的本地 bindings | 变更 | 飞书 `/archive` 的本地运维对应；批量和跨实例本地 binding 清理能力仅本地 CLI 提供 |
| `focusctl [--instance <name>] thread unarchive --thread-id <id> [--thread-id <id> ...]` | 逐项调用上游恢复一个或多个 archived thread；执行前拒绝仍被本机已知 Focus binding 引用的目标，成功后不创建 binding | 变更 | 无 |
| `focusctl [--instance <name>] thread delete --thread-id <id> [--force]` | 调用上游永久删除 root thread；上游可能级联删除 spawned descendants，Focus 不承诺完整预览该集合 | 破坏性变更 | 无 |
| `focusctl [--instance <name>] thread clear-archived-bindings (--thread-id <id> \| --all) [--dry-run]` | 删除已归档 thread 残留的本地 binding 记录；不调用上游 archive；`--thread-id` 删除指向指定 thread 的 binding，`--all` 先查询上游 archived 列表再删除命中的 binding；默认扫描所有运行中实例和已知非运行实例，显式 `--instance` 时只作用于该实例 | 变更 | 无；这是本地 binding 记录修复 / 运维入口 |
| `focusctl [--instance <name>] thread attach (--thread-id <id> \| --thread-name <name>)` | 恢复某个 thread 当前所有 detached bindings 的飞书推送 | 变更 | 飞书 `/attach thread`，以及 reset 结果卡里的“附着当前线程” |
| `focusctl [--instance <name>] thread detach (--thread-id <id> \| --thread-name <name>)` | 暂停某个 thread 的飞书推送，同时保留 thread 与 binding 关系 | 变更 | 飞书 thread-scoped 的 detach 管理动作 |

说明：

- 本地 `thread detach` 走的是正在运行的 FOCUS 服务控制面。
- 底层实现仍可能调用上游 `thread/unsubscribe`，但这属于内部协议，不再作为用户命令名。
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
- `thread archive` 与 `thread delete` 都会拒绝机器级 runtime lease 中仍有 `fcodex` 等非 service holder 的 root；delete 还会拒绝已知为 `active` 或 backend 状态无法可靠读取的 root。只由目标 service 持有且状态为 `idle` 的 root 仍可由目标实例执行 lifecycle mutation。
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

## 5. 与飞书命令面的对应关系

| 本地命令 | 飞书侧最接近入口 | 关键差异 |
| --- | --- | --- |
| `service reset-backend` | `/reset-backend` | 都是实例级 backend 管理；一个是 CLI，一个是飞书卡片流 |
| `service attach` | `/attach service` | 都是实例级恢复动作；飞书主入口通常来自 reset 结果卡 |
| `binding status <binding_id>` | `/status`、`/preflight` | 本地输出更底层，带 binding id、reason code、当前实例 interaction owner |
| `binding attach <binding_id>` | `/attach binding` | 本地可直接按任意 binding id 定位；飞书默认作用于当前 chat |
| `binding detach <binding_id>` | `/detach` | 飞书 `/detach` 只作用于当前 chat；本地可直接按任意 binding id 定位 |
| `prompt send --binding-id <binding_id>` | 无 | 本地可以从 service control plane 合成一条未来或系统触发的 prompt；飞书侧当前没有等价 slash 命令 |
| `thread attach --thread-id/--thread-name` | `/attach thread` | 飞书 thread 级动作只能基于当前 chat 当前 thread；本地可直接按任意目标 thread 定位 |
| `thread detach --thread-id/--thread-name` | 无一条完全等价的飞书命令 | 飞书 `/detach` 是当前 chat binding 级；本地 thread 级动作会批量影响该 thread 当前所有 attached bindings |
| `thread goal --thread-id/--thread-name` | `/goal` | 飞书只作用于当前 chat 当前 thread；本地 CLI 是 thread-scoped 调试 / 运维面，可直接读取任意目标 thread 的 goal |
| `thread goal set/clear` | `/goal set`、`/goal clear` | 飞书命令面只覆盖当前 chat 当前 thread；本地 CLI 可以直接定位任意显式目标 thread。`thread goal set --status active\|paused` 只是 thread-scoped persisted goal 改写，不等价于飞书 `/goal pause` / `/goal resume` |
| `thread list --scope cwd` | `/threads` | 飞书是聊天入口；本地只是线程发现面 |
| `thread status` | `/status`、`/preflight`、`/attach`/`/detach` 的底层诊断 | 本地是 thread-scoped 调试面 |
| `migrate from-feishu-codex` | 无 | 只做一次性本地安装 / 数据转移，不是运行时命令 |

## 6. 边界

下列期待当前不成立：

- 不能把 `focusctl` 理解成飞书 `/threads` 的本地 UI
- 不能期待 `focusctl` 进入 Codex TUI
- 不能把 `focusctl migrate from-feishu-codex` 理解成持续兼容层
- 不能把 `binding clear` 理解成 “停掉当前线程推送”
- 不能把 `thread goal set --status active|paused` 理解成 runtime 恢复 / 暂停命令；它不承诺 load、settings sync 或立即执行

如果新增、删除、改名任何 `focusctl` 子命令，或改变参数约束、实例解析规则、与飞书面的对应关系，必须同步更新本文。
