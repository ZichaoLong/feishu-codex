# Session、Resume 与 Profile 语义

英文原文：`docs/contracts/session-profile-semantics.md`

另见：

- `docs/contracts/local-command-and-thread-profile-contract.zh-CN.md`
- `docs/contracts/runtime-control-surface.zh-CN.md`
- `docs/decisions/shared-backend-resume-safety.zh-CN.md`

本文描述当前已收口的三层语义：

1. 飞书命令面
2. 本地 `fcodex` / `feishu-codexctl` 命令面
3. 进入 TUI 后的 upstream Codex 命令面

如果旧文档仍把 `fcodex` shell 层写成一组 slash 自命令，以本文为准。

## 1. 飞书侧语义

### `/session`

- 作用范围：当前目录
- provider：跨 provider 聚合
- `default` 实例：看当前 backend 的当前目录线程
- 命名实例：只看当前实例可见线程；可见集合受 `admission + 当前实例现有 binding` 约束

### `/resume <thread_id|thread_name>`

- 支持精确 `thread_id`
- 也支持精确 `thread_name`
- provider：跨 provider
- `default` 实例：看 backend 全局
- 命名实例：只在当前实例可见集合内匹配
- 0 个匹配报错；多个同名精确匹配也报错

### `/new`

- 立即创建新 thread，并把当前 chat binding 切到这个 thread
- 会把当前实例当前生效的新线程默认 profile 作为这个 thread 的一次性 seed
- thread 真正创建成功后，seed 会按 `thread_id` 持久化为 thread-wise resume profile

### `/profile [name]`

- 作用对象：当前绑定 thread
- 没有绑定 thread 时直接拒绝
- 只有目标 thread verifiably globally unloaded 时才允许修改
- 对 loaded thread，或 loaded / unloaded 事实无法验证的 thread，会直接拒绝；不做热切，也不会偷偷记账等下次生效

### `/unsubscribe`

- 作用对象：当前 chat binding 指向的 thread
- 释放的是 Feishu 对该 thread 的 runtime residency
- 不清 binding，不删 thread，不 archive thread
- 更精确的状态词汇以 `docs/contracts/runtime-control-surface.zh-CN.md` 为准

## 2. 本地命令面

### `fcodex`

`fcodex` 现在是 thin wrapper，不再提供 shell 层 slash 自命令。

它保留的仓库级能力只有两类：

1. `resume` 的增强路由与名字解析
2. `-p/--profile` 的 thread-wise 语义接入

这意味着：

- 不再支持 `fcodex /help`
- 不再支持 `fcodex /session`
- 不再支持 `fcodex /profile`
- 不再支持 `fcodex /rm`
- 不再支持 `fcodex /resume`
- 不再支持 `fcodex --dry-run ...`

### `fcodex resume <thread_id|thread_name>`

- `thread_id`：按目标实例 shared backend 直接恢复
- `thread_name`：先做跨 provider 精确名字匹配，再按 thread id 恢复
- 多实例下，仍服从 runtime lease 与实例路由规则
- 本地恢复目标解析是操作者视角，不读取 Feishu 命名实例的 admission 过滤

### `fcodex -p <profile>`

- 若这次启动不是 resume，而是准备新开会话：
  - `-p` 会透传给 upstream Codex
  - 同时作为**本次启动创建的第一个新 thread**的一次性 seed
- 这个 seed 只在第一次 `thread/start` 成功后写入 thread-wise store
- 如果这次启动根本没创建 thread，就不会落任何 thread-wise 记录

### `fcodex -p <profile> resume <thread>`

- 若目标 thread verifiably globally unloaded：
  - 允许写入该 thread 的 thread-wise resume profile
  - 然后再恢复该 thread
- 否则：
  - 直接拒绝
  - 提示先 `unsubscribe`，并关闭其他打开该 thread 的 `fcodex` TUI

### `fcodex resume <thread>`（未显式 `-p`）

- 如果 thread 已保存 thread-wise profile，则自动注入该 profile
- 如果没有保存记录，则不再回退到“当前实例的新线程默认 profile”
- 当前实例的新线程默认 profile 现在只负责 seed 新 thread，不负责覆盖旧 thread 的 resume

### `feishu-codexctl`

`feishu-codexctl` 是本地查看 / 管理面。

它负责：

- `thread list --scope cwd|global`
- `thread status`
- `thread bindings`
- `thread unsubscribe`
- `binding list/status/clear`
- `thread admissions/import/revoke`

它不是第二个 Codex 前端，也不负责进入 TUI。

## 3. TUI 内语义

一旦进入运行中的 `fcodex` TUI：

- `/help` 是 upstream Codex 的 `/help`
- `/resume` 是 upstream Codex 的 `/resume`
- `/new` 是 upstream Codex 的 `/new`
- 其他命令也都按 upstream 语义解释

因此：

- TUI 内 `/resume` 不等同于飞书 `/resume`
- TUI 内 `/resume` 不等同于 `fcodex resume <thread_name>`
- shared backend 代表共享 live thread 状态，不代表所有前端存在一个即时同步的统一设置面

## 4. Profile 语义总结

当前已经不再把“当前实例的新线程默认 profile”当作主要 resume 模型。

应按下面理解：

- 飞书 `/profile` 改的是当前绑定 thread 的下次 resume 配置
- `fcodex -p <profile>` 新开会话时，只 seed 本次启动创建的第一个新 thread
- `fcodex -p <profile> resume <thread>` 改的是该 thread 的持久化 resume 配置
- 旧 thread 后续 resume 读的是它自己的 thread-wise 配置，而不是实例当前的新线程默认 profile

## 5. 多实例与可见性

- 飞书 `/session`、`/resume` 在命名实例下受 admission 过滤
- `fcodex resume <thread_name>` 与 `feishu-codexctl thread list` 更偏本地操作者视角
- runtime lease、实例选择与转移安全边界，见 `docs/decisions/shared-backend-resume-safety.zh-CN.md`
