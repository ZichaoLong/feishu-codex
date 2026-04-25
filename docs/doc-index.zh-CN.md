# 文档索引

这个目录是仓库架构、运行时边界、功能合同的事实来源。

## 阅读原则

如果代码行为与文档不一致，把它视为合同缺口，收紧代码、文档，或两者一起修正。

## 文档类型

当前 active 文档按角色分层：

- `docs/contracts/`
  - 正式功能合同与运行时行为合同
- `docs/architecture/`
  - 当前架构、分层、模块边界与实现形状
- `docs/decisions/`
  - 基于上游调查与安全边界分析形成的决策记录
- `docs/verification/`
  - 手测清单与验证辅助材料
- `docs/archive/`
  - 已完成计划与历史 rollout 记录；可用于理解来路，但不再是当前运行时合同

状态口径：

- `contracts/`、`architecture/`、`decisions/` 视为 active repository facts
- `verification/` 只用于验证，不定义产品语义
- `archive/` 只作历史参考
- `docs/_work/` 下的本地工作笔记不属于仓库事实源

## 按类型读

### 用户入口

- [README.md](../README.md)
  - 快速开始、安装、常用命令、运维避坑，以及继续深挖该看哪里

### 功能合同

- [`feishu-thread-lifecycle.zh-CN.md`](./contracts/feishu-thread-lifecycle.zh-CN.md)
- [`runtime-control-surface.zh-CN.md`](./contracts/runtime-control-surface.zh-CN.md)
- [`session-profile-semantics.zh-CN.md`](./contracts/session-profile-semantics.zh-CN.md)
- [`feishu-help-navigation.zh-CN.md`](./contracts/feishu-help-navigation.zh-CN.md)
- [`codex-permissions-model.zh-CN.md`](./contracts/codex-permissions-model.zh-CN.md)
- [`group-chat-contract.zh-CN.md`](./contracts/group-chat-contract.zh-CN.md)
- [`local-command-and-thread-profile-contract.zh-CN.md`](./contracts/local-command-and-thread-profile-contract.zh-CN.md)

### 架构设计

- [`feishu-codex-design.zh-CN.md`](./architecture/feishu-codex-design.zh-CN.md)
- [`fcodex-shared-backend-runtime.zh-CN.md`](./architecture/fcodex-shared-backend-runtime.zh-CN.md)

### 决策记录

- [`shared-backend-resume-safety.zh-CN.md`](./decisions/shared-backend-resume-safety.zh-CN.md)
- [`feishu-attachment-ingress.zh-CN.md`](./decisions/feishu-attachment-ingress.zh-CN.md)
- [`feishu-card-text-projection.zh-CN.md`](./decisions/feishu-card-text-projection.zh-CN.md)

### 验证材料

- [`group-chat-manual-test-checklist.zh-CN.md`](./verification/group-chat-manual-test-checklist.zh-CN.md)

### 历史归档

- [`codex-handler-decomposition-plan.zh-CN.md`](./archive/codex-handler-decomposition-plan.zh-CN.md)

## 按问题选文档

| 你想确认什么 | 应阅读的文档 |
| --- | --- |
| 当前总体架构、分层、模块划分、仓库结构是什么？ | [`feishu-codex-design.zh-CN.md`](./architecture/feishu-codex-design.zh-CN.md) |
| 飞书侧线程生命周期是什么？哪些状态绝不能混淆？ | [`feishu-thread-lifecycle.zh-CN.md`](./contracts/feishu-thread-lifecycle.zh-CN.md) |
| `/status`、`/unsubscribe`、`feishu-codexctl` 共享的状态词汇与管理面合同是什么？ | [`runtime-control-surface.zh-CN.md`](./contracts/runtime-control-surface.zh-CN.md) |
| `/session`、`/resume`、`/profile`、`/rm` 在飞书、`fcodex`、TUI 三层里分别是什么意思？ | [`session-profile-semantics.zh-CN.md`](./contracts/session-profile-semantics.zh-CN.md) |
| 本地命令面应如何重划？`unsubscribe`、`fcodex` thin wrapper、`feishu-codexctl` 分工、thread-wise profile/provider 的当前正式合同是什么？ | [`local-command-and-thread-profile-contract.zh-CN.md`](./contracts/local-command-and-thread-profile-contract.zh-CN.md) |
| 多实例下 `default` / 命名实例、admission、`fcodex --instance`、全局 runtime lease 怎么工作？ | [`session-profile-semantics.zh-CN.md`](./contracts/session-profile-semantics.zh-CN.md)、[`runtime-control-surface.zh-CN.md`](./contracts/runtime-control-surface.zh-CN.md)、[`fcodex-shared-backend-runtime.zh-CN.md`](./architecture/fcodex-shared-backend-runtime.zh-CN.md) |
| 飞书 `/help` 的信息架构、按钮导航与 slash 语义一致性合同是什么？ | [`feishu-help-navigation.zh-CN.md`](./contracts/feishu-help-navigation.zh-CN.md) |
| 群激活、群聊模式、历史回捞、群命令触发的正式合同是什么？ | [`group-chat-contract.zh-CN.md`](./contracts/group-chat-contract.zh-CN.md) |
| approval、sandbox、writable roots、受保护路径的语义是什么？ | [`codex-permissions-model.zh-CN.md`](./contracts/codex-permissions-model.zh-CN.md) |
| `fcodex` shared-backend 的运行时模型是什么？wrapper、本地代理、`--cd` 语义如何工作？ | [`fcodex-shared-backend-runtime.zh-CN.md`](./architecture/fcodex-shared-backend-runtime.zh-CN.md) |
| shared backend 复用与 `/resume` 有哪些安全规则？ | [`shared-backend-resume-safety.zh-CN.md`](./decisions/shared-backend-resume-safety.zh-CN.md) |
| 飞书附件 / 文件消息应如何进入本地工作区？哪些类型支持下载、哪些行为不由本仓库负责？ | [`feishu-attachment-ingress.zh-CN.md`](./decisions/feishu-attachment-ingress.zh-CN.md) |
| 飞书卡片消息的文本投影、终态 `final_reply_text`、以及普通卡片的 best-effort 文本提取边界是什么？ | [`feishu-card-text-projection.zh-CN.md`](./decisions/feishu-card-text-projection.zh-CN.md) |
| 群聊相关功能需要做哪些手工回归检查？ | [`group-chat-manual-test-checklist.zh-CN.md`](./verification/group-chat-manual-test-checklist.zh-CN.md) |
| `CodexHandler` ownership 拆分当时的 rollout 计划是什么？ | [`codex-handler-decomposition-plan.zh-CN.md`](./archive/codex-handler-decomposition-plan.zh-CN.md) |

## 常见阅读路径

- 做架构调整或较大重构时：
  - [`feishu-codex-design.zh-CN.md`](./architecture/feishu-codex-design.zh-CN.md)
  - 再按需补读相关 `contracts/` 与 `decisions/`
- 排查 session、线程恢复、运行时切换问题时：
  - [`feishu-thread-lifecycle.zh-CN.md`](./contracts/feishu-thread-lifecycle.zh-CN.md)
  - [`runtime-control-surface.zh-CN.md`](./contracts/runtime-control-surface.zh-CN.md)
  - [`session-profile-semantics.zh-CN.md`](./contracts/session-profile-semantics.zh-CN.md)
  - [`local-command-and-thread-profile-contract.zh-CN.md`](./contracts/local-command-and-thread-profile-contract.zh-CN.md)
  - [`shared-backend-resume-safety.zh-CN.md`](./decisions/shared-backend-resume-safety.zh-CN.md)
- 改群聊相关能力时：
  - [`group-chat-contract.zh-CN.md`](./contracts/group-chat-contract.zh-CN.md)
  - [`feishu-help-navigation.zh-CN.md`](./contracts/feishu-help-navigation.zh-CN.md)
  - [`group-chat-manual-test-checklist.zh-CN.md`](./verification/group-chat-manual-test-checklist.zh-CN.md)
- 改 `fcodex` wrapper、shared backend、本地代理相关逻辑时：
  - [`local-command-and-thread-profile-contract.zh-CN.md`](./contracts/local-command-and-thread-profile-contract.zh-CN.md)
  - [`fcodex-shared-backend-runtime.zh-CN.md`](./architecture/fcodex-shared-backend-runtime.zh-CN.md)
  - [`shared-backend-resume-safety.zh-CN.md`](./decisions/shared-backend-resume-safety.zh-CN.md)
- 改多实例、实例 admission、`feishu-codexctl --instance` 或跨实例 runtime lease 相关逻辑时：
  - [`session-profile-semantics.zh-CN.md`](./contracts/session-profile-semantics.zh-CN.md)
  - [`runtime-control-surface.zh-CN.md`](./contracts/runtime-control-surface.zh-CN.md)
  - [`fcodex-shared-backend-runtime.zh-CN.md`](./architecture/fcodex-shared-backend-runtime.zh-CN.md)
  - [`shared-backend-resume-safety.zh-CN.md`](./decisions/shared-backend-resume-safety.zh-CN.md)
- 改飞书附件、文件消息、本地暂存、图片输入升级相关逻辑时：
  - [`feishu-attachment-ingress.zh-CN.md`](./decisions/feishu-attachment-ingress.zh-CN.md)
  - [`codex-permissions-model.zh-CN.md`](./contracts/codex-permissions-model.zh-CN.md)
  - [`group-chat-contract.zh-CN.md`](./contracts/group-chat-contract.zh-CN.md)
- 改飞书卡片消息、终态结果 round-trip、普通卡片文本提取相关逻辑时：
  - [`feishu-card-text-projection.zh-CN.md`](./decisions/feishu-card-text-projection.zh-CN.md)
  - [`feishu-thread-lifecycle.zh-CN.md`](./contracts/feishu-thread-lifecycle.zh-CN.md)
  - [`feishu-codex-design.zh-CN.md`](./architecture/feishu-codex-design.zh-CN.md)
- 处理权限、执行审批、沙箱报错或产品文案时：
  - [`codex-permissions-model.zh-CN.md`](./contracts/codex-permissions-model.zh-CN.md)

## 语言说明

- 大部分技术文档同时提供英文版与中文版。
- 当前群聊手测清单只有中文版。
