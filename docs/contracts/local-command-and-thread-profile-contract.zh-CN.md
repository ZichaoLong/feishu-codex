# 本地命令面与 Thread-Wise Profile 合同

英文原文：`docs/contracts/local-command-and-thread-profile-contract.md`

另见：

- `docs/contracts/session-profile-semantics.zh-CN.md`
- `docs/contracts/runtime-control-surface.zh-CN.md`
- `docs/architecture/fcodex-shared-backend-runtime.zh-CN.md`
- `docs/decisions/shared-backend-resume-safety.zh-CN.md`

本文记录当前已经讨论并接受、且已进入实现的命令面与 profile/provider 正式合同。

它回答 5 件事：

- 飞书侧为何统一使用 `unsubscribe`
- `fcodex` 作为 thin wrapper 的当前正式形状是什么
- `feishu-codexctl` 与 `fcodex` 当前如何分工
- thread-wise `profile/provider` 的正式状态与拒绝规则是什么
- 飞书侧与 `fcodex` 的 `sandbox/approval` 设置边界应如何划分

如果当前实现与本文不一致，应把它视为合同缺口，并按本文收紧实现、文档，或两者一起修正。

## 1. 适用范围与优先级

本文只覆盖以下主题：

- 本地 `fcodex` / `feishu-codexctl` 命令面重划
- 飞书侧 `unsubscribe` 的命名与语义
- thread-wise `profile/provider` 的当前正式合同
- 飞书侧与 `fcodex` 的 `sandbox/approval` 设置边界

对这些主题，若本文与下列文档中的旧表述冲突，以本文为准：

- `docs/contracts/session-profile-semantics.zh-CN.md`
- `docs/contracts/runtime-control-surface.zh-CN.md`

这些旧文档后续应被合并更新，不应长期与本文并存冲突表述。

## 2. 飞书侧 `unsubscribe`

### 2.1 命名

飞书侧统一使用：

- 飞书命令：`/unsubscribe`
- 本地管理 CLI：`feishu-codexctl thread unsubscribe`

此前旧名为 `/release-feishu-runtime`；当前正式合同统一使用 `unsubscribe`。

### 2.2 语义

`unsubscribe` 的语义是：

- 作用对象：当前 chat binding 所指向的 thread
- 实际动作：`feishu-codex` 服务实例对该 thread 释放自己的 Feishu-side runtime residency，并执行 `thread/unsubscribe`
- 保留当前 binding
- 若当前 `interaction owner` 是 Feishu，则同时清理该 owner
- 把所有仍 `attached` 的相关 Feishu binding 统一切到 `released`

### 2.3 它不做什么

`unsubscribe` 不会：

- 删除 thread
- archive thread
- 清空当前 chat binding
- 强制关闭任何 `fcodex` TUI
- 强制让 backend 立刻 unload

因此：

- `unsubscribe` 成功后，thread 仍可能保持 loaded
- 最常见原因是本地 `fcodex` 仍在订阅这个 thread

## 3. `fcodex` 的当前正式形状

### 3.1 总体定位

`fcodex` 当前尽量保持接近裸 `codex`：

- 它本质上是 stock `codex` 前面的一个 thin wrapper
- 它负责 shared-backend 路由、实例选择、cwd 修正代理，以及少量与 thread-wise 设置相关的启动前逻辑
- 它不再承担一个庞大的本地管理命令面

换句话说：

- `fcodex` 应尽量“像 `codex` 一样用”
- 本仓库附加的认知负担，应尽量限制在 wrapper 必须承担的最小集合

### 3.2 命令面

`fcodex` 不再保留 `/help`、`/session`、`/rm`、`/profile` 这类 slash 自命令。

`fcodex` 只保留两类与本仓库直接相关的能力：

1. 对 `resume` 的 wrapper 级增强
2. 对 `-p/--profile` 的 thread-wise 语义接入

其余行为应尽量透传给 upstream `codex`。

### 3.3 `resume`

`fcodex resume` 仍是本仓库应显式接管的一条命令面。

原因是：

- 需要复用 shared backend / 实例路由
- 需要支持跨 provider 的 thread 发现与精确恢复
- 需要接入 thread-wise `profile/provider` 恢复合同

但一旦进入运行中的 TUI：

- TUI 内部的 `/resume`
- TUI 内部的 `/new`
- TUI 内部其他 upstream 命令

都属于 upstream 行为，不再由本仓库额外制造平行命令语义。

## 4. `feishu-codexctl` 的当前正式形状

### 4.1 总体定位

`feishu-codexctl` 是本地发现、查看、管理面。

它不是第二个 Codex 前端，也不承担“进入 TUI、继续 live thread”这一职责。

### 4.2 责任范围

应优先由 `feishu-codexctl` 承担的能力包括：

- thread / binding 查看
- thread 发现与本地诊断
- `unsubscribe`
- admission 管理
- 其他 thread-scoped / binding-scoped 管理动作

这意味着：

- `fcodex` 负责 attach / resume / 进入 Codex
- `feishu-codexctl` 负责查看 / 诊断 / 管理

这两者是不同职责，不应继续混成一个“又能查、又能进 TUI、又能管运行时”的模糊入口。

## 5. Thread-Wise `profile/provider`

## 5.1 目标

`profile/provider` 不再以“实例级默认值”作为主要产品模型。

当前正式模型是：

- 每个 thread 持有自己的 thread-wise 恢复设置
- 这份设置在后续 resume 时继续生效
- 它可被飞书侧与 `fcodex` 共同读取
- 它应跨实例共享，而不是局限于单实例本地状态

## 5.2 设置的真实含义

thread-wise 设置表达的是：

- 该 thread **下次从 unloaded 状态恢复时**，应使用的期望恢复配置

它不是：

- 当前 live runtime 的权威事实
- 当前 loaded thread 一定已经生效的 provider/model

因此，本文使用的术语是：

- desired resume config

而不是：

- current runtime config

## 5.3 存储层级

thread-wise `profile/provider` 应以 `thread_id` 为 key，存放在 machine-global 共享层。

它不应属于：

- 某个实例的本地默认 profile store
- 某个 Feishu binding store
- 某个 `fcodex` 进程私有状态

原因是：

- thread namespace 本来就是跨实例共享的
- 若设置仍是实例级或 binding 级，就会制造“我在这个实例里改了，但另一个实例看不到”的额外认知负担

## 5.4 存储内容

thread-wise store 至少应保存：

- `profile`
- `model`
- `model_provider`
- `updated_at`

其中：

- `profile` 是用户主要操作的概念
- `model` 与 `model_provider` 是从 `profile` 解析出的恢复实参

用户侧主要设置入口应是 `profile`。

`provider` 不应被设计成独立于 `profile` 的平行写入口；
它主要作为解析结果与恢复实参存在，而不是独立产品旋钮。

## 5.5 允许修改的条件

只有当 thread **可验证地处于全局 unloaded** 状态时，才允许修改其 thread-wise `profile/provider`。

更准确地说：

- 不仅要看当前实例 backend 是否 `notLoaded`
- 还要确认 machine-global 上不存在该 thread 的 live runtime owner
- 如果当前 loaded / unloaded 事实无法验证，也必须拒绝写入

因此，允许修改的真实条件是：

- verifiably globally unloaded

而不是仅仅：

- current backend notLoaded

## 5.6 loaded 时的行为

若目标 thread 当前仍 loaded，或 loaded / unloaded 事实无法验证，则修改请求必须被直接拒绝。

不允许：

- 在 loaded thread 上热切 provider
- 先记账、等下一次莫名其妙生效
- 对 live runtime 做 best-effort 改写

拒绝时应返回直接、可操作的错误提示。

推荐提示方向：

- 飞书侧：先执行 `unsubscribe`
- 本地侧：关闭所有打开该 thread 的 `fcodex` TUI

本文刻意选择“明确拒绝 + 直接提示”，而不是在 README 里提前铺陈大量复杂背景。

## 5.7 飞书侧写入口

当飞书 chat 当前已绑定某个 thread 时：

- `/profile <name>` 应作用于该 **当前绑定 thread**
- 若该 thread verifiably globally unloaded，则写入 thread-wise desired resume config
- 否则拒绝并提示

当飞书 chat 当前没有绑定 thread 时：

- `/profile` 应直接拒绝
- 提示用户先执行 `/new`，或先发送第一条普通消息创建 thread
- 它不应再退回“改当前实例的新线程默认 profile”

这条拒绝规则适用于：

- `/profile`
- `/profile <name>`
- profile 卡片按钮动作

## 5.7.1 飞书 `/new` 与首条普通消息的 seed

飞书侧创建新 thread 的两条入口：

- `/new`
- 未绑定 chat 下的首条普通消息

都应遵守同一条 seed 合同：

- 创建 thread 时，仍可带当前实例当前生效的新线程默认 profile 作为创建实参
- 一旦成功拿到真实 `thread_id`，就立刻把这个默认 profile 解析并写入 thread-wise store
- 这份写入只作用于这个新 thread，不回写到 binding 级或实例级临时状态

因此：

- `/new` 与“直接发第一条消息”不应形成两套不同的新 thread profile 语义
- 若后续要切换该 thread 的 profile，仍应走 `unsubscribe` / `/profile <name>` / `resume`

## 5.8 `fcodex resume -p <profile>`

当 `fcodex resume <thread>` 显式带 `-p/--profile` 时：

- 若目标 thread verifiably globally unloaded：
  - 先把该 `profile` 解析成 `model/model_provider`
  - 写入 thread-wise desired resume config
  - 再按该配置恢复 thread
- 否则：
  - 直接拒绝
  - 提示用户先释放 Feishu 订阅，并关闭所有仍打开该 thread 的 `fcodex` TUI

这里不做“loaded 时先偷偷记下来、等下次再生效”的隐式行为。

## 5.9 `fcodex -p <profile>` 新开会话

当用户执行：

- `fcodex -p <profile>`

且此次启动不是在恢复一个已有 thread，而是新开一个 thread 时：

- 该 `-p/--profile` 只作为一次性 seed
- 它只影响**本次启动创建出的第一个新 thread**

一旦第一个新 thread 创建成功并写入 thread-wise store：

- 这次 seed 即视为消费完成
- 不继续影响此 TUI 生命周期内后续可能发生的其他 upstream `/new`

本文刻意不试图把 TUI 内部全部后续行为都重新纳入本仓库合同。

## 5.10 新 thread seed 的写入时机

`fcodex -p <profile>` 的 seed，不应在“还没有 thread_id”时落到 binding 或 instance 级临时状态里。

正确时机是：

- 第一个 `thread/start` 成功
- 已拿到新 thread 的 `thread_id`
- 此时再把 seed 解析并落到 thread-wise store

本文刻意避免“先写一个 pending 占位、等之后再猜它属于哪个 thread”这类模糊设计。

## 5.11 写入失败

若出现下列情况：

- `thread/start` 已成功
- 但 thread-wise store 写入失败

则：

- 新 thread 本身仍然有效
- 但系统必须显式暴露“thread 已创建、但 thread-wise 设置未成功持久化”的错误或警告
- 不得静默假装 seed 已成功落盘

本文不要求在这一层回滚已创建的 thread。

## 6. `sandbox/approval` 的边界

本轮讨论确认：

- 飞书侧各 binding 的 `sandbox/approval` 设置应保持 binding-wise 持久化，并在重启后恢复
- `fcodex` 不需要引入一套 owner-wise 持久化共享设置

`fcodex` 侧行为应保持为：

- 默认读取 `CODEX_HOME/config.toml`
- 显式参数优先于默认值
- 不与飞书侧形成一个跨前端共享的持久化设置面

因此：

- 飞书侧 `sandbox/approval` 与 `fcodex` 的默认设置面是分离的
- 只有在某一轮实际由哪个前端发起 turn 时，那个前端携带的设置才进入该轮执行

## 7. 本文刻意暂缓的问题

本文当前**不**定义以下主题的最终产品语义：

- 是否需要单独暴露 thread-wise profile 的查看命令

这些主题后续应单独收紧，不应在当前阶段靠隐式推断补全。
