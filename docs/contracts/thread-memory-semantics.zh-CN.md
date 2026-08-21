# Thread Memory 语义（已退役）

文档角色：中文规范源。英文同步副本：`docs/contracts/thread-memory-semantics.md`。

本文件只作为历史名称下的退役说明保留。

## 当前状态

本项目已经移除了 thread-wise memory 控制面。

下列入口已不再属于正式合同：

- `/memory`
- `focusctl thread memory`
- `new_thread_memory_mode_seed`
- 任何项目自管的 thread-memory persistence / restore 路径

## 现在应使用什么

如果操作者想调整上游 memory/provider 行为，请使用：

- 上游 Codex 配置
- 上游 profile-v2 文件
- 本项目命令面之外的上游启动参数

如果操作者想调整某个 Feishu binding 的后续 turn 行为，请使用：

- `/model`
- `/effort`
- `/approval`
- `/permissions`

## 当前正式文档

- 设置分层：`docs/contracts/runtime-settings-fact-sources.zh-CN.md`
- 飞书控制面：`docs/contracts/runtime-control-surface.zh-CN.md`
- next-load 退役说明：`docs/contracts/thread-next-load-settings-semantics.zh-CN.md`
