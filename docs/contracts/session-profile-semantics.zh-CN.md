# Session、Resume 与 Profile 语义

英文原文：`docs/contracts/session-profile-semantics.md`

本文定义下列三类命令面向用户的目标语义：

- 飞书命令
- `fcodex` shell wrapper 命令
- 运行中 TUI 内部的 upstream Codex 命令

如果当前实现与本文不一致，应将该差异视为 bug、实现限制，或明确记录的后续工作。本文描述的是目标契约，不是所有当前细节行为的罗列。

## 1. 三层语义面

这里有三套彼此独立的命令面：

1. 飞书命令，例如 `/session`、`/resume`、`/profile`
2. `fcodex` shell wrapper 命令，例如 `fcodex /session`
3. 运行中 TUI 内部的 upstream Codex 命令，例如 TUI 内的 `/resume`

它们可以共享后端状态，但并不能彼此等同。
更准确地说：backend 中的 thread / session 数据可以是同一份，但“发现候选线程、如何匹配、是否恢复、恢复后如何绑定当前会话”这些处理环节，分别属于不同的命令面语义。

## 2. 飞书侧语义

### `/session`

- 作用范围：仅当前目录
- 可见性范围：
  - `default` 实例：等同当前 backend 中当前目录下的全局线程列表
  - 命名实例：仅当前实例可见的线程；可见集合由 `admission + 当前实例现有 binding` 组成
- Provider 行为：跨 provider 聚合
- 目的：浏览与当前目录相关的线程
- 排序：按最近更新时间排序

### `/resume <thread_id|thread_name>`

- 作用范围：
  - `default` 实例：整个 backend 全局
  - 命名实例：当前实例可见线程集合；即已导入（admitted）或当前实例已绑定的线程
- Provider 行为：跨 provider
- 匹配方式：
  - 精确线程 id，或
  - 精确线程名
  - 精确名称匹配会复用与 session 发现面相同的分页/精确匹配算法，但仍服从当前实例的可见性范围；它会继续扫描后续分页，直到能证明唯一命中或存在歧义
- 错误行为：
  - 0 个匹配：报错
  - 多个精确同名匹配：报错
- 成功行为：
  - 先立即进入后台恢复流程，给用户一个 pending 提示
  - 恢复目标线程
  - 将当前飞书会话切换到该线程自己的工作目录

### `/profile [name]`

- 读取或修改**当前实例**的本地默认 profile
- 影响范围：
  - 当前实例飞书侧默认 profile
  - 所有没有显式传入 `-p/--profile`，且路由到同一实例的新 `fcodex` 启动
- 不影响：
  - 其他实例的本地默认 profile
  - 原生 `codex` 的全局配置
  - 已经在运行中的 TUI 实例

### `/rm [thread_id|thread_name]`

- 使用 Codex 对外公开的 archive 语义
- 这不是硬删除
- upstream Codex 会把持久化的 rollout JSONL 从 `sessions/` 移到 `archived_sessions/`
- 线程也会在持久化元数据里被标记为 archived
- 归档线程默认不会出现在 `thread/list` 结果里，只有显式请求 archived 过滤时才会列出
- upstream Codex 支持 `thread/unarchive`，但 `feishu-codex` 当前还没有单独暴露 `/unarchive` 命令

### `/release-feishu-runtime`

- 这是飞书侧专有命令，不属于 Feishu 与 `fcodex` wrapper 的 shared surface
- 它面向“当前 chat 绑定的 thread”，但释放的是 Feishu 服务对该 thread 的 runtime residency
- 它不会清空当前 chat 的线程绑定，也不会删除 / archive 线程
- 它与 `feishu-codexctl thread release-feishu-runtime` 共享同一组 runtime 词汇，但不是同一个入口层
- `/release-feishu-runtime` 的精确状态迁移、阻塞条件与 pure reject 规则，统一以 `docs/contracts/runtime-control-surface.zh-CN.md` 为准

## 3. `fcodex` Shell Wrapper 语义

### 实例路由

`fcodex` 在多实例下总是先选定一个目标实例 backend，再进入 wrapper 或 upstream Codex 路径。

默认路由规则是：

- 若显式传了 `--instance <name>`，直接使用该实例
- 否则，若目标 `thread_id` 当前已有全局 live runtime lease，则优先路由到该 owner 实例
- 否则，若当前只有一个运行中的实例，使用它
- 否则，若 `default` 实例正在运行，使用 `default`
- 否则，若存在多个运行中实例且仍无法消歧，直接报错，要求显式 `--instance`
- 若当前没有运行中的实例，则回退到当前 shell / 环境推导出的本地实例目录

这里的“自动路由”只决定 `fcodex` 要连哪个实例 backend。
它不会改变飞书侧 `binding`、`admission` 或 owner 合同。

### 直接运行 `fcodex`

下列入口仍然是 upstream Codex CLI 的入口：

- `fcodex`
- `fcodex <prompt>`
- `fcodex resume <thread_id>`

wrapper 额外增加的行为：

- 默认连接到**所选实例**的 shared backend
- 如果没有传 `-p/--profile`，会继承所选实例的本地默认 profile
- 工作目录取显式的 `--cd` 值；如果没有，则取当前 shell cwd
- 如果显式传了 `--cd` / `-C` 但缺少值，wrapper 应直接报错，而不是静默回退到当前 cwd

## Wrapper 命令

下面这些由 `fcodex` wrapper 自己处理：

- `fcodex /help`
- `fcodex /profile [name]`
- `fcodex /rm <thread_id|thread_name>`
- `fcodex /session [cwd|global]`
- `fcodex /resume <thread_id|thread_name>`

这 5 条命令就是 Feishu 与 `fcodex` wrapper 之间完整的 shared surface；
运行中 TUI 内的 upstream `/help`、`/resume`、`/profile` 等命令不在这个 shared surface 内。

凡是飞书 help 卡片、session 卡片或 wrapper help 文本需要引用这些命令时，
都应复用这份 shared surface 合同，而不是各自再拷一份硬编码用法字符串。

它们必须以独立 wrapper 命令形式使用，不会与裸 `codex` 的 flags 或 subcommands 混用。

需要额外记住一条多实例差异：

- `fcodex` 复用与飞书相同的**分页 / 精确匹配算法**
- 但它默认面向本地操作者视角，不读取命名实例的 `thread admission` 过滤
- 因此，`fcodex /session`、`fcodex /resume <name>` 的可见面可以比飞书命名实例更宽

### `fcodex /session [cwd|global]`

- 作用对象：所选实例 backend
- `fcodex /session`
  - 仅当前目录
  - 跨 provider 聚合
- `fcodex /session global`
  - backend 全局
  - 跨 provider 聚合

这个命令复用的是与飞书相同的 shared discovery 算法，而不是 upstream TUI 自带的 picker。
但它不读取命名实例的 `thread admission` store；它是本地操作者视角的发现面。

### `fcodex /resume <thread_id|thread_name>`

- `thread_id`
  - 在所选实例 backend 上透传给 upstream `codex resume <id>`
- `thread_name`
  - 通过共享的 `feishu-codex` discovery 层解析
  - 精确名称匹配
  - 所选实例 backend 全局
  - 跨 provider
  - 过滤规则与 `fcodex /session global` 一致，但会继续扫描后续分页，直到能证明唯一命中或存在歧义
  - 0 个匹配：报错
  - 多个精确同名匹配：报错

在拿到唯一匹配后，wrapper 会通过所选实例的 shared backend 以 thread id 恢复该线程。
如果 live runtime 当前由另一实例持有，真正的附着仍要服从全局 `thread runtime lease`
的自动转移 / 明确拒绝规则。

### `fcodex --dry-run /session` 与 `fcodex --dry-run /resume`

`--dry-run` 是 `fcodex` wrapper 的本地只读诊断前缀，不属于 TUI 内部命令。

- `fcodex --dry-run /session [cwd|global]`
  - 复用 `fcodex /session [cwd|global]` 的 discovery 规则
  - 显式标注这是只读查询
  - 不启动 TUI
- `fcodex --dry-run /resume <thread_id|thread_name>`
  - 复用 `fcodex /resume <thread_id|thread_name>` 的目标解析规则
  - 输出将路由到的实例、解析出的 thread、默认 profile、thread runtime lease 检查
  - 不启动 TUI，不调用 `thread/resume`
  - 不清理 stale profile，也不写入任何本地状态

它与普通 `fcodex /session` / `fcodex /resume` 一样，不读取命名实例的
`thread admission` 过滤；这是本地操作者视角的预检。

### `fcodex /profile [name]`

- 读取或修改与飞书共用的同一份本地默认 profile 状态
- 不会重写裸 `codex` 的全局配置
- `fcodex -p <profile>` 永远优先于保存下来的本地默认值

### `fcodex /rm <thread_id|thread_name>`

- 使用 Codex archive 语义
- 不是硬删除
- 它与飞书 `/rm` 使用同一套底层 archive 行为
- 持久化的 rollout JSONL 会保留在 `archived_sessions/` 下，而不是被删除

## 4. TUI 内部语义

一旦用户进入运行中的 `fcodex` TUI 会话：

- `/help` 是 upstream Codex 的帮助
- `/resume` 是 upstream Codex 的 resume 行为
- `/profile` 是 upstream Codex 的行为

重要结论：

- TUI 内 `/resume` 不等同于飞书 `/resume`
- TUI 内 `/resume` 不等同于 `fcodex /resume <name>`
- TUI 内 `/resume` 不复用 `feishu-codex` 的跨 provider 名称匹配
- 飞书 `/mode` 只改变未来由飞书发起的 turn 会携带的 mode
- TUI 内协作模式的修改只影响未来由 TUI 发起的 turn
- shared backend 表示共享 live thread 状态，不表示存在一个全局即时同步的协作模式控制面

应把 TUI 视为“运行在 shared backend 上的 upstream 行为”，而不是 wrapper 命令面的延伸。

### 协作模式作用域

- 飞书 `/mode` 不会立刻改写 TUI `/collab` 当前显示的内容
- TUI `/collab` 不是“当前飞书会话待发下一轮 mode”的权威视图
- 真正决定下一轮协作模式的是：哪个客户端实际发起了下一次 `turn/start`
- 一旦某轮 turn 启动，该轮携带的 `collaborationMode` 会更新 backend 线程的后续默认值，直到之后又被其他客户端显式覆盖

## 5. Profile 契约

`feishu-codex` 为**每个实例**维护一份本地默认 profile 状态：

- 该实例上的飞书 `/profile`
- 路由到该实例的 `fcodex /profile`

这份状态与裸 Codex 的全局配置相互独立。

因此：

- 修改某实例的飞书 `/profile` 会改变未来路由到该实例的 wrapper 启动默认 profile
- 修改某实例的 `fcodex /profile` 会改变该实例飞书侧看到的默认 profile
- 一个实例的 `/profile` 不会改写其他实例的本地默认 profile
- `fcodex -p <profile>` 只覆盖当前这次启动
- 裸 `codex -p <profile>` 不在本契约内

### 恢复线程时的 profile 解析

- 当目标线程当前 `not-loaded-in-current-backend` 时：
  - 飞书 `/resume`
  - `fcodex resume <thread_id>`
  - `fcodex /resume <thread_id|thread_name>`
  在没有显式指定 profile 时，最终都以**当前实例 / 所选实例**的本地默认 profile 为准。
- 上一条是行为合同，不要求实现路径相同：
  - 飞书侧会在恢复请求前解析当前实例本地默认 profile，并显式携带解析出的 profile / model / model_provider
  - `fcodex` wrapper 则会先注入所选实例的默认 profile，再进入 upstream `codex resume` 路径
- 因此，对 unloaded 线程，飞书与 `fcodex` 的 resume 行为应视为“行为一致、路径不同”，而不是两套互相矛盾的 profile 规则。
- 当目标线程当前 `loaded-in-current-backend` 时，`resume` 复用的是现有 live runtime。
  - 这一分支上，不能通过 `resume` 改写该 live thread 的 profile 或 provider
  - 显式 `-p/--profile` 或其他 resume 时覆盖项，在这个分支上不构成有效切换
- 如果某个飞书 binding 当前仍指向该 thread，但其 `feishu runtime` 已是 `released`，则后续普通消息会先走 `docs/contracts/runtime-control-surface.zh-CN.md` 定义的 reattach / pure-reject 规则。
  - 被拒绝时必须 pure reject，binding 继续保持 `released`
  - 只有被接受时，Feishu 才会按当前 binding 重新附着 / resume
  - 若 accepted 路径命中了 `notLoaded` thread，则回到本节的 unloaded-thread 规则
  - 若 thread 仍 `loaded`，则只是复用现有 live runtime，不能借此切 provider
- 因此，“线程原始 profile”不是本项目推荐使用的合同术语。
  更准确的说法是：
  - “以当前实例 / 所选实例本地默认 profile 恢复”
  - “以显式 profile 恢复”

## 6. 安全规则

所有地方统一使用一条规则：

- 如果你希望本地 TUI 和飞书继续操作同一个 live thread，请使用 `fcodex`，不要直接用裸 `codex`
- 在多实例场景，本地 TUI 应接到实际持有该 live thread 的同一实例 backend；`fcodex` 会尽量自动路由，歧义时应显式 `--instance`

`fcodex` 是 shared-backend 路径。裸 `codex` 默认刻意不在“共享线程的安全契约”内，除非它被手工指向同一个 remote backend。
