# Session、Resume 与 Profile 语义

英文原文：`docs/session-profile-semantics.md`

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
- Provider 行为：跨 provider 聚合
- 目的：浏览与当前目录相关的线程
- 排序：按最近更新时间排序

### `/resume <thread_id|thread_name>`

- 作用范围：整个 backend 全局
- Provider 行为：跨 provider
- 匹配方式：
  - 精确线程 id，或
  - 精确线程名
  - 精确名称匹配会复用与 session 发现面相同的跨 provider 全局过滤规则，但会继续扫描后续分页，直到能证明唯一命中或存在歧义
- 错误行为：
  - 0 个匹配：报错
  - 多个精确同名匹配：报错
- 成功行为：
  - 先立即进入后台恢复流程，给用户一个 pending 提示
  - 恢复目标线程
  - 将当前飞书会话切换到该线程自己的工作目录

### `/profile [name]`

- 读取或修改 `feishu-codex` 的本地默认 profile
- 影响范围：
  - 飞书侧默认 profile
  - 所有没有显式传入 `-p/--profile` 的新 `fcodex` 启动
- 不影响：
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
- 它只释放 Feishu 服务自己对当前绑定 thread 的 runtime 持有
- 它不会清空当前 chat 的线程绑定，也不会删除 / archive 线程
- 如果命令后 thread 仍处于 `loaded`，说明还有外部订阅者仍在附着，最常见的是本地 `fcodex`
- 如果命令后 thread 已 `notLoaded`，后续再恢复时是否能切 profile / provider，遵守本文第 5 节的 profile 恢复合同

## 3. `fcodex` Shell Wrapper 语义

## 直接运行 `fcodex`

下列入口仍然是 upstream Codex CLI 的入口：

- `fcodex`
- `fcodex <prompt>`
- `fcodex resume <thread_id>`

wrapper 额外增加的行为：

- 默认连接到 `feishu-codex` 的 shared backend
- 如果没有传 `-p/--profile`，会继承本地默认 profile
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

它们必须以独立 wrapper 命令形式使用，不会与裸 `codex` 的 flags 或 subcommands 混用。

### `fcodex /session [cwd|global]`

- `fcodex /session`
  - 仅当前目录
  - 跨 provider 聚合
- `fcodex /session global`
  - backend 全局
  - 跨 provider 聚合

这个命令使用的是与飞书相同的 shared discovery 逻辑，而不是 upstream TUI 自带的 picker。

### `fcodex /resume <thread_id|thread_name>`

- `thread_id`
  - 直接透传给 upstream `codex resume <id>`
- `thread_name`
  - 通过共享的 `feishu-codex` discovery 层解析
  - 精确名称匹配
  - backend 全局
  - 跨 provider
  - 过滤规则与 `fcodex /session global` 一致，但会继续扫描后续分页，直到能证明唯一命中或存在歧义
  - 0 个匹配：报错
  - 多个精确同名匹配：报错

在拿到唯一匹配后，wrapper 会通过 shared backend 以 thread id 恢复该线程。

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

`feishu-codex` 维护一份本地默认 profile 状态：

- 飞书 `/profile`
- `fcodex /profile`

这份状态与裸 Codex 的全局配置相互独立。

因此：

- 修改飞书 `/profile` 会改变未来 wrapper 启动时使用的默认 profile
- 修改 `fcodex /profile` 会改变飞书侧看到的默认 profile
- `fcodex -p <profile>` 只覆盖当前这次启动
- 裸 `codex -p <profile>` 不在本契约内

### 恢复线程时的 profile 解析

- 当目标线程当前 `not-loaded-in-current-backend` 时：
  - 飞书 `/resume`
  - `fcodex resume <thread_id>`
  - `fcodex /resume <thread_id|thread_name>`
  在没有显式指定 profile 时，最终都以 `feishu-codex` 当前本地默认 profile 为准。
- 上一条是行为合同，不要求实现路径相同：
  - 飞书侧会在恢复请求前解析本地默认 profile，并显式携带解析出的 profile / model / model_provider
  - `fcodex` wrapper 则会先注入默认 profile，再进入 upstream `codex resume` 路径
- 因此，对 unloaded 线程，飞书与 `fcodex` 的 resume 行为应视为“行为一致、路径不同”，而不是两套互相矛盾的 profile 规则。
- 当目标线程当前 `loaded-in-current-backend` 时，`resume` 复用的是现有 live runtime。
  - 这一分支上，不能通过 `resume` 改写该 live thread 的 profile 或 provider
  - 显式 `-p/--profile` 或其他 resume 时覆盖项，在这个分支上不构成有效切换
- 如果某个飞书 binding 当前仍指向该 thread，但其 `feishu runtime` 已是 `released`，则下一条普通消息会先走正常的 prompt preflight。
  - 如果这条 prompt 被拒绝，则这次拒绝必须是 pure reject，binding 继续保持 `released`
  - 只有在 prompt 被接受时，Feishu 才会按当前绑定重新附着 / resume，再启动 turn
  - 如果当时 thread 已 `notLoaded`，就回到本节的 unloaded-thread 规则
  - 如果当时 thread 仍 `loaded`，则只是复用现有 live runtime，不能借此切 provider
- 因此，“线程原始 profile”不是本项目推荐使用的合同术语。
  更准确的说法是：
  - “以当前本地默认 profile 恢复”
  - “以显式 profile 恢复”

## 6. 安全规则

所有地方统一使用一条规则：

- 如果你希望本地 TUI 和飞书继续操作同一个 live thread，请使用 `fcodex`，不要直接用裸 `codex`

`fcodex` 是 shared-backend 路径。裸 `codex` 默认刻意不在“共享线程的安全契约”内，除非它被手工指向同一个 remote backend。
