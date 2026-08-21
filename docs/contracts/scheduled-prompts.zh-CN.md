# 定时续跑与 Synthetic Prompt 合同

文档角色：中文规范源。英文同步副本：`docs/contracts/scheduled-prompts.md`。

本文定义当前仓库针对“未来某个时间点继续同一 Feishu 绑定 thread”的正式最小合同。

它覆盖三层：

- service control plane：`binding/submit-prompt`
- 本地 CLI：`focusctl prompt send`
- Linux `systemd --user` managed skill：`feishu-scheduled-prompts`

## 1. 目标

当前正式支持的不是“内建 scheduler 子系统”，而是：

- 在未来时点，安全地向某个既有 Feishu binding 合成发起一轮新的 prompt
- 继续复用同一个 FOCUS 实例 backend
- 保持现有 running-turn / attach / interaction / live-runtime 安全边界

当前明确不支持：

- 持久化 scheduler / job queue
- 跨 binding fan-out prompt
- 另起一个裸 Codex backend 去恢复同一 thread

## 2. `binding/submit-prompt`

control plane 新增：

- `binding/submit-prompt`

它的合同是：

- 作用域是 **binding**，不是 thread
- 入参至少要有：
  - `binding_id`
  - `text` 或 `input_items`
- 可选：
  - `actor_open_id`
  - `synthetic_source`
  - `display_mode`
- 目标 binding 必须已经存在；缺失 binding 时必须 fail-close，不能隐式创建新 binding
- 允许目标 binding 当前尚未绑定 thread；这里指的是“已有 binding，但当前无 thread”，此时沿用普通 prompt 入口的“先建 thread 再启动 turn”语义
- 允许目标 binding 当前是 `detached`；若 attach / resume 预检可通过，则按现有绑定恢复路径执行
- 所有真正写入前的检查都必须复用现有安全边界，而不是旁路

返回值合同：

- `started=true`
  - 表示上游已经接受该 prompt 的 `turn/start` submission；上游 idle 时通常开始新 turn，极窄竞态中也可能加入已经 active 的 regular turn
  - response 保留 authoritative upstream `turn.id`；Focus lease/execution lifecycle 仍必须等待 matching `turn/started` 激活
- `queued=true`
  - 表示目标 binding 正在执行，且该 synthetic prompt 已进入同一 binding 的本地 FIFO
  - 返回值应包含 `queue_position`
  - 出队时必须重新读取该 binding 的最新 next-turn 设置，如 `/model`、`/effort`、`/approval`、`/permissions`
- `started=false, queued=false`
  - 表示 fail-closed 拒绝或启动失败；已接受的延后 prompt 则为 `started=false, queued=true`
  - 必须返回 `reason`；若有明确 reason code，也应返回 `reason_code`

## 3. `focusctl prompt send`

本地 CLI 新增：

- `focusctl [--instance <name>] prompt send --binding-id <binding_id> (--text <text> | --text-file <file>)`

它的合同是：

- 这是 `binding/submit-prompt` 的正式本地入口
- 默认是 `display_mode=silent`
- 可额外传：
  - `--synthetic-source`
  - `--display-mode silent|announce`
  - `--actor-open-id`
- 目标 binding 当前不可写时：
  - 退出码必须非零
  - 输出必须带拒绝原因
- 若目标 binding 获得下文定义的 exact FIFO admission，则不视为失败；prompt 进入该 binding 的本地 FIFO，并在
  matching active execution 结束后继续执行。证明只来自本 binding 的 exact execution anchor、既有
  same-binding/root/epoch continuity，或在 shared binding lock 内再次核对的 preprojection exact local turn；
  writer denial、start response 或其中返回的 `turn.id` 本身都不够

## 4. `display_mode`

当前只支持两个模式：

- `silent`
  - 不额外发“这是系统触发”的说明消息
  - 若成功，正常执行卡 / 终态卡仍按现有运行时逻辑产生
- `announce`
  - 只有 synthetic prompt 的 `turn/start` submission 已被上游接受（`started=true`）后，才向目标 chat 发送一条简短触发说明
  - 被拒绝、启动失败、结果 unknown 或只是已入队的提交都不得公告；已入队项目只能在自己真正出队且 submission 被接受后才可公告

当前没有更复杂的消息编排合同。

## 5. `feishu-scheduled-prompts` skill

当前正式提供一个 Linux-only managed skill：

- `feishu-scheduled-prompts`

它的合同是：

- 目标是管理 `systemd --user` timer/service
- 到点后仍然通过 `focusctl prompt send` 回到当前实例 control plane
- 不直接调用独立 Codex SDK helper
- 不直接依赖飞书消息回环

skill helper 当前提供：

- `create`
- `list`
- `show`
- `remove`
- `run-now`

这些 helper 不是飞书 slash 命令，也不是正式的跨平台公共产品面；它们只是 Linux 本机短期方案。

本地工具解析也是 helper 合同的一部分：

- 普通用户在登录 shell 中使用时，如果 `PATH` 里有 `focusctl`，可以继续依赖 `PATH`
- `create --ctl-path <path>` 是显式 override，并会写入生成的 service unit
- 省略 `--ctl-path` 时，helper 按以下顺序发现 `focusctl`：
  1. `PATH`
  2. `FOCUS_BIN_DIR/focusctl`，或 `~/.local/bin/focusctl`
  3. `FOCUS_DATA_ROOT/.venv/bin/focusctl`，或
     `~/.local/share/focus/.venv/bin/focusctl`
- managed skill 指南应使用受管 venv 的 Python 运行 helper，通常是
  `FOCUS_DATA_ROOT/.venv/bin/python`，而不是假设系统 `python3` 满足本项目运行时

Recurring timer 必须有明确终止策略。`systemd --user` 只理解
`OnCalendar`，不理解业务任务是否完成。可接受形态是：

- 使用具体未来时间的一次性任务
- prompt 中包含明确 self-removal 条件和删除命令的 recurring 任务
- 带已知截止时间、并额外创建 one-shot cleanup prompt 的 recurring 任务

## 6. 安全边界

以下约束是正式合同：

1. 定时任务只是“未来时点发起一次新的 prompt”。
2. 定时任务不能绕过当前实例的 interaction / attach / running-turn 保护。
3. 只有目标 binding 的 exact 进程内 execution anchor、既有 same-binding/root/epoch continuity，或在 shared
   binding lock 内再次核对的 preprojection exact local turn 可以进入本地内存 FIFO。preprojection 只接受
   `InteractionLeaseStore` 中本进程、非飞书、非空 exact active lease；对 Web/`fcodex` 而言，这只可能来自仍
   lease-bearing 的 exclusive/autonomous path，普通 prompt/start 不创建该证明。跨 binding、foreign/stale lease、
   turn/root mismatch 与 attach/preflight 失败仍必须 fail-closed；writer denial、start response 或其中返回的 `turn.id`
   不建立 continuity。
4. 当前不做跨实例自动抢占 live runtime owner。
5. Linux skill 只是调度壳；真正执行面仍是 `binding/submit-prompt`。
6. `display_mode=announce` 是 `turn/start` submission 被上游接受后的通知，绝不是预先准入的证据，也不得在 submission 被接受前公告。
7. helper 持久化的 prompt、task metadata 与生成的 user unit 都可能暴露输入内容或 binding 信息，必须以当前用户私有权限写入；task 目录为 `0700`，文件为 `0600`。

## 6.1 Binding FIFO

Feishu 普通 prompt、`focusctl prompt send` / `binding/submit-prompt`、以及 `/compact` 共享同一个 binding admission 语义：

- 当前 binding idle 时，立即执行
- 当前 binding 有 active execution 时，只有同一个 binding 可入队；队列准入不再额外要求 `actor_open_id` 与当前 running turn 的 actor 相同
- 若该 execution 是 Web/`fcodex` 发起 turn 的飞书镜像，只有普通 prompt 可以在同一锁内复核 exact
  active/attached/inflight binding/root/turn 镜像后入队；镜像已有非空 exact turn 时不要求 origin lease，先发生
  writer denial 既不是证明也不是前提。`/compact`、另一飞书 binding 与没有 exact turn 的 submission 不使用该例外
- active/attached binding 已镜像非空 exact autonomous turn 时同样不需要伪造 lease；mirror 尚未到达时，只允许
  `InteractionLeaseStore` 中本进程、非飞书、非空 exact active lease 代替，并在 append 前于同一锁内再次读取仍
  完全一致。普通 Web/`fcodex` prompt/start 没有 lease，不能提供这份 preprojection 证明
- 没有 execution anchor、既有 FIFO continuity 或 preprojection exact local turn 时，普通 prompt 不建立队列；
  它取得 blank submission lease 后直接调用官方 `turn/start`
- `actor_open_id` 仍是身份、审计、运行时交互归属与回复上下文的一部分，但不是同 binding 排队的额外分区键
- 队列只保存在当前进程内存中，不承诺服务重启恢复、列表、取消或跨 binding 排队
- 每一项同时绑定入队时的 exact binding、target thread 与 binding epoch。它不是可持久化的
  continuation token、writer credential，也不能授权它跟随之后的 binding/target 变更
- `FeishuExecutionQueueController` 是 item 顺序与单调 binding epoch 的唯一进程内 owner。每次成功移除旧
  authority 都会 invalidate epoch，所以即使 binding key 或 root id 被复用，A -> B -> A 也不能复活旧 A item
- drain 只用同时校验 issuer 与对象 identity 的 receipt claim exact head；execution 未结算前不会提前出队。
  recall 或 binding invalidation 会取消该 receipt；连续 preparation-drop/known-failure head 以循环结算，不用
  callback 递归
- `/compact` 入队后出队时会先建立本地 execution anchor，再调用上游 `thread/compact/start`；在这张 anchor 收到自己的 `turn/started`、`contextCompaction item/started` 补绑定、或明确启动失败前，后续 prompt 不能穿透
- `/model`、`/effort`、`/approval`、`/permissions` 等设置命令不入队，立即修改 binding-wise next-turn 设置；后续出队 prompt 读取最新设置

所有真正跨过 app-server 边界的飞书普通 prompt，包括即时、出队与 synthetic scheduled input，都使用完整
input/settings payload 调用官方 `turn/start`。上游 idle 时通常开始新 turn；在极窄竞态中，若 Web、`fcodex`
或 resume 后的 autonomous goal continuation 先变成 active，上游 start-or-steer 会把这笔飞书 input 与本轮
settings 加入该 active regular turn。这是已接受并产生 effect 的 submission，不是 FIFO 拒绝，不能自动重发。
response 保留 authoritative upstream `turn.id`；Focus 等待 matching `turn/started` 激活本地 lease/execution lifecycle。`/compact` 保持独立
start 合同。

同一 root 任一时刻最多只有一个飞书 binding 拥有 pending/draining FIFO continuity。continuity 建立后，只保证同一
binding/root/epoch 后续输入的 FIFO 顺序；另一 binding 必须 fail closed，直到旧 continuity 排空或失效。真正出队
仍是一笔新的 `turn/start` submission；若竞态使它加入 active regular turn，该 head 已被消费并等待该 actual turn 的
matching lifecycle terminal，而不是重新排队或重发。不轮询、不设 timer/scheduler、不自旋、不持久化 wake-up，也不
自动重发。unknown/malformed start、root-owner settlement failure 与 execution-anchor retirement failure 继续 blocked，
绝不创建或推进新的 FIFO continuity。

### 6.2 FIFO 失效与真正出队

一次成功移除 binding 旧 authority 的生命周期变更，必须丢弃该 binding/target 的 FIFO。包括成功的
binding deactivation / clear、runtime detach、把一个已有 binding 移到另一个 root（包括 `/cd` 的
clear-and-rebuild 路径）、archive/delete 清理，以及 known-gone-thread recovery。群停用也必须丢弃该群
binding 的旧 FIFO，即使管理员以后可以重新激活该群。

正在 drain 的 head 自己第一次创建一个初始 binding，不属于这里的 target rebind。这里约束的是一个已经
绑定的旧 thread：为它提交的项绝不能仅因 binding 名称被复用，就变成另一个 thread 的输入。

丢弃 FIFO 时，还必须取消已经被 re-entrant drainer 取出的 head。队列会记录 cancellation marker，drainer
在真正启动旧项前必须检查它。因此旧项不能先逃出 deque、再穿过一次成功的失效变更，最后在重建的 binding
或新 root 下启动。

真正出队是一项新的 writer action，而不是沿用过去的准入结果。它必须在 start/compact execution 前取得
exact blank submission lease。若这次准入失败，该项按
正常失败路径拒绝或丢弃；绝不能隐藏等待成一次 takeover，也不能交给另一个 frontend。见
[`root-operation-owner.zh-CN.md`](root-operation-owner.zh-CN.md)。

若已准入的上游 start 结果不可确认，exact queue head 不得 replay。此后在当前进程内阻塞后继 head 的是
PID-bound blank lease 与本地 execution anchor，而不是 queue receipt；matching lifecycle 或 epoch invalidation
负责收敛。known no-send 或 preparation drop 可以消费 exact head，并继续同 epoch 的下一项。

## 7. 平台边界

当前仓库只把 `systemd --user` 方案作为正式短期实现。

因此：

- `feishu-scheduled-prompts` helper 当前只承诺 Linux
- macOS / Windows 当前没有对应的受管定时 helper 合同

如果后续新增跨平台 scheduler 产品面，本文必须同步更新。
