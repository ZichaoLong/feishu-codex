# 代码库审视报告 — 2026-04-29

> 后续更新：
> `f08f74b` 已修复本文原先记录的 service-level live runtime lease 异常释放回归，
> 并补上回归测试。当前真正剩余的跟进项，主要是 `_work` 文档卫生。

- 复核基线：`main` HEAD `581d937` + 当前未提交的 `ThreadAdmissionStore` 删除改动
- 测试基线：`pytest tests/ -q` → **547 passed**
- 复核原则：以当前代码 + 当前 `docs/contracts/` / `docs/architecture/` / `docs/decisions/` 为准；不沿用旧审视报告的结论

## 结论先行

- 本轮 `ThreadAdmissionStore` 删除是一次彻底的契约简化，code / tests / docs / README 同步度高，没有遗留 dead import、失联控制面端点或双语漂移
- 原先记录的 service-level live runtime lease 异常释放回归，已在 `f08f74b` 中修复，并同步补上回归测试
- 当前真正剩余的，是 `_work` 多实例草案仍可能把 admission 设计误读成现状；这属于文档卫生项

---

## 一、当前仍成立、需要跟进的问题

### 1.1 `docs/_work/multi-instance-rollout-plan*.md` 与 `multi-instance-admin-user-guide*.md` 仍把 admission 写成正在落地的设计（**P3 / 文档卫生**）

- 位置：
  - `docs/_work/multi-instance-rollout-plan.zh-CN.md:69-106, 226, 307-324`
  - `docs/_work/multi-instance-admin-user-guide.zh-CN.md:80, 121, 134`
  - 对应英文版 `multi-instance-rollout-plan.md`、`multi-instance-admin-user-guide.md`
- 这两份文件没有在 `docs/doc-index.md` 中索引，但代码里有人 `grep` 到这两份时容易把"命名实例 admission"当成现状
- 建议在文件头加一段 `Status: superseded — 命名实例不再做 admission，所有实例共享同一套 persisted thread 命名空间，详见 docs/contracts/runtime-control-surface.md §6.8`，或者直接重写到现状

---

## 二、已修复的本轮回归与旧 backlog

### 2.1 service-level live runtime lease 异常释放回归已修复

- 原回归点：`_bind_thread` / `_resume_snapshot_by_id` 在异常分支里会无条件释放 service-level runtime lease
- 修复提交：`f08f74b` `Keep shared runtime lease on bind failures`
- 修复方式：`_ensure_service_thread_runtime_lease` 现在返回 `was_newly_acquired`；只有本次真正新拿到 holder 时，异常分支才回滚 lease
- 回归覆盖：`tests/test_codex_handler.py` 已新增“已有 service holder 时 bind 失败 / resume 失败不能清掉 lease”的测试

下面这些点是更早的审视报告中曾标为 P1 / P2 的问题，在当前 HEAD 上已经不成立，记录在此以便后续不再重复立项：

| 旧问题 | 当前现状 | 证据位置 |
|---|---|---|
| 卡片文本 projection 把 strong-contract 与 best-effort 混线，终态卡片可能 fallback 到 `visible_text` | 已修复：终态卡片若抽不到 `final_reply_text` 现在返回空 `text`，不再降级 | `bot/card_text_projection.py:68-89` |
| 跨实例 live-runtime 转移缺少源端锁，存在 query-status 与 unsubscribe 之间的并发窗口 | 已修复：现在显式 `reserve_transfer → remote unsubscribe → 失败回滚 reservation` | `bot/thread_runtime_coordination.py:75-89`，`bot/stores/thread_runtime_lease_store.py:178+` 的 `reserve_transfer` |
| `shared_command_surface.py` 只覆盖部分一等命令，`/preflight` `/unsubscribe` 缺位 | 已修复：现已覆盖 `/help` `/profile` `/rm` `/session` `/preflight` `/resume` `/unsubscribe` 全集 | `bot/shared_command_surface.py:21-64` |
| `_resume_thread`、`_resolve_resume_target`、`_list_runtime_threads` 在 `default` 实例与命名实例上走两套分支 | 已统一：`/resume` `/session` 在所有实例上走同一份 `list_current_dir_threads` / `resolve_resume_target_by_name` | `bot/codex_handler.py:1834-2008` |

---

## 三、契约-代码一致性核查（本轮 ThreadAdmissionStore 删除）

- 代码侧：`bot/stores/thread_admission_store.py` 删除；`bot/codex_handler.py`、`bot/runtime_admin_controller.py`、`bot/feishu_codexctl.py` 全部移除相关字段、构造器参数、控制面端点；无 dead import
- 测试侧：`tests/test_thread_admission_store.py` 删除；`tests/test_codex_handler.py`、`tests/test_feishu_codexctl.py`、`tests/test_runtime_admin_controller.py` 同步移除 admission 相关 case
- 控制面：`feishu-codexctl thread admissions/import/revoke` 在 parser 与 dispatcher 中同步删除；`thread/admissions`、`thread/import`、`thread/revoke` 三个 HTTP endpoint 同步移除
- 文档侧：
  - `docs/contracts/runtime-control-surface{,.zh-CN}.md` §6.3 命令清单与 §6.8 章节标题语义同步重写
  - `docs/contracts/session-profile-semantics{,.zh-CN}.md` §2.1、§3、§5 同步重写
  - `docs/decisions/shared-backend-resume-safety{,.zh-CN}.md` §6 同步重写
  - `docs/contracts/local-command-and-thread-profile-contract{,.zh-CN}.md`、`docs/architecture/feishu-codex-design{,.zh-CN}.md`、`docs/doc-index{,.zh-CN}.md` 同步
  - 中英文双语 parity 保持
- README：`README.md:226-228, 441-447` 同步
- 字符串残留排查：当前代码与文档中剩余的 `admission` / `admit` 仅指 per-turn `interaction-owner admission` / `prompt admission` / `write admission`，与已删除的 per-instance thread admission 不是一个概念

---

## 四、未在本轮触碰、可放入下一轮 backlog 的条目

下列点不是回归，更像架构清理空间。当前只保留仍值得后续跟进的项：

- **说明**：`/group activate` 文案与 profile 责任边界不再列为遗留问题；前者当前成功文案已覆盖审批 / 补充输入 / 管理员兜底，后者也已在正式合同与架构文档中显式写明

---

## 五、推荐的最小收尾清单

按 ROI：

1. 给 `docs/_work/multi-instance-{rollout-plan,admin-user-guide}.{md,zh-CN.md}` 头部加 superseded 标注，避免后续维护者把它们当现状
2. 在本文头部或结论处补充“`1.1` 已由 `f08f74b` 修复”的状态更新，避免把已修复回归继续读成待办
