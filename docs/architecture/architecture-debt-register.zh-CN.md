# FOCUS 活跃架构债务台帐

文档角色：中文规范源。英文同步副本：`docs/architecture/architecture-debt-register.md`。

本文只记录当前未结架构债务、外部能力缺口和验收条件。功能语义以 `docs/contracts/` 为准；
执行中的切片、提交预算和临时证据可放在 `docs/_work/`，但关闭本台帐条目必须回到代码、正式合同与测试。

## 1. 维护规则

状态只有四种：

- `open`：问题已证实，尚未开始完整实现；
- `in_progress`：同一收口链已有实现，但仍有明确退出条件；
- `upstream_blocked`：本仓库只能缩小产品承诺，不能可靠补出上游原语；
- `closed`：当前代码、合同与回归已经满足退出条件。

维护约束：

- 活跃项只写根因、当前 owner、目标边界和可验证退出条件，不保存逐提交流水账；
- closed 项只留简短索引；详细历史由 Git 与冻结 work ledger 保存；
- 文件体量是 ownership/context review 信号，不是完成标准；阈值与执行纪律由三份 `AGENTS` 文档统一规定；
- 不为通过门禁机械拆文件，也不在行为修改中混入 package move；
- 已删除的 durable root writer、descendant gate、operator tree-stop、create/resume quarantine 与 durable
  server-request ledger 不是兼容目标，不得借重构恢复。

## 2. 当前执行顺序

1. 保持 `GAP-003` 为明确的 macOS 产品边界；本地 diagnostics 或人工 recovery 不得冒充缺失的平台原语。
2. 超限文件、package 与目录整理不作为常驻清理队列。只有具体任务证明存在 owner 混装、依赖方向或上下文
   阻塞时才进入同一有界 campaign，文件体量本身不能触发连续切片。

## 3. 活跃债务

本 campaign 当前没有仓库自身仍可实现的活跃项；外部能力缺口保留如下。

## 4. 外部能力缺口

### GAP-001 Codex app-server 缺失的可靠性原语

- **状态：** `upstream_blocked`
- **已确认缺口：** 通用 mutation idempotency/exactly-once、durable event cursor、可认证 backend process
  incarnation，以及原子完整 descendant-tree freeze/interrupt 并不存在于当前上游合同。
- **本地边界：** unknown exact effect 不自动 retry；snapshot/history 重建 UI；普通 stop 只 interrupt exact active
  turn；create/resume/server request 不建立 durable/global quarantine。
- **退出条件：** 只有上游提供可验证原语，或 Focus 为一个具体场景设计并验证完整受限事务后，才能扩大对应
  产品承诺。缓存、PID、lineage guess 或底层 bytes RPC 都不能充当证明。

### GAP-003 macOS escaped-descendant containment

- **状态：** `upstream_blocked`
- **缺口：** macOS guardian 可证明 app-server 与其 process group，不能 containment 主动建立新 session/group
  的任意 tool/MCP descendant。
- **本地边界：** stop、崩溃清理、diagnostics、cleanup receipt 与人工操作都只覆盖该 process group；不能声称
  与 Linux subreaper 或 Windows Job Object 等价，也不能证明主动逃逸的 descendant 已不存在。
- **退出条件：** 获得可靠 containment primitive 并通过真实平台验证；在此之前，较窄支持边界保持为产品限制。

## 5. Closed 索引

| ID | 当前关闭边界 |
| --- | --- |
| AD-001 | `ServiceRuntimeLifecycle` 持有启动、公开 ingress、shutdown barrier 与 authority release 顺序。 |
| AD-002 | `AdapterIngressGate` 持有 connection generation、reset fence 与普通 outbound epoch。 |
| AD-003 | create 只使用 typed response 与即时 local callback；unknown 不自动 retry、不隔离其他 thread。 |
| AD-004 | 旧 durable root-operation/descendant writer 已删除；当前只保留 exact active-main-turn lease。 |
| AD-005 | Web backend/frontend 巨型状态机已拆为 document、profile、interest、read-model、mutation/action 等 owner；wire 边界随后由 AD-008 关闭。 |
| AD-006 | 高风险跨 owner 顺序已有具名 command/coordinator 与 RuntimeLoop guard；无事实 owner 的构造期 required-port graph、空 effect 和测试专用 façade 已删除。 |
| AD-007 | Binding runtime raw mutable state 已收进 trust zone 与 typed transition/snapshot 边界。 |
| AD-008 | 版本化 catalog 持有 Focus Web endpoint、event、DTO required field 与封闭 enum；双端 generated guard、producer fixture 与正式 wire 合同阻止平行清单和 drift。 |
| AD-009 | 旧 Handler/app-server 测试 aggregate 与零 production consumer 的 `_bind_thread` façade 已删除；测试按 owner 独立发现，装配细节仅留在有界 harness/integration suite。 |
| AD-010 | 可执行依赖方向门禁与 adapters、protocol、stores、Runtime Admin canonical package 已建立；剩余平铺模块按已证明 owner 边界逐案审查，不再视为整体债务。 |
| AD-011 | fcodex targetless create 只保留 current-generation one-shot capability。 |
| AD-012 | install/uninstall/purge 共用 idle-only lifecycle 与机器 lock owner；uninstall 删除受管 `.venv` 并保留数据，Windows 以 armed handoff barrier 和异步 exact result 完成自删除，不提供热升级、force 或 generation rollback。 |
| AD-013 | guardian 正常停止或崩溃后的常见恢复已通过 process identity 与 matching cleanup receipt 自动退休 exact generation。receipt 缺失/不匹配或 legacy direct-child record 刻意 fail-closed：操作者按平台边界独立核验进程后，只能删除该 exact instance record；不提供把 unknown 变成 proof 的 recovery/force 命令。 |
| AD-014 | server request 对标上游进程内 callback/replay；durable request/root/global fence 已删除。 |
| AD-015 | destination loss 只作用于 matching binding/delivery cleanup；page uncertainty 只约束一个原 UUID，terminal presentation 不再阻塞 main-turn 退役或 FIFO。 |
| AD-016 | Runtime Admin、Feishu ingress/cache/codec、managed process、RPC stop 与 RPC connection fact 均有明确 owner；原 aggregate 只剩 presentation、SDK、composition 或 typed façade。 |
| GAP-002 | external app-server deployment 已从产品面删除；内部 attached client 不取得 lifecycle authority。 |

Closed 表不是兼容承诺。若新需求要恢复其中的旧机制，必须重新提交上游证据、用户场景、最小合同与
可验证退出条件，不能直接复活历史实现。
