# 文档索引

这个目录是仓库架构、运行时边界、功能合同的事实来源。

`AGENTS.md` 只记录仓库所有者的工程偏好，不替代这里的设计与合同文档。

## 使用方式

- 当你需要整体架构、当前仓库结构、模块边界时，先读 [`feishu-codex-design.zh-CN.md`](./feishu-codex-design.zh-CN.md)。
- 然后只按需继续阅读和当前修改直接相关的专题文档。
- 如果代码行为与文档不一致，把它视为合同缺口，收紧代码、文档，或两者一起修正。

## 按问题选文档

| 你想确认什么 | 应阅读的文档 |
| --- | --- |
| 当前总体架构、分层、模块划分、仓库结构是什么？ | [`feishu-codex-design.zh-CN.md`](./feishu-codex-design.zh-CN.md) |
| 飞书侧线程生命周期是什么？哪些状态绝不能混淆？ | [`feishu-thread-lifecycle.zh-CN.md`](./feishu-thread-lifecycle.zh-CN.md) |
| `/status`、`/release-feishu-runtime`、`feishu-codexctl` 共享的状态词汇与管理面合同是什么？ | [`runtime-control-surface.zh-CN.md`](./runtime-control-surface.zh-CN.md) |
| 飞书 `/help` 的信息架构、按钮导航与 slash 语义一致性合同是什么？ | [`feishu-help-navigation.zh-CN.md`](./feishu-help-navigation.zh-CN.md) |
| `fcodex` shared-backend 的运行时模型是什么？wrapper、本地代理、`--cd` 语义如何工作？ | [`fcodex-shared-backend-runtime.zh-CN.md`](./fcodex-shared-backend-runtime.zh-CN.md) |
| `/session`、`/resume`、`/profile`、`/rm` 在飞书、`fcodex`、TUI 三层里分别是什么意思？ | [`session-profile-semantics.zh-CN.md`](./session-profile-semantics.zh-CN.md) |
| shared backend 复用与 `/resume` 有哪些安全规则？ | [`shared-backend-resume-safety.zh-CN.md`](./shared-backend-resume-safety.zh-CN.md) |
| approval、sandbox、writable roots、受保护路径的语义是什么？ | [`codex-permissions-model.zh-CN.md`](./codex-permissions-model.zh-CN.md) |
| 群聊相关功能需要做哪些手工回归检查？ | [`group-chat-manual-test-checklist.zh-CN.md`](./group-chat-manual-test-checklist.zh-CN.md) |

## 常见阅读路径

- 做架构调整或较大重构时：先读 [`feishu-codex-design.zh-CN.md`](./feishu-codex-design.zh-CN.md)，再按主题补读对应文档。
- 排查 session、线程恢复、运行时切换问题时：重点读 [`feishu-thread-lifecycle.zh-CN.md`](./feishu-thread-lifecycle.zh-CN.md)、[`runtime-control-surface.zh-CN.md`](./runtime-control-surface.zh-CN.md)、[`session-profile-semantics.zh-CN.md`](./session-profile-semantics.zh-CN.md)、[`shared-backend-resume-safety.zh-CN.md`](./shared-backend-resume-safety.zh-CN.md)。
- 改飞书 `/help`、命令可发现性、按钮与 slash 语义一致性时：先读 [`feishu-help-navigation.zh-CN.md`](./feishu-help-navigation.zh-CN.md)，再按它引用的专题文档继续读。
- 改 `fcodex` wrapper、shared backend、本地代理相关逻辑时：重点读 [`fcodex-shared-backend-runtime.zh-CN.md`](./fcodex-shared-backend-runtime.zh-CN.md) 和 [`shared-backend-resume-safety.zh-CN.md`](./shared-backend-resume-safety.zh-CN.md)。
- 处理权限、执行审批、沙箱报错或产品文案时：先读 [`codex-permissions-model.zh-CN.md`](./codex-permissions-model.zh-CN.md)。

## 语言说明

- 大部分技术文档同时提供英文版与中文版。
- 当前群聊手测清单只有中文版。
