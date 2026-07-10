# Runtime 设置事实源与生效边界

英文原文：`docs/contracts/runtime-settings-fact-sources.md`

本文定义统一规则：一个设置写入之后，哪一层才是它的权威事实源？

## 1. 只剩一个可写设置族

### 1.1 binding-wise next-turn settings

当前正式成员：

- model
- effort
- approval
- permissions

它们的属性：

- 作用域是当前 Feishu binding
- 持久化在 binding runtime settings 中
- 主要在 `turn/start` 被消费
- 在恢复未 loaded thread 时，cold `thread/resume` 也可能携带其中一小段
  one-shot override，避免恢复后的第一轮 autonomous turn 回退到旧的
  loaded-thread 默认值

## 2. 本项目不再拥有任何 thread-wise next-load setting

下列表面已被移出本项目合同：

- 历史上的项目自管 profile 命令
- `/memory`
- `focusctl thread memory`
- `new_thread_memory_mode_seed`
- 任何项目自管的 thread-level memory/provider/profile restore state

因此，本项目不再维护“某个 thread 下次 resume 时还会额外注入什么配置”这类
持久化事实源。

## 3. 只读事实族：live runtime / upstream snapshot

有些值仍会被读取，但它们不是项目自管的持久化设置：

- live loaded-backend state
- 上游 thread snapshot
- 上游 `config/read` 返回的 runtime view

这些值可以展示在：

- `/status`
- diagnostics
- admin cards

但不得把它们当成：

- 一个可写的项目设置层
- 某个已移除 legacy profile 命令背后的持久化事实源

## 4. 可写设置表

| 设置族 | 持久化源 | 正式生效边界 | 主要读侧 |
| --- | --- | --- | --- |
| binding-wise next-turn | 当前 binding 的 persisted runtime settings | `turn/start`；恢复未 loaded thread 时，cold `thread/resume` 也可能携带一小段 one-shot override | `/status`、setting cards、preflight |

## 5. binding-wise next-turn 的判定规则

如果问题是：

- “这个 Feishu chat 的下一轮 turn 会使用什么 model / effort / permissions？”

首先看：

- 当前 binding 的 persisted runtime settings

在这个设置族里：

- `auto` 仍表示“不显式 override”
- 它不再映射到任何项目自管 thread-level persisted state
- adapter 不得把 `auto` materialize 成完整的上游 settings 对象并发送旧
  snapshot 值；普通 auto turn 应让上游当前 thread state 自己延续。
- `model` / `reasoning_effort` 与 `approval_policy` / `permissions_profile_id`
  的空值语义不同：
  - `model` / `reasoning_effort` 可以保持空值，表示 `auto`
  - `approval_policy` / `permissions_profile_id` 是 binding-local 安全
    baseline；新 binding 用 `codex.yaml` seed 解析出初始值，一旦 binding
    落盘，就冻结这份 resolved 安全基线，后续不随实例默认漂移
- 两组设置的 turn dispatch 也不同：
  - approval / permissions 每个 Feishu turn 都显式发送，用于重新声明该 binding 的安全基线
  - model / effort 只在非 `auto` 时发送；`auto` 不重新声明，让 upstream thread 当前状态继续生效
- `codex.yaml` 中的 `model` 与 `reasoning_effort` 只 seed 新 binding 的
  初始 runtime state；进入 binding 后，`thread/start` 与普通
  `turn/start` 都只看 binding runtime settings，不再从 adapter config
  fallback。
- `model_provider` 不是 binding runtime setting；它不会在 `/new`、首条
  prompt 创建 thread 或普通 turn 中从 adapter config 自动注入。`codex.yaml`
  不再接受 `model_provider`；provider 应交给上游 Codex 配置，或仅在调用方
  显式传入 provider hint 时发送。
- collaboration mode 不再是 Feishu runtime setting。如需使用，交给上游
  Codex 配置/行为；本项目不再构造或发送上游 `collaborationMode`
  payload。

### 5.1 model / effort 组合校验

Focus 只使用 app-server `model/list` 返回的
`supportedReasoningEfforts` 作为显式 model 的校验 metadata，不尝试从
当前 thread 反推一个“effective model / effort”镜像。

- effort 为 `auto`：`validated`
- model 为 `auto` 且 effort 显式：`deferred`
- 显式 model 没有可用 metadata 且 effort 显式：`deferred`
- 显式 model metadata 声明支持该 effort：`validated`
- 显式 model metadata 未声明支持该 effort：`rejected`

控制面不允许用户新建 `rejected` 组合，但不会迁移或修复已有 binding
数据，也不会在 prompt dispatch 前增加第二套 admission。已有值仍按原字符串传给
app-server，最终执行结果由上游决定。

显式 model / effort 在 `turn/start` 上的上游语义是作用于当前及后续
共享 thread turn。因此，Feishu binding 与本地 TUI 不共享一份项目持久化设置，
但它们仍可能通过同一个 upstream thread 观察或覆盖彼此最近发送的显式值。

## 6. binding store 的空值规则

`chat_bindings.json` 是持久化投影，不是运行语义事实源。runtime-setting
的值、安全基线和显式配置意图是几类不同事实。store 层只负责：

- 保存和读取字符串字段，以及 `configured_settings` 列表
- 校验结构和非空枚举值
- 兼容旧字段名（例如 legacy `sandbox` -> `permissions_profile_id` 字段）

store 层不得引入实例默认 fallback。空字符串必须原样保留，直到
`BindingRuntimeManager` hydrate 时，才按当前实例配置解释：

- `approval_policy` / `permissions_profile_id` 空值只表示旧记录或尚未
  materialize 的 store 形态；hydrate 时解析为当前实例默认，之后一旦
  binding 再次落盘，就写出 resolved 安全基线
- 旧 `collaboration_mode` 字段读取时忽略，新保存不再写出
- `model` / `reasoning_effort` 空值 -> `auto`，不显式 override

`configured_settings` 是 binding-local 的显式用户操作事实源，但不是
`approval_policy` / `permissions_profile_id` 是否存在安全基线的事实源。
它只由 `/model`、`/effort`、`/approval`、`/permissions` 或对应卡片交互写入；
`codex.yaml` seed 不产生 intent。即使某个 value 等于实例默认值，只要对应
setting 名字出现在这个列表里，它仍表示用户显式操作过。

因此：

- 对 `model` / `reasoning_effort`，`configured_settings` 区分“用户显式选择
  auto”与“从未配置”
- 对 `approval_policy` / `permissions_profile_id`，binding 持久化值本身就是
  当前 binding 的安全基线；`configured_settings` 只说明用户是否显式改过它
- 旧记录没有 `configured_settings` 时，store 会按规范化后的非空 setting value
  保守推断 intent；历史上的空值 `auto` intent 无法恢复，这个歧义可以接受

未绑定但已保存过 setting 的 binding 是合法状态：没有 `thread_id`，但承载了
用户的下一轮配置决策或 binding-local 安全基线。具体来说，
`configured/unbound` 表示没有 thread bookmark，但持久化 binding 仍有
`configured_settings`、安全基线或其他必须保留的
binding-local fact。管理面可显示为 `configured/unbound`；它不是 stale thread
binding，不应被 `binding clear-stale` 清理。

## 7. 一条维护规则

如果将来要新增设置，必须先被归类为且只归类为：

1. binding-wise next-turn settings
2. 只读诊断视图

在这个归类存在之前，该设置不得成为新的命令面或新的项目持久化状态层。
