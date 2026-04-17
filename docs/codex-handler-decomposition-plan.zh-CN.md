# CodexHandler Ownership 拆分计划

英文原文：`docs/codex-handler-decomposition-plan.md`

本文是实施计划，不是运行时语义合同。

它回答的问题是：

- 为什么下一步不该继续做零散修补
- `CodexHandler` 应按什么 ownership 边界继续拆
- 每个阶段具体改什么、不改什么、什么算完成

如果后续实现顺序调整，应优先更新本文，而不是把计划性内容混入正式合同文档。

## 1. 背景

当前仓库已经完成了几轮重要收口：

- binding 持久化 schema 已 fail-closed
- binding clear / clear-all 已进入正式 control-plane / admin CLI
- shared command surface 已有一致性测试
- help/card action payload 已移除未使用的 `plugin`
- binding 解析与 runtime state hydrate/create 已收口到单一 resolver 路径

这些修改降低了局部歧义，但还没有解决最核心的结构问题：

- `CodexHandler` 仍然同时持有多个状态机
- 很多约束仍需要靠“调用顺序记忆”来理解
- `RuntimeLoop` 与 `_lock` 仍在兜底一个过大的共享状态面

因此，下一步不应继续优先做散点修补，而应先做 ownership decomposition。

## 2. 目标

本轮计划的目标是：

- 让 `CodexHandler` 从“持有所有状态机的大对象”收成“编排器”
- 让不同状态机的 ownership 更明确
- 降低对粗粒度共享锁和跨方法隐式顺序的依赖
- 在不改变用户侧行为的前提下，为后续合同收紧和回归测试扩展建立更清晰边界

## 3. 非目标

本计划不追求：

- 仅通过“把一个大文件拆成多个文件”来制造轻解耦
- 先做锁粒度微调
- 为了重构而同时改变用户可见行为
- 在边界未清楚前继续追加 helper 把复杂度堆回 `CodexHandler`

## 4. 设计原则

- 先拆 state ownership，再谈锁优化
- 先抽显式接口，再考虑内部实现替换
- 先保持用户行为不变，再讨论能力扩展
- 组件边界应按“谁拥有哪类状态迁移”来划分，而不是按代码行数或文件大小

## 5. 当前核心问题

`CodexHandler` 目前仍集中持有至少四组相互独立但彼此耦合的状态职责：

1. binding / subscribe / attach / released 运行时
2. Feishu write owner / interaction owner / thread lease
3. turn / execution 生命周期
4. control-plane / adapter event bridge 编排

这些职责共居一个对象带来的问题是：

- 同一个变更可能同时触碰 binding、owner、execution、UI anchor
- 很多正确性需要手工证明“调用顺序刚好成立”
- 测试虽能覆盖行为，但难以锁住 ownership 边界
- 继续在 handler 内部修补，只会提高局部正确性，无法降低整体理解成本

## 6. 总体方案

建议按三个阶段推进：

1. `BindingRuntimeManager` 拆分
2. `TurnExecutionCoordinator` 拆分
3. 剩余合同与命名收尾

这三个阶段之间是前后依赖关系，不建议调换顺序。

当前进度：

- 第一阶段已完成：`BindingRuntimeManager`
- 第二阶段已进入细化拆分：
  - `TurnExecutionCoordinator` 负责执行状态迁移
  - `ExecutionOutputController` 负责执行卡片与 follow-up 发布
  - `ExecutionRecoveryController` 负责 watchdog、快照对账、终态补账、降级判定
- `CodexHandler` 仍未完全收成编排器，但已不再直接拥有上述三类实现细节

## 7. 第一阶段：BindingRuntimeManager

### 7.1 目标

把 “binding/runtime ownership” 从 `CodexHandler` 中抽出，先建立一个明确的内部边界。

### 7.2 负责的状态与职责

第一阶段新组件应负责：

- binding 解析
- runtime state hydrate / create
- bound / attached / released / unbound 状态迁移
- subscribe / unsubscribe
- binding 持久化同步
- Feishu write owner
- interaction owner / interaction lease
- thread write lease
- binding status snapshot
- binding clear / clear-all 的底层执行
- `/release-feishu-runtime` 的底层执行

### 7.3 不负责的内容

第一阶段不应负责：

- turn/start / cancel / finalize
- execution transcript
- approval / ask-user pending request
- patch timer / watchdog / follow-up
- adapter notification 解释

这些仍先留给后续执行生命周期组件。

### 7.4 建议接口

组件不应暴露内部 dict 和 store，而应暴露显式操作接口，例如：

- `resolve_binding(...)`
- `get_runtime_view(...)`
- `bind_thread(...)`
- `clear_thread_binding(...)`
- `release_feishu_runtime(...)`
- `clear_binding(...)`
- `clear_all_bindings(...)`
- `snapshot(...)`
- `acquire_write_lease(...)`
- `release_write_lease(...)`
- `acquire_interaction_lease(...)`
- `release_interaction_lease(...)`

调用方应依赖这些接口表达意图，而不是直接访问 `_runtime_state_by_binding`、`_chat_binding_store`、`_thread_lease_registry`、`_interaction_lease_store`。

### 7.5 迁移策略

建议按以下顺序迁移：

1. 把当前 resolver / hydrate / snapshot 相关逻辑先搬入 manager
2. 让 `CodexHandler` 只通过 manager 获取 binding 与 runtime view
3. 再把 attach / release / clear / owner lease 操作逐步挪入 manager
4. 最后收掉 handler 内对 `_runtime_state_by_binding` 及相关 store 的直接访问

### 7.6 验收标准

- 用户侧行为不变
- 现有 binding / attach / release / clear / owner 相关回归测试继续通过
- 新增 manager 级测试，覆盖：
  - binding 解析
  - hydrate / create
  - attach / release
  - write owner / interaction owner
  - clear / clear-all 的拒绝条件

## 8. 第二阶段：TurnExecutionCoordinator

### 8.1 目标

把 “turn / execution lifecycle ownership” 从 `CodexHandler` 中抽出，并与 binding/runtime 管理彻底分开。

### 8.2 负责的状态与职责

第二阶段执行生命周期边界现已细化为三个协作组件，共同负责：

- `TurnExecutionCoordinator`
  - prompt turn start
  - cancel turn
  - execution anchor
  - execution transcript
  - plan state
  - terminal finalize 前的显式状态迁移
- `ExecutionOutputController`
  - patch timer
  - 执行卡片 send / patch
  - follow-up 发送决策
  - plan card publish / patch
- `ExecutionRecoveryController`
  - mirror watchdog
  - snapshot reconcile
  - terminal reconcile 补账
  - runtime degraded 标记

### 8.3 与 BindingRuntimeManager 的边界

执行生命周期组件不负责决定 binding 是什么，也不直接管理 attach/release。

它应通过 `BindingRuntimeManager` 获取：

- 当前 binding
- 当前 thread
- 当前 runtime view
- owner / lease 能否写入

也就是说：

- `BindingRuntimeManager` 决定“这是谁的线程状态”
- `TurnExecutionCoordinator` 决定“这个 turn 如何开始、运行、结束”

### 8.4 迁移策略

建议按以下顺序迁移：

1. 先搬 start / cancel / retire 这条主路径
2. 再搬 pending request 与 execution anchor
3. 再搬 transcript / plan / patch / watchdog / follow-up
4. 最后把 snapshot reconcile / finalize 收进去

当前已完成到第 3 步和第 4 步中的大部分执行状态路径，但 `CodexHandler` 仍持有：

- pending approval / ask-user request 生命周期
- adapter notification 到具体 domain / controller 的分发编排
- 非执行类 UI 与命令面 glue code

### 8.5 验收标准

- turn start / cancel / pending request / finalize / reconcile 的现有测试继续通过
- 新增 coordinator 级测试，覆盖：
  - 终态通知
  - follow-up 不重复
  - approval / ask-user 状态迁移
  - watchdog 兜底
  - snapshot reconcile 对 anchor/transcript 的影响

## 9. 第三阶段：剩余合同与命名收尾

前两阶段完成后，再处理剩余更适合落在清晰边界内的条目：

- `#2` `admin_open_ids` 单一事实源
- `#9` authoritative read 与 bounded-list best-effort lookup 的命名与文档
- `#15` `ThreadLeaseRegistry` 的并发合同

这一步之所以后置，是因为它们在当前结构下继续修，只会继续把 helper 堆回 `CodexHandler`。

## 10. 为什么不建议别的顺序

### 10.1 不建议先拆锁

先拆锁很容易得到：

- 锁更多了
- 状态边界却更糊了

这不是我们要的长期架构。

### 10.2 不建议先做更多散点 review 修补

当前局部 bug 与局部合同已收口不少，继续做散点修补的边际收益会下降。

更高价值的是先降低整体推理成本。

### 10.3 不建议先做文件级切分

如果只是把 handler 拆成更多文件，但状态 ownership 仍不清楚，那只是“把大文件导航变成多文件导航”，不是真正解耦。

## 11. 执行约束

前两阶段建议遵守以下约束：

- 默认不改变用户可见行为
- 每阶段都先抽边界，再迁移调用点
- 每阶段都补对应组件级测试
- 不在同一批改动里同时处理无关合同条目
- 允许重命名内部 API，但不保留为了兼容而存在的中间层

## 12. 建议提交节奏

建议每个阶段按类似节奏拆成多次提交：

1. 文档与边界说明
2. 组件骨架与最小接口
3. handler 切换到新接口
4. 补组件级回归测试
5. 删除旧直连路径与遗留 helper

这样做的好处是：

- review 更容易看清 ownership 是否真的转移
- 回滚粒度更小
- 不会把“边界定义”和“行为改动”混成一个超大提交

## 13. 当前推荐的下一步

下一步应直接开始第一阶段：`BindingRuntimeManager` 拆分。

建议第一批只做：

- manager 文档落点
- manager 最小骨架
- binding resolver / hydrate / runtime view / snapshot 迁移

先把“binding/runtime ownership”这条线抽出来，再进入 attach/release/lease 操作的进一步迁移。
