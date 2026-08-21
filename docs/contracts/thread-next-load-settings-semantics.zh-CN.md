# Thread Next-Load 设置语义

文档角色：中文规范源。英文同步副本：`docs/contracts/thread-next-load-settings-semantics.md`。

本文件以历史名称保留，作为退役说明。

## 1. 当前结论

本项目已经不再保留任何项目自管的 thread-wise next-load setting。

这意味着正式合同里已不再包括：

- 任何 thread memory setting
- 任何 thread provider setting
- 任何 thread profile setting
- `new_thread_memory_mode_seed`
- 任何先由本项目持久化、再在 resume 时重新注入的 thread-level setting layer

## 2. 现在还剩什么

### 2.1 binding-wise next-turn settings

通过以下入口管理：

- `/model`
- `/effort`
- `/approval`
- `/permissions`

它们的语义：

- 只作用于当前 Feishu binding 的后续 turn
- 主要在 `turn/start` 被消费
- 在恢复未 loaded thread 时，cold `thread/resume` 也可能为恢复后的第一轮
  autonomous turn 携带其中一小段 one-shot override
- 不是 thread-level persisted restore settings

这条设置时机不授权 observer resume。只有 `root-operation-owner.zh-CN.md` 中封闭、已审阅的安全 goal 预检才允许这条
没有 record 的路径；active、空、无法读取、未来或未识别 status 都让 resume 具有 continuation 能力，必须满足
connected writer/admission 规则。

### 2.2 上游拥有的 process 与 thread 状态

如果操作者想使用上游 profile/provider 或 memory 行为，应直接使用上游
Codex 配置、上游 profile-v2 文件，或上游启动参数。

本项目不会把这些选择镜像成一个项目自管 next-load 层。

## 3. 当前 `resume` 合同

本项目支持的 resume 路径现在只承诺：

- 线程身份解析与安全准入
- 对着正确的实例 backend 做恢复
- 保持 frontend 自己的 runtime 语义

它们不承诺：

- 为某个 thread 恢复额外的项目自管 profile/memory/provider slice

## 4. 为何本文件仍保留

“thread-wise next-load settings” 这个概念仍然有价值，因为它能阻止维护者混淆：

- binding overrides
- live-runtime diagnostics
- 上游拥有的 process state

但在当前版本里，这个类别中的正式成员数量是：

- `0`

## 5. 未来维护规则

如果未来要重新引入 thread-wise next-load setting，项目必须先文档化：

1. 写入后的持久化事实源
2. 正式生效边界
3. 它与 binding overrides 以及上游 process state 的区别

在这些内容存在之前，命令面不得重新引入它。
