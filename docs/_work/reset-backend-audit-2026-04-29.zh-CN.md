# Reset-Backend 与 `/profile` reset 接入审视报告 — 2026-04-29

> 审视基线：`main` HEAD `fb3b6c5` + 当前未提交的 `service reset-backend` 控制面端点与 `/profile` reset 路径接入改动（19 文件，+1448/-107）。
> 测试基线：`python -m pytest tests/ -q` → **565 passed**（旧基线 547 + 本轮 18 用例）。
> 复核原则：以当前代码 + 当前 `docs/contracts/` / `docs/architecture/` / `docs/decisions/` 为准；先与既有 `docs/_work/codebase-audit-*.md` 的结论做差分，再补充新的发现。

> 后续状态（当前工作树）：
> - 1.1 的 `reason_code` 命名错位已修复为 `*_FORCE_ONLY_BY_RUNTIME_UNVERIFIED`
> - 1.2 的 README 命令清单已补上 `feishu-codexctl service reset-backend`
> - 1.3 提到的“自动 fail-close 会先 patch 卡片再 auto-reject”已写入 `docs/contracts/runtime-control-surface.{md,zh-CN.md} §2.6`
> - 下文保留原始审视记录，作为当时基线上的 review 快照

## 结论先行

- 本轮新增的 `service reset-backend` 控制面端点 + 飞书侧 `/profile` reset 路径，是契约 `runtime-control-surface §6.3.1` 与 `local-command-and-thread-profile §5.6 / §5.7` 的一次完整落地。
- 代码-契约-测试三角整体对齐，但有 **2 处命名一致性 / 文档卫生**问题，**1 处隐式行为变更**建议在 changelog 里明确说一声。
- 没有真正会爆的 bug；reset 路径的并发、生命周期、跨实例边界都已被 `_runtime_call` 串行化与 preview gate 覆盖。

与上一轮 `docs/_work/codebase-audit-2026-04-29.zh-CN.md` 的差分：

- 上轮 P3（`docs/_work/multi-instance-{rollout-plan,admin-user-guide}` 没有 superseded 标注）**已修复**，中英两份现在都带 `Status: superseded` 头，结论不再成立。
- 上轮 backlog "instance routing 重复 (`fcodex.py:176-236` vs `instance_resolution.py:53-97`)" 复核后发现已过时：`fcodex` 主入口现在通过 `resolve_cli_runtime_target(...)` 复用共享 resolver，自身只补充 `preferred_running_instance`，不再维护第二套 explicit / unique-running / default-running 解析规则。

---

## 一、当前仍成立的问题

### 1.1 reason_code 命名与 `status` 语义错位（**P3 / 命名一致性**）

- 位置：
  - 常量：`bot/reason_codes.py:22` `BACKEND_RESET_BLOCKED_BY_RUNTIME_UNVERIFIED`、`bot/reason_codes.py:33` `REPROFILE_BLOCKED_BY_RUNTIME_UNVERIFIED`
  - 使用点：`bot/runtime_admin_controller.py` 第 ~702 行 `_backend_reset_preview`（`runtime_verification_failed` 分支）；第 ~750 行 `plan_thread_reprofile`（force-only 分支）
- 这两个常量名字带 `BLOCKED_BY_`，但实际都用于 `status="force-only"` / `status="reset-force-only"` 分支：runtime 状态不可验证 → 仍可 force reset。其他四个力度同级的兄弟常量都叫 `BACKEND_RESET_FORCE_ONLY_BY_*`，命名不一致。
- 与契约的关系：契约 §6.3.1 写 "non-force execution is allowed only when ... backend state is verifiable" — 不可验证应该是 force-only 而不是 blocked。代码 status 给出的是 force-only（正确），错的是常量名字。
- 影响：将来在 telemetry / 日志检索 / 条件分支里靠 reason_code 判断，名字会误导维护者去走 blocked 分支。
- 建议：重命名为 `BACKEND_RESET_FORCE_ONLY_BY_RUNTIME_UNVERIFIED` / `REPROFILE_RESET_FORCE_ONLY_BY_RUNTIME_UNVERIFIED`，调整 `runtime_admin_controller.py` 的两处使用点。

### 1.2 `README.md` 常用命令清单未同步 `service reset-backend`（**P3 / 文档卫生**）

- 位置：`README.md:427-444` 那一段 `常用命令` 代码块。
- 契约 `runtime-control-surface.md §6.3` / `.zh-CN.md §6.3` 已把 `feishu-codexctl service reset-backend [--force]` 列入 formal command set，CLI 的 `epilog` 也加了这一行，但 README 主体的 "常用命令" 列表漏了。
- 影响：README 是新读者读到的第一份 command list；漏写会让人觉得它属于隐藏 / 未稳定动作。
- 建议：在 `feishu-codexctl service status` 后面加一行 `feishu-codexctl service reset-backend`，与契约对齐。

### 1.3 `fail_close_chat_requests` 隐含新增了 card patch 行为（**P4 / 行为变更需要在 changelog 里写一句**）

- 位置：`bot/interaction_request_controller.py:142-202`。
- 旧版本 `fail_close_chat_requests` 只 `auto_reject_request`，并不触动飞书侧的卡片。
- 重构成共享 `_fail_close_matching_requests` 后，**两条入口都会 patch 卡片**（user input → grey markdown card；其他 → approval-handled card）。
- 这是一次行为提升（用户能看到收口），但既不在合同里，也没有显式 changelog 行；以后如果用户报 "unsubscribe 后旧卡片为什么变 grey"，要回过来翻 git log 才能定位。
- 建议：本身不算 bug，提交信息或 PR description 里加一句 "`/unsubscribe` 路径现在也会同步 patch 还在 pending 的审批卡片" 即可。

---

## 二、复核后已对齐 / 不再成立的项目

下列点是上一轮 audit 或日常排查里会被怀疑的位置，本轮已逐项过：

| 复核点 | 现状 | 证据 |
|---|---|---|
| `_reset_current_instance_backend` 与并发请求 | 单线程化 | 控制面 `_handle_service_control_request:1743` 与卡片 action `handle_card_action:686` 都经 `_runtime_call`，串行到同一 runtime_loop |
| 反向破坏：reset 会不会动 binding bookmark / thread-wise profile | 不会 | `_reset_current_instance_backend:2087-2153` 只走 `unsubscribe_feishu_runtime_by_thread_id_locked` + `purge_all_for_instance` + `adapter.stop/start`；`thread_resume_profile_store` / `chat_binding_store` 没被 touch |
| `purge_all_for_instance` 的 transfer 路径 | 已覆盖 | `bot/stores/thread_runtime_lease_store.py:354-417`：`lease.holders` 过滤当前实例；`transfer.owner_instance == 当前实例` 或 `target_instance == 当前实例` 时清掉 transfer；剩余 holders 还有时把第一个 promote 为新 owner |
| `plan_thread_reprofile` 是否覆盖契约 §5.6 全集 | 是 | 顺序：unbound → globally-unloaded direct-write → other-instance-owner blocked → reset-preview 翻译；与契约 5 条不可直接写入条件全部对应 |
| `_apply_profile_after_backend_reset` reset 后是否再 gate | 是 | `bot/codex_settings_domain.py:626` reset 完再过 `check_thread_resume_profile_mutable`，避免 reset 完仍写不进 profile 的悄悄失败 |
| Force-only / blocked 的判定边界 | 与契约一致 | `pending_request → force-only`、`running_binding → force-only`、`active_loaded_thread → force-only`、`runtime_verification_failed → force-only`、`remote app-server → blocked`，与契约 §6.3.1 列举一致 |
| 中英文契约 parity | 对齐 | runtime-control-surface / local-command-and-thread-profile / session-profile-semantics 三对，section header 数量一致；全部 §6.3.1 / §5.6 改写均双语同步 |
| `_work/multi-instance-{rollout-plan,admin-user-guide}` superseded 标注 | 已落地 | EN 与 ZH 两份头部都加了 `Status: superseded`，上轮 P3 不再成立 |

---

## 三、契约 ↔ 代码对照（本轮 reset-backend / `/profile` reset 接入）

- **CLI 面**：`bot/feishu_codexctl.py` 新增 `service reset-backend [--force]` parser、dispatcher、status 上 `app_server_mode` / `backend_reset_status` / `backend_reset_reason_code` / `backend_reset_reason` 4 个字段输出；与契约 §6.3.1 "`service status` should expose ..." 一致。
- **控制面 dispatcher**：`bot/runtime_admin_controller.py` 增加 `service/reset-backend` 方法分支（line ~896）、`backend_reset_preview` / `plan_thread_reprofile` 两个 query API；与契约 §6.3.1 / §5.6 / §5.7 提到的状态机一致。
- **handler 落地**：`bot/codex_handler.py` 新增 `_interrupt_binding_execution_for_backend_reset` 与 `_reset_current_instance_backend`：先 preview gate（`status==blocked` raise / `force-only` 但 `force=False` raise），再依次 interrupt active turns → fail-close pending requests → unsubscribe runtime → `_adapter.stop()` → `purge_all_for_instance` → `_adapter.start()` → `_register_instance_runtime`，与契约 §6.3.1 各条对齐。
- **不会重启整个 service 进程**（仅 adapter 层）；**不会清 binding bookmark**（`unsubscribe_feishu_runtime_by_thread_id_locked` 不动 store 里的 binding 行）；**不会动 thread-wise profile / provider** — 与契约 "不会覆盖" 一节对齐。
- **飞书侧 `/profile` reset 路径**：`bot/codex_settings_domain.py` 新增 `_handle_profile_request` / `_apply_profile_after_backend_reset` / `_build_profile_summary_card`；卡片在 `reset-available` / `reset-force-only` 状态下 emit `apply_profile_with_backend_reset` action button；button 路由 `bot/codex_handler.py:1471` 接到 settings domain。整条路径与契约 §5.7 "Feishu Write Surface" 描述一致。
- **`feishu_runtime_state` 收敛**：`_reset_current_instance_backend` 在 `unsubscribe_feishu_runtime_by_thread_id_locked` 后显式 `_apply_persisted_runtime_state_message_locked(... ThreadStateChanged(feishu_runtime_state=FEISHU_RUNTIME_RELEASED))` 并 `_sync_stored_binding_locked`，让飞书侧的 binding 状态与 runtime fact 一致。
- **测试覆盖**：新增 18 用例，覆盖 reset 路径、profile→reset 卡片转换、CLI 转发 force flag、跨实例 owner 阻断、direct-write 路径四类场景。

---

## 四、未在本轮触碰、可放入下一轮 backlog 的条目

- **`docs/architecture/` / `docs/doc-index.md` 是否需要补 reset-backend 条目**：当前 doc-index 仍在指向三份契约入口，没有单独提 §6.3.1。读者按 "reset" 检索 doc-index 找不到对应条目；属可选改进，不阻塞。

---

## 五、推荐的最小收尾清单

按 ROI：

1. 重命名两个 `*_BLOCKED_BY_RUNTIME_UNVERIFIED` 常量为 `*_FORCE_ONLY_BY_RUNTIME_UNVERIFIED`，并同步两处使用点（`bot/runtime_admin_controller.py`）。改动量极小，避免之后有人按 reason_code 名字误判分支。
2. 给 `README.md:427-444` 的 `常用命令` 代码块加一行 `feishu-codexctl service reset-backend`。
3. 在本轮 PR description / changelog 提一句 "`fail_close_chat_requests` 现在会同步 patch pending 卡片"。
