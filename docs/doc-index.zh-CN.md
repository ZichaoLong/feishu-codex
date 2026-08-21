# 文档索引

这个目录是仓库架构、运行时边界、功能合同的事实来源。

## 阅读原则

如果代码行为与文档不一致，把它视为合同缺口，收紧代码、文档，或两者一起修正。

### 双语事实源

- `contracts/`、`architecture/`、`decisions/` 中的 active 双语对以中文文档为
  canonical 规范源，英文文档是同步 peer。若两者语义冲突，以中文规范源为准，
  同时把英文漂移视为必须修复的合同缺口。
- 每个 active 中文文档标题后必须声明“中文规范源”并精确链接英文副本；
  英文文档必须声明“synchronized English peer”并精确链接 canonical Chinese。
- 改变产品语义时，应在同一变更中同步两份。`check-docs.sh` 会校验文件对、
  角色与互链，但不会假装能自动证明两种自然语言的语义完全一致；后者仍需要审阅。

### 多前端合同的优先顺序

只要问题涉及 Web、飞书、`focus` / `fcodex` 或本地控制面如何操作同一个 shared thread，就按以下顺序读当前正式合同：

1. `contracts/root-operation-owner.zh-CN.md` 定义 lease-bearing 飞书/exclusive action 的唯一
   main-turn writer 与立即释放规则，以及普通 Web/`fcodex` realtime input/control 的 effect-specific 边界。
2. `contracts/fcodex-operation-owner.zh-CN.md` 与
   `contracts/subagent-observation-and-recovery.zh-CN.md` 分别补充 fcodex transport 细节，以及
   upstream-owned child lifecycle、parent-history Tasks、direct-child write 与 cold-resume 边界。
3. 某个前端的 lifecycle、settings、群聊或命令合同只管理自己的 effect；除非另有可观察 invariant 的明确论证，
   不得延长 lease-bearing main-turn ownership，也不得把普通 realtime contributor 重新解释成 writer。

这样不会把 retained recovery、subscription、presentation 或 transport 状态误读成 durable main-turn writer。

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

- [`focusctl-command-matrix.zh-CN.md`](./contracts/focusctl-command-matrix.zh-CN.md)
- [`feishu-command-matrix.zh-CN.md`](./contracts/feishu-command-matrix.zh-CN.md)
- [`feishu-thread-lifecycle.zh-CN.md`](./contracts/feishu-thread-lifecycle.zh-CN.md)
- [`runtime-control-surface.zh-CN.md`](./contracts/runtime-control-surface.zh-CN.md)
- [`runtime-settings-fact-sources.zh-CN.md`](./contracts/runtime-settings-fact-sources.zh-CN.md)
- [`thread-next-load-settings-semantics.zh-CN.md`](./contracts/thread-next-load-settings-semantics.zh-CN.md)
- [`thread-profile-semantics.zh-CN.md`](./contracts/thread-profile-semantics.zh-CN.md)
- [`thread-resume-local-commit.zh-CN.md`](./contracts/thread-resume-local-commit.zh-CN.md)
- [`thread-create-local-commit.zh-CN.md`](./contracts/thread-create-local-commit.zh-CN.md)
- [`thread-memory-semantics.zh-CN.md`](./contracts/thread-memory-semantics.zh-CN.md)
- [`feishu-help-navigation.zh-CN.md`](./contracts/feishu-help-navigation.zh-CN.md)
- [`scheduled-prompts.zh-CN.md`](./contracts/scheduled-prompts.zh-CN.md)
- [`codex-config.zh-CN.md`](./contracts/codex-config.zh-CN.md)
- [`system-config.zh-CN.md`](./contracts/system-config.zh-CN.md)
- [`codex-permissions-model.zh-CN.md`](./contracts/codex-permissions-model.zh-CN.md)
- [`group-chat-contract.zh-CN.md`](./contracts/group-chat-contract.zh-CN.md)
- [`local-command-and-thread-profile-contract.zh-CN.md`](./contracts/local-command-and-thread-profile-contract.zh-CN.md)
- [`subagent-observation-and-recovery.zh-CN.md`](./contracts/subagent-observation-and-recovery.zh-CN.md)
- [`root-operation-owner.zh-CN.md`](./contracts/root-operation-owner.zh-CN.md)
- [`server-request-lifecycle.zh-CN.md`](./contracts/server-request-lifecycle.zh-CN.md)
- [`codex-app-server-schema-drift.zh-CN.md`](./contracts/codex-app-server-schema-drift.zh-CN.md)
- [`focus-web-wire.zh-CN.md`](./contracts/focus-web-wire.zh-CN.md)
- [`focus-web-prompt-mutation-recovery.zh-CN.md`](./contracts/focus-web-prompt-mutation-recovery.zh-CN.md)
- [`fcodex-operation-owner.zh-CN.md`](./contracts/fcodex-operation-owner.zh-CN.md)
- [`install-artifact-delivery.zh-CN.md`](./contracts/install-artifact-delivery.zh-CN.md)

### 架构设计

- [`focus-design.zh-CN.md`](./architecture/focus-design.zh-CN.md)
- [`focus-shared-backend-runtime.zh-CN.md`](./architecture/focus-shared-backend-runtime.zh-CN.md)
- [`development-navigation.zh-CN.md`](./architecture/development-navigation.zh-CN.md)
- [`architecture-debt-register.zh-CN.md`](./architecture/architecture-debt-register.zh-CN.md)

### 决策记录

- [`python-dependency-locking.zh-CN.md`](./decisions/python-dependency-locking.zh-CN.md)
- [`cross-instance-live-runtime-admission.zh-CN.md`](./decisions/cross-instance-live-runtime-admission.zh-CN.md)
- [`feishu-attachment-ingress.zh-CN.md`](./decisions/feishu-attachment-ingress.zh-CN.md)
- [`feishu-card-text-projection.zh-CN.md`](./decisions/feishu-card-text-projection.zh-CN.md)
- [`feishu-raw-card-retrieval.zh-CN.md`](./decisions/feishu-raw-card-retrieval.zh-CN.md)
- [`feishu-output-images.zh-CN.md`](./decisions/feishu-output-images.zh-CN.md)
- [`focus-web-ui-and-kimi-web-reuse.zh-CN.md`](./decisions/focus-web-ui-and-kimi-web-reuse.zh-CN.md)
- [`focus-web-external-access.zh-CN.md`](./decisions/focus-web-external-access.zh-CN.md)

### 验证材料

- [`group-chat-manual-test-checklist.zh-CN.md`](./verification/group-chat-manual-test-checklist.zh-CN.md)

### 历史归档

- [`codex-handler-decomposition-plan.zh-CN.md`](./archive/codex-handler-decomposition-plan.zh-CN.md)

## 按问题选文档

| 你想确认什么 | 应阅读的文档 |
| --- | --- |
| `focusctl` 到底有哪些子命令、分别作用于哪个状态层、哪些会改状态、参数约束是什么、以及与飞书命令面如何对应？ | [`focusctl-command-matrix.zh-CN.md`](./contracts/focusctl-command-matrix.zh-CN.md) |
| 飞书侧到底有哪些 slash 命令、哪些能从 `/help` 到达、谁可执行、有哪些按钮、以及它们与本地 CLI 的对应关系是什么？ | [`feishu-command-matrix.zh-CN.md`](./contracts/feishu-command-matrix.zh-CN.md) |
| 当前总体架构、分层、模块划分、仓库结构是什么？ | [`focus-design.zh-CN.md`](./architecture/focus-design.zh-CN.md) |
| 日常开发应如何限制首轮读取、按证据扩展变更锥、处理 stale 导航，并在不制造第二事实源的前提下闭合导航影响？ | [`development-navigation.zh-CN.md`](./architecture/development-navigation.zh-CN.md) |
| 当前有哪些未结架构债务、上游能力缺口、依赖顺序与验收条件？ | [`architecture-debt-register.zh-CN.md`](./architecture/architecture-debt-register.zh-CN.md) |
| 飞书侧线程生命周期是什么？哪些状态绝不能混淆？ | [`feishu-thread-lifecycle.zh-CN.md`](./contracts/feishu-thread-lifecycle.zh-CN.md) |
| `/status`、`/detach`、`focusctl` 共享的状态词汇与管理面合同是什么？ | [`runtime-control-surface.zh-CN.md`](./contracts/runtime-control-surface.zh-CN.md) |
| 某个运行时设置“刚写了什么”“持久化在哪”“什么时候才算生效”“provisional 阶段是否已有正式事实源”这些问题该怎么区分？ | [`runtime-settings-fact-sources.zh-CN.md`](./contracts/runtime-settings-fact-sources.zh-CN.md)、[`runtime-control-surface.zh-CN.md`](./contracts/runtime-control-surface.zh-CN.md) |
| 历史上的 thread-wise next-load 设置为何被收缩掉、收缩后还剩什么？ | [`thread-next-load-settings-semantics.zh-CN.md`](./contracts/thread-next-load-settings-semantics.zh-CN.md)、[`thread-profile-semantics.zh-CN.md`](./contracts/thread-profile-semantics.zh-CN.md) |
| `/threads`、`/resume`、`/archive` 在飞书、`focus` / `fcodex`、TUI 三层里分别是什么意思？ | [`thread-profile-semantics.zh-CN.md`](./contracts/thread-profile-semantics.zh-CN.md) |
| app-server 返回 `thread/resume` 成功后，必须先提交哪一层本地 owner/interest 才能结算事务？失败时何时可补偿、何时必须保留 recovery？ | [`thread-resume-local-commit.zh-CN.md`](./contracts/thread-resume-local-commit.zh-CN.md) |
| Web/飞书与 targetless `focus` / `fcodex` 在收到 typed `thread/start` response 后执行哪份最小本地提交；为何 unknown create 只能限于该 request 且不能自动重试？ | [`thread-create-local-commit.zh-CN.md`](./contracts/thread-create-local-commit.zh-CN.md) |
| 历史上的 thread memory 控制面为何被移除、现在应改看哪两层设置？ | [`thread-memory-semantics.zh-CN.md`](./contracts/thread-memory-semantics.zh-CN.md)、[`runtime-settings-fact-sources.zh-CN.md`](./contracts/runtime-settings-fact-sources.zh-CN.md) |
| 如何在未来时点继续当前 Feishu 绑定 thread？`binding/submit-prompt`、`focusctl prompt send`、Linux `systemd --user` skill 的正式边界是什么？ | [`scheduled-prompts.zh-CN.md`](./contracts/scheduled-prompts.zh-CN.md) |
| `codex.yaml` 接受哪些键和类型？默认值与校验的权威事实源在哪里？ | [`codex-config.zh-CN.md`](./contracts/codex-config.zh-CN.md) |
| `system.yaml` 接受哪些实例身份、触发、网络与历史回捞字段？`/init` 如何受控更新？ | [`system-config.zh-CN.md`](./contracts/system-config.zh-CN.md) |
| 本地命令面应如何重划？`/detach`、`focus` / `fcodex` thin wrapper、`focusctl` 分工的当前正式合同是什么？ | [`local-command-and-thread-profile-contract.zh-CN.md`](./contracts/local-command-and-thread-profile-contract.zh-CN.md) |
| 多实例下 `default` / 命名实例、共享 thread 可见面、`focus --instance` / `fcodex --instance`、全局 runtime lease 怎么工作？ | [`thread-profile-semantics.zh-CN.md`](./contracts/thread-profile-semantics.zh-CN.md)、[`runtime-control-surface.zh-CN.md`](./contracts/runtime-control-surface.zh-CN.md)、[`focus-shared-backend-runtime.zh-CN.md`](./architecture/focus-shared-backend-runtime.zh-CN.md) |
| 飞书 `/help` 的信息架构、按钮导航与 slash 语义一致性合同是什么？ | [`feishu-help-navigation.zh-CN.md`](./contracts/feishu-help-navigation.zh-CN.md) |
| 群激活、群聊模式、历史回捞、群命令触发的正式合同是什么？ | [`group-chat-contract.zh-CN.md`](./contracts/group-chat-contract.zh-CN.md) |
| approval、sandbox、writable roots、受保护路径的语义是什么？ | [`codex-permissions-model.zh-CN.md`](./contracts/codex-permissions-model.zh-CN.md) |
| `focus` / `fcodex` shared-backend 的运行时模型是什么？wrapper、本地代理、`--cd` 语义如何工作？ | [`focus-shared-backend-runtime.zh-CN.md`](./architecture/focus-shared-backend-runtime.zh-CN.md) |
| shared backend 复用与 `/resume` 有哪些安全规则？ | [`focus-shared-backend-runtime.zh-CN.md`](./architecture/focus-shared-backend-runtime.zh-CN.md)、[`thread-resume-local-commit.zh-CN.md`](./contracts/thread-resume-local-commit.zh-CN.md)、[`root-operation-owner.zh-CN.md`](./contracts/root-operation-owner.zh-CN.md) |
| 飞书附件 / 文件消息应如何进入本地工作区？哪些类型支持下载、哪些行为不由本仓库负责？ | [`feishu-attachment-ingress.zh-CN.md`](./decisions/feishu-attachment-ingress.zh-CN.md) |
| 飞书卡片消息的文本投影、终态 `final_reply_text`、以及普通卡片的 best-effort 文本提取边界是什么？ | [`feishu-card-text-projection.zh-CN.md`](./decisions/feishu-card-text-projection.zh-CN.md) |
| 飞书卡片如何从 JSON 2.0 升级到“按 `message_id` 原卡读取”？普通转发、合并转发、best-effort 投影之间的读取决策是什么？重启后又如何确认项目实际收到了什么？ | [`feishu-raw-card-retrieval.zh-CN.md`](./decisions/feishu-raw-card-retrieval.zh-CN.md) |
| 飞书出站生成图片的当前边界是什么？文本为何必须先于图片送达？为什么任意工作区图片不在范围内？ | [`feishu-output-images.zh-CN.md`](./decisions/feishu-output-images.zh-CN.md) |
| Focus Web 为什么复用 kimi-web 源码？导入后谁拥有 adapter/projection，provenance 与 license 如何维护？ | [`focus-web-ui-and-kimi-web-reuse.zh-CN.md`](./decisions/focus-web-ui-and-kimi-web-reuse.zh-CN.md) |
| spawned subagent 的 upstream lifecycle、parent-history Tasks、direct-child write 与 cold-resume 边界是什么？为什么 Focus 不观察、恢复或补投 child？ | [`subagent-observation-and-recovery.zh-CN.md`](./contracts/subagent-observation-and-recovery.zh-CN.md) |
| 哪些飞书/exclusive action 仍取得 main-turn writer，普通 Web/`fcodex` input 为何不取得，effect-specific steer/interrupt 为什么不转移 writer，已有 writer 何时释放？ | [`root-operation-owner.zh-CN.md`](./contracts/root-operation-owner.zh-CN.md) |
| Codex pending server request 由谁持有？replay、response、lifecycle、disconnect 与 backend reset 如何投影？为什么 Focus 不再保存 durable request fence？ | [`server-request-lifecycle.zh-CN.md`](./contracts/server-request-lifecycle.zh-CN.md) |
| Codex app-server 升级时如何发现 method/schema 漂移？哪些生成物与分类必须经过审阅？ | [`codex-app-server-schema-drift.zh-CN.md`](./contracts/codex-app-server-schema-drift.zh-CN.md) |
| Focus Web endpoint、event、DTO required field 与封闭 enum 的唯一事实源在哪里？异常 HTTP/event 在何处 fail closed？ | [`focus-web-wire.zh-CN.md`](./contracts/focus-web-wire.zh-CN.md) |
| existing-thread Web prompt 如何用单次 POST 与有界 result receipt 限制重复 effect，并在 F5、断线、乱序或 unknown outcome 后只查询结果、不重放 payload 或恢复附件？ | [`focus-web-prompt-mutation-recovery.zh-CN.md`](./contracts/focus-web-prompt-mutation-recovery.zh-CN.md) |
| fcodex 连接如何落实 ordinary realtime input 与 exclusive action 的不同边界、限制无 target 的 app-server RPC，并让 transport/server-request recovery 与 writer authority 分离？ | [`root-operation-owner.zh-CN.md`](./contracts/root-operation-owner.zh-CN.md)、[`fcodex-operation-owner.zh-CN.md`](./contracts/fcodex-operation-owner.zh-CN.md) |
| Focus Web 自部署外部访问的边界是什么，包括 loopback 默认值、反向代理、共享信任域与未来公网暴露？ | [`focus-web-external-access.zh-CN.md`](./decisions/focus-web-external-access.zh-CN.md) |
| clone 后的默认安装从哪里取得 Focus 与 Web 成品？stable、development、本地 bundle、下载校验和显式发布的合同是什么？ | [`install-artifact-delivery.zh-CN.md`](./contracts/install-artifact-delivery.zh-CN.md)、[`python-dependency-locking.zh-CN.md`](./decisions/python-dependency-locking.zh-CN.md) |
| Python 运行、构建与开发依赖分别在哪里声明？lock 如何重生成或显式升级？安装实际保证到哪一级可复现性？ | [`python-dependency-locking.zh-CN.md`](./decisions/python-dependency-locking.zh-CN.md) |
| 跨实例 `attach / resume` 之前到底要遵守什么安全准入规则？为什么不能只看 `ThreadRuntimeLease`？ | [`cross-instance-live-runtime-admission.zh-CN.md`](./decisions/cross-instance-live-runtime-admission.zh-CN.md)、[`runtime-control-surface.zh-CN.md`](./contracts/runtime-control-surface.zh-CN.md) |
| 群聊相关功能需要做哪些手工回归检查？ | [`group-chat-manual-test-checklist.zh-CN.md`](./verification/group-chat-manual-test-checklist.zh-CN.md) |
| `CodexHandler` ownership 拆分当时的 rollout 计划是什么？ | [`codex-handler-decomposition-plan.zh-CN.md`](./archive/codex-handler-decomposition-plan.zh-CN.md) |

## 常见阅读路径

- 做任何仓库诊断、实现、review 或重构时：
  - 可用单句 `使用 $develop-focus 完成：<任务>` 启用当前完整开发纪律
  - [`development-navigation.zh-CN.md`](./architecture/development-navigation.zh-CN.md)
  - 再只读它指向的 capability refs 与有证据触发的相邻来源
- 做架构调整或较大重构时：
  - [`focus-design.zh-CN.md`](./architecture/focus-design.zh-CN.md)
  - [`architecture-debt-register.zh-CN.md`](./architecture/architecture-debt-register.zh-CN.md)
  - 再按需补读相关 `contracts/` 与 `decisions/`
- 排查 session、线程恢复、运行时切换问题时：
  - [`feishu-thread-lifecycle.zh-CN.md`](./contracts/feishu-thread-lifecycle.zh-CN.md)
  - [`runtime-control-surface.zh-CN.md`](./contracts/runtime-control-surface.zh-CN.md)
  - [`runtime-settings-fact-sources.zh-CN.md`](./contracts/runtime-settings-fact-sources.zh-CN.md)
  - [`thread-profile-semantics.zh-CN.md`](./contracts/thread-profile-semantics.zh-CN.md)
  - [`thread-resume-local-commit.zh-CN.md`](./contracts/thread-resume-local-commit.zh-CN.md)
  - [`local-command-and-thread-profile-contract.zh-CN.md`](./contracts/local-command-and-thread-profile-contract.zh-CN.md)
  - [`cross-instance-live-runtime-admission.zh-CN.md`](./decisions/cross-instance-live-runtime-admission.zh-CN.md)
- 改群聊相关能力时：
  - [`feishu-command-matrix.zh-CN.md`](./contracts/feishu-command-matrix.zh-CN.md)
  - [`group-chat-contract.zh-CN.md`](./contracts/group-chat-contract.zh-CN.md)
  - [`feishu-help-navigation.zh-CN.md`](./contracts/feishu-help-navigation.zh-CN.md)
  - [`group-chat-manual-test-checklist.zh-CN.md`](./verification/group-chat-manual-test-checklist.zh-CN.md)
- 改本地 `focusctl` 查看 / 管理面时：
  - [`focusctl-command-matrix.zh-CN.md`](./contracts/focusctl-command-matrix.zh-CN.md)
  - [`scheduled-prompts.zh-CN.md`](./contracts/scheduled-prompts.zh-CN.md)
  - [`local-command-and-thread-profile-contract.zh-CN.md`](./contracts/local-command-and-thread-profile-contract.zh-CN.md)
  - [`runtime-control-surface.zh-CN.md`](./contracts/runtime-control-surface.zh-CN.md)
  - [`runtime-settings-fact-sources.zh-CN.md`](./contracts/runtime-settings-fact-sources.zh-CN.md)
  - [`thread-profile-semantics.zh-CN.md`](./contracts/thread-profile-semantics.zh-CN.md)
- 改安装 bundle、本地制品构建或制品发布时：
  - [`install-artifact-delivery.zh-CN.md`](./contracts/install-artifact-delivery.zh-CN.md)
  - [`python-dependency-locking.zh-CN.md`](./decisions/python-dependency-locking.zh-CN.md)
- 改 `focus` / `fcodex` wrapper、shared backend、本地代理相关逻辑时：
  - [`local-command-and-thread-profile-contract.zh-CN.md`](./contracts/local-command-and-thread-profile-contract.zh-CN.md)
  - [`root-operation-owner.zh-CN.md`](./contracts/root-operation-owner.zh-CN.md)
  - [`fcodex-operation-owner.zh-CN.md`](./contracts/fcodex-operation-owner.zh-CN.md)
  - [`focus-shared-backend-runtime.zh-CN.md`](./architecture/focus-shared-backend-runtime.zh-CN.md)
- 改多实例、共享 thread 可见面、`focusctl --instance` 或跨实例 runtime lease 相关逻辑时：
  - [`thread-profile-semantics.zh-CN.md`](./contracts/thread-profile-semantics.zh-CN.md)
  - [`runtime-control-surface.zh-CN.md`](./contracts/runtime-control-surface.zh-CN.md)
  - [`cross-instance-live-runtime-admission.zh-CN.md`](./decisions/cross-instance-live-runtime-admission.zh-CN.md)
  - [`focus-shared-backend-runtime.zh-CN.md`](./architecture/focus-shared-backend-runtime.zh-CN.md)
- 改飞书附件、文件消息、本地暂存、图片输入升级相关逻辑时：
  - [`feishu-attachment-ingress.zh-CN.md`](./decisions/feishu-attachment-ingress.zh-CN.md)
  - [`feishu-output-images.zh-CN.md`](./decisions/feishu-output-images.zh-CN.md)
  - [`codex-permissions-model.zh-CN.md`](./contracts/codex-permissions-model.zh-CN.md)
  - [`group-chat-contract.zh-CN.md`](./contracts/group-chat-contract.zh-CN.md)
- 改飞书卡片消息、终态结果 round-trip、普通卡片文本提取相关逻辑时：
  - [`feishu-card-text-projection.zh-CN.md`](./decisions/feishu-card-text-projection.zh-CN.md)
  - [`feishu-raw-card-retrieval.zh-CN.md`](./decisions/feishu-raw-card-retrieval.zh-CN.md)
  - [`feishu-output-images.zh-CN.md`](./decisions/feishu-output-images.zh-CN.md)
  - [`feishu-thread-lifecycle.zh-CN.md`](./contracts/feishu-thread-lifecycle.zh-CN.md)
  - [`focus-design.zh-CN.md`](./architecture/focus-design.zh-CN.md)
- 处理权限、执行审批、沙箱报错或产品文案时：
  - [`codex-permissions-model.zh-CN.md`](./contracts/codex-permissions-model.zh-CN.md)
- 设计浏览器前端、完整 Markdown、远程 Web 访问或 kimi-web 复用时：
  - [`focus-web-wire.zh-CN.md`](./contracts/focus-web-wire.zh-CN.md)
  - [`focus-web-prompt-mutation-recovery.zh-CN.md`](./contracts/focus-web-prompt-mutation-recovery.zh-CN.md)
  - [`focus-web-ui-and-kimi-web-reuse.zh-CN.md`](./decisions/focus-web-ui-and-kimi-web-reuse.zh-CN.md)
  - [`focus-web-external-access.zh-CN.md`](./decisions/focus-web-external-access.zh-CN.md)
  - [`root-operation-owner.zh-CN.md`](./contracts/root-operation-owner.zh-CN.md)
  - [`subagent-observation-and-recovery.zh-CN.md`](./contracts/subagent-observation-and-recovery.zh-CN.md)
  - [`focus-design.zh-CN.md`](./architecture/focus-design.zh-CN.md)
  - [`focus-shared-backend-runtime.zh-CN.md`](./architecture/focus-shared-backend-runtime.zh-CN.md)

## 语言说明

- 大部分技术文档同时提供英文版与中文版。
- 当前群聊手测清单只有中文版。
