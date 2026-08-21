# Codex 权限模型

文档角色：中文规范源。英文同步副本：`docs/contracts/codex-permissions-model.md`。

本文记录 FOCUS 当前暴露的 `approval_policy` 与 `permissions_profile_id`，以及 upstream 里 legacy `sandbox` / canonical `permissions` 的关系。

写这篇文档有两个目的：

- 让 Focus 各前端的文案始终与 upstream Codex 行为保持一致
- 把精简用户帮助与实现细节、排障细节分开

上游基线：

- Codex 源码仓库：[`openai/codex`](https://github.com/openai/codex.git)
- 下文上游细节与行号链接的历史本地验证基线：`codex-cli 0.118.0`，本地可解析到上游 tag
  `rust-v0.118.0`（commit
  `b630ce9a4e754d35a1f33e4366ba638d18626142`），核对日期为 2026-04-03
- 文中后续的上游文件 / 行号引用，均固定到这次基线对应的 commit，便于后续开发者恢复当时讨论的精确源码快照
- 它不表示所有当前 app-server 行为仍固定在该版本；当前 Web/多前端合同会注明各自的调查基线。
- Focus 的封闭 approval/reviewer 表面已再次对照 upstream commit
  [`f21dc4638803f40046c9e294b0349782928f6b36`](https://github.com/openai/codex/commit/f21dc4638803f40046c9e294b0349782928f6b36)（2026-08-05）核对。该版本包含
  结构化 `granular` approval 与 `auto_review`；两者都尚未进入 Focus 当前产品合同。
- managed requirements 的 lifecycle 行为又在 upstream commit
  [`f4cfbaf90af76f7c0b3301e931d8a58f4b56cc31`](https://github.com/openai/codex/commit/f4cfbaf90af76f7c0b3301e931d8a58f4b56cc31)（2026-08-08）核对；它会因具体
  路径及 thread 是否已 loaded 而 fallback、忽略或拒绝，不提供统一的 effect 前准入。

## 1. 三层概念

FOCUS 当前暴露两个正式 runtime setting，并需要理解一个上游 legacy 概念：

1. `approval_policy`
- 运行在什么时机需要停下来等待审批

2. `permissions_profile_id`
- Focus 在后续某个 turn 注入哪个 upstream permission profile id

3. legacy `sandbox`
- 上游仍兼容，但飞书侧不再把它当成正式 persisted setting

重要点在于：飞书侧当前的 `/permissions` 已不再是“组合预设”，而是独立设置 upstream canonical
`permissions` profile id。其他 Focus 前端复用同一个 upstream setting，含义不变，但不因此共用持久化事实：
飞书仍是 binding-wise；Web 则使用同一 instance 所有 browser/thread 共享的 durable
`WebNextTurnSettings`。两者都不与本地 TUI 自动合并。main-turn admission 只路由 live mutation 或 reviewer
request；它不拥有或阻止 instance-wide Web settings mutation，也不会让各 frontend 的设置自动合并。具体生效边界
只由 [runtime settings 事实源合同](./runtime-settings-fact-sources.zh-CN.md) 定义。

## 2. Approval 与 Sandbox

最简洁的心智模型是：

- `sandbox` 是技术执行边界
- `approval_policy` 是审批边界

这个模型总体是对的，但还需要几处精度修正。

### 2.1 在 upstream 里，approval 不必然等于“人工审批”

upstream Codex 把 approval 建模为“策略 + reviewer 流程”，并不严格等同于“必须由一个人点击批准”。

Focus canonical adapter 当前发送 `approvals_reviewer=user`。在 Focus 边界内，当前 backend epoch 中带非空 `turnId` 且
属于 exact direct root 的 canonical command、file-change 或 permission approval 会投影到其 exact
Feishu binding，以及所有已 materialize 该 root 的 authenticated live Web/fcodex endpoint。每个 surface
取得 exact 一次性 capability，第一份有效 response 胜出。其他飞书 chat 不进入该 domain；user-input、
MCP、authentication、dynamic-tool 与 child-thread request 保持原 exact route。因此，把
`approval_policy` 解释为审批边界仍然准确，但 turn writer 不再是唯一可能审批者；无 writer 的 autonomous
goal turn 也不因此失去当前 canonical approval。

跨前端准入之后，各前端仍可施加额外的本地 actor 规则。处于**激活状态**的飞书群里，管理员只能在
同一个已准入的 Feishu binding 内、按群 interaction guard 作为卡片兜底处理者。可信本机 Web/fcodex
对同一 shared approval 另有管理员等价的 response authority，但这绝不授权其他 Feishu binding，也不授予
writer、settings、goal、binding 或 backend-reset authority。群停用后，普通成员 origin 的 pending 必须 fail-close；取消未确认时它只是 blocker，
而不是管理员接管。只有原始 origin 本来就是管理员的请求，才仍是常规管理员工作。browser/socket
断线只删除本地 projection；之后 upstream resume replay 可以创建 fresh actionable capability。

审批选项遵守 upstream 协议形状，而不是统一依赖某一个 request 字段：command approval 遵守
`availableDecisions`，file 与 permission approval 展示各自 schema 定义的选项，包括 session approval
与 permission `strictAutoReview`。Focus 不制造 upstream 未定义的 response enum。

当前 upstream 还暴露了 `auto_review`，会把审批交给带专门 prompt 的 subagent。
Focus 尚未为这条路径建立经过审阅的 interaction、ownership 与 audit 合同，因此
`codex.yaml` 仍只接受 `user`。upstream 新增 enum 是需要评估的 capability gap，
不会自动成为本地功能。native fcodex TUI transport 是另一条边界：它保留 TUI 的
upstream-owned reviewer 字段，但不会把该值变成 Focus setting、卡片路由或事实源。

这个边界必须在 thread 生命周期 RPC 上成立，而不能只依赖进程配置：Focus adapter
发起的 `thread/start` / `thread/resume` 会覆盖来宾值并显式请求
`approvalsReviewer=user`；只有响应也明确报告 `user` 才是 canonical adapter 路径可接受的
已知成功。native fcodex `thread/start` / `thread/resume` 则在 exact-target 准入后保留上游
TUI payload。上游可以忽略 already-loaded thread 的 reviewer override 并报告既有 reviewer，
因此 proxy 不改写该字段，也不把不匹配当成 transport quarantine 证据。这既不授予 fcodex
reviewer ownership，也不改变 Focus adapter 安全基线。见
[`fcodex-operation-owner.zh-CN.md`](./fcodex-operation-owner.zh-CN.md#直接-thread-target)。

对携带显式 approval/permissions 的 continuation-capable cold resume，Focus 还要求
response 中的 `approvalPolicy` 与 `activePermissionProfile.id` 精确证明请求值已经生效。
这项 postcondition 不能替代 pre-resume override；它用于把忽略 override、缺字段或协议漂移
收口为 unknown outcome，而不是让 persisted goal 按未证明的安全 profile 继续后误报成功。

Connection 初始化只校验 `configRequirements/read` 的 response envelope，以及 Focus 实际依赖的
optional `allowedApprovalsReviewers`：字段缺失/null 表示没有这项限制；非null时必须是字符串数组并
包含 `user`。`allowedApprovalPolicies`、`allowedSandboxModes` 与
`allowedPermissionProfiles` 是 upstream 对具体值的约束，不是“Focus 静态目录中的每个值都必须在
本连接可用”的声明。因此，上述三个非 reviewer allow-list 中任一项为部分或空值，都不会隔离整个
shared backend；Focus 也不把这些字段缓存成跨时间、跨cwd或跨frontend的可用目录。

这意味着 Focus 菜单表达的是本项目支持传递的静态 vocabulary，不保证当前 upstream policy 接受
每一个值。具体 effect 仍由 upstream 处理，可能 fallback、忽略或拒绝；`thread/start` 保存 response
报告的实际 effective 值，不额外增加 exact-match 合同，而携带显式安全 override 的 cold resume 继续
遵守上一段既有 postcondition。若未来需要 UI 只展示实时可用值，必须重新取得 config revision /
invalidation 或 atomic validate-and-effect authority，不能从一次 snapshot 推导。

相关参考：

- [`codex-rs/protocol/src/protocol.rs:L627`](https://github.com/openai/codex/blob/b630ce9a4e754d35a1f33e4366ba638d18626142/codex-rs/protocol/src/protocol.rs#L627)
- [Focus `codex.yaml` 配置示例](../../config/codex.yaml.example)

### 2.2 Sandbox 不是“换了一套工具”

切换 `sandbox` 的核心含义，不是换掉可用工具列表。
更准确地说，它改变的是同一批 shell 命令和工具在执行时要套上的约束。

例如：

- `read-only` 不等于“只剩读命令可用”
- `workspace-write` 不等于“换了一套 shell”
- `danger-full-access` 不等于“突然多出额外工具”

更准确的表述是：

- 模型会收到不同的权限上下文
- 运行时会对命令执行施加不同的 OS 级限制

这就是为什么 sandbox 改变时，用户体感上有时像是“换了工具”，但底层核心工具面其实没有变。

## 3. Upstream Approval 语义

Focus 当前用户可选的 approval 表面包括：

- `untrusted`
  - 只有“已知安全且只读文件”的命令会被自动批准
- `on-request`
  - 由模型决定什么时候请求审批
- `never`
  - 从不请求审批；失败会直接返回
- `on-failure`
  - upstream 已弃用

当前 upstream 还新增了结构化 `granular` policy。Focus 暂不暴露它：当前配置与
前端合同只持久化标量 policy enum，也尚未定义 granular 的 rule/sandbox/skill
审批开关如何与 exact reviewer/action-capability 路由协作。“暂不支持”不表示把 `granular`
静默映射成别的 policy；在合同完成前，本地配置会直接拒绝它。

本仓库已不再在用户可选的飞书表面暴露 `on-failure`。若旧本地配置里仍写了它，
配置层会自动按 `on-request` 归一化处理。

相关上游参考：

- [`codex-rs/protocol/src/protocol.rs:L627`](https://github.com/openai/codex/blob/b630ce9a4e754d35a1f33e4366ba638d18626142/codex-rs/protocol/src/protocol.rs#L627)
- [`codex-rs/core/src/codex.rs:L1648`](https://github.com/openai/codex/blob/b630ce9a4e754d35a1f33e4366ba638d18626142/codex-rs/core/src/codex.rs#L1648)

应避免的写法：

- “untrusted 表示只允许读命令”
- “never 表示命令完全不受限制”

这些说法不对，因为 approval policy 讨论的是升级与审批流程，而不是完整的运行时限制模型。

## 4. Upstream Sandbox 语义

upstream 对平台沙箱的选择是明确的：

- macOS：Seatbelt
- Linux：Linux sandbox helper，默认走 bubblewrap
- Windows：restricted-token sandbox，并提供 elevated pipeline

相关上游参考：

- [`codex-rs/sandboxing/src/manager.rs:L49`](https://github.com/openai/codex/blob/b630ce9a4e754d35a1f33e4366ba638d18626142/codex-rs/sandboxing/src/manager.rs#L49)
- [`codex-rs/linux-sandbox/src/lib.rs:L1`](https://github.com/openai/codex/blob/b630ce9a4e754d35a1f33e4366ba638d18626142/codex-rs/linux-sandbox/src/lib.rs#L1)
- [`codex-rs/core/src/seatbelt.rs:L1`](https://github.com/openai/codex/blob/b630ce9a4e754d35a1f33e4366ba638d18626142/codex-rs/core/src/seatbelt.rs#L1)
- [`codex-rs/features/src/lib.rs:L110`](https://github.com/openai/codex/blob/b630ce9a4e754d35a1f33e4366ba638d18626142/codex-rs/features/src/lib.rs#L110)
- [`codex-rs/windows-sandbox-rs/src/elevated/command_runner_win.rs:L1`](https://github.com/openai/codex/blob/b630ce9a4e754d35a1f33e4366ba638d18626142/codex-rs/windows-sandbox-rs/src/elevated/command_runner_win.rs#L1)
- [`codex-rs/windows-sandbox-rs/src/token.rs:L308`](https://github.com/openai/codex/blob/b630ce9a4e754d35a1f33e4366ba638d18626142/codex-rs/windows-sandbox-rs/src/token.rs#L308)

这也是为什么 Docker 只能算一个很松的类比。
Codex 的主路径并不是切到一份单独 image 或替换 rootfs，而是使用宿主机原生的进程级沙箱机制。

### 4.1 Linux

Linux helper 在代码里直接写明：

- 进程内限制：`no_new_privs` 与 `seccomp`
- 文件系统隔离：bubblewrap

因此，更贴切的理解是“宿主机上的轻量进程沙箱”，而不是“把任务丢进一个完整容器镜像”。

### 4.2 macOS

macOS 路径会生成 Seatbelt policy，并把命令放在 Seatbelt 入口下执行。

### 4.3 Windows

Windows 路径使用 restricted token。除此之外，upstream 还实现了一条带专用 runner 的 elevated sandbox pipeline。

所以“restricted token / elevated runner”不是拍脑袋类比，而是有源码对应的上游实现点。

## 5. Writable Roots 与受保护路径

`workspace-write` 不应该被过度简化为“可以写当前工作目录”。

更准确的说法是：

- 写入允许发生在配置好的 writable roots 内
- 这些可写根下的一些顶层受保护路径，默认仍保持只读

upstream 当前至少会保护：

- `.git`
- `.agents`
- `.codex`

相关上游参考：

- [`codex-rs/protocol/src/permissions.rs:L1098`](https://github.com/openai/codex/blob/b630ce9a4e754d35a1f33e4366ba638d18626142/codex-rs/protocol/src/permissions.rs#L1098)

这个区别很重要，因为它解释了为什么 agent 通常可以修改项目文件，但仍然会被拦在 repo 元数据或 Codex 元数据之外。

## 6. 为什么沙箱有时“看起来像坏了”

Sandbox 相关失败，大致分成两类，而且含义完全不同：

1. 沙箱工作正常，并正确拦截了写入、网络访问或受保护路径
2. 沙箱后端自身在 bootstrap 阶段就失败了

第二类情况下，即便是无害的只读命令，也可能在真正执行目标命令之前就失败。

这时用户很容易误以为：

- 读权限配置错了
- 工具本身没了
- Codex 换了一套命令面

但真正的问题通常出在更前面的 sandbox setup。

在验证本仓库时，本机就复现了这类错误：

```text
bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted
```

这也说明，排障指导应该进入正式文档，而不是只存在于口口相传的经验里。

## 7. 排障参考

upstream CLI 提供了明确的 sandbox 调试子命令：

- `codex sandbox linux`
- `codex sandbox macos`
- `codex sandbox windows`

相关上游参考：

- [`codex-rs/cli/src/main.rs:L252`](https://github.com/openai/codex/blob/b630ce9a4e754d35a1f33e4366ba638d18626142/codex-rs/cli/src/main.rs#L252)

建议的排障顺序：

1. 先区分这是策略拦截，还是 sandbox bootstrap 失败
2. 确认当前平台理论上应走哪条 backend
3. 直接测试对应平台的 sandbox 子命令
4. 如果外层 VM / container 已提供隔离，再判断内层 Codex sandbox 是否仍有价值，还是只是在与宿主环境冲突

## 8. 推荐产品文案

对 FOCUS 这类面向用户的文档，最稳妥的写法是：

- `sandbox` 控制技术执行边界
- `approval_policy` 控制什么时候必须先审批才能继续
- `/permissions` 设置独立的 upstream canonical `permissions` profile id
- `/approval` 单独设置审批策略
- 飞书侧不再暴露 `/sandbox` 用户面

推荐的简明表述：

- “`/permissions` 决定后续 turn 采用哪条权限基线。”
- “`/approval` 决定什么时候必须先停下来等待审批。”
- “权限基线决定执行边界；审批策略决定是否需要停下来等待批准。”

不要在顶层 README 里过度承诺那些未来可能变化的实现细节。
像平台后端、实现路径、排障分层这类内容，更适合放在像本文这样的专门文档里。
