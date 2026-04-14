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

## 6. 安全规则

所有地方统一使用一条规则：

- 如果你希望本地 TUI 和飞书继续操作同一个 live thread，请使用 `fcodex`，不要直接用裸 `codex`

`fcodex` 是 shared-backend 路径。裸 `codex` 默认刻意不在“共享线程的安全契约”内，除非它被手工指向同一个 remote backend。
