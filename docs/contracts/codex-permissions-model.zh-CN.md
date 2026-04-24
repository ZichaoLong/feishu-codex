# Codex 权限模型

英文原文：`docs/contracts/codex-permissions-model.md`

本文记录 `feishu-codex` 暴露出来的 `approval_policy`、`sandbox` 和 `permissions` 预设背后的 upstream 语义。

写这篇文档有两个目的：

- 让飞书侧文案始终与 upstream Codex 行为保持一致
- 把精简用户帮助与实现细节、排障细节分开

上游基线：

- Codex 源码仓库：[`openai/codex`](https://github.com/openai/codex.git)
- 当前本地验证基线：`codex-cli 0.118.0`（2026-04-03）
- 文中后续的文件 / 行号引用，是基于这套基线在本地核对过的实现线索，适合做定位参考，但不应视为永久稳定的 permalink

## 1. 三层概念

`feishu-codex` 暴露了三个彼此相关的概念：

1. `approval_policy`
- 运行在什么时机需要停下来等待审批

2. `sandbox`
- 命令最终在什么文件系统与网络限制下执行

3. `permissions`
- 飞书侧的一个预设，它会同时修改前两者

重要点在于：`permissions` 不是 upstream 原生概念。
它是产品层对两个 upstream 旋钮的便捷封装：

- `approval_policy`
- `sandbox`

## 2. Approval 与 Sandbox

最简洁的心智模型是：

- `sandbox` 是技术执行边界
- `approval_policy` 是审批边界

这个模型总体是对的，但还需要几处精度修正。

### 2.1 在 upstream 里，approval 不必然等于“人工审批”

upstream Codex 把 approval 建模为“策略 + reviewer 流程”，并不严格等同于“必须由一个人点击批准”。

不过在本仓库当前产品合同里，默认 reviewer 仍然是飞书用户，因此把 `approval_policy` 解释为审批边界，对当前产品语义仍然是准确的。

相关上游参考：

- [`protocol.rs:627`](/home/zlong/llm/codex/codex-rs/protocol/src/protocol.rs)
- [`codex.yaml.example:35`](/home/zlong/llm/feishu-codex/config/codex.yaml.example)

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

当前 upstream `AskForApproval` 包括：

- `untrusted`
  - 只有“已知安全且只读文件”的命令会被自动批准
- `on-request`
  - 由模型决定什么时候请求审批
- `never`
  - 从不请求审批；失败会直接返回
- `on-failure`
  - upstream 已弃用

本仓库已不再在用户可选的飞书表面暴露 `on-failure`。若旧本地配置里仍写了它，
配置层会自动按 `on-request` 归一化处理。

相关上游参考：

- [`protocol.rs:627`](/home/zlong/llm/codex/codex-rs/protocol/src/protocol.rs)
- [`codex.rs:1648`](/home/zlong/llm/codex/codex-rs/core/src/codex.rs)

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

- [`manager.rs:49`](/home/zlong/llm/codex/codex-rs/sandboxing/src/manager.rs)
- [`linux-sandbox/lib.rs:1`](/home/zlong/llm/codex/codex-rs/linux-sandbox/src/lib.rs)
- [`seatbelt.rs:1`](/home/zlong/llm/codex/codex-rs/core/src/seatbelt.rs)
- [`features/lib.rs:110`](/home/zlong/llm/codex/codex-rs/features/src/lib.rs)
- [`command_runner_win.rs:1`](/home/zlong/llm/codex/codex-rs/windows-sandbox-rs/src/elevated/command_runner_win.rs)
- [`token.rs:307`](/home/zlong/llm/codex/codex-rs/windows-sandbox-rs/src/token.rs)

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

- [`permissions.rs:1098`](/home/zlong/llm/codex/codex-rs/protocol/src/permissions.rs)

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

- [`main.rs:252`](/home/zlong/llm/codex/codex-rs/cli/src/main.rs)

建议的排障顺序：

1. 先区分这是策略拦截，还是 sandbox bootstrap 失败
2. 确认当前平台理论上应走哪条 backend
3. 直接测试对应平台的 sandbox 子命令
4. 如果外层 VM / container 已提供隔离，再判断内层 Codex sandbox 是否仍有价值，还是只是在与宿主环境冲突

## 8. 推荐产品文案

对 `feishu-codex` 这类面向用户的文档，最稳妥的写法是：

- `sandbox` 控制技术执行边界
- `approval_policy` 控制什么时候必须先审批才能继续
- `permissions` 是同时调整两者的预设

推荐的简明表述：

- “`sandbox` 决定命令运行时的文件系统和网络边界。”
- “`approval_policy` 决定什么时候必须先停下来等待审批。”

不要在顶层 README 里过度承诺那些未来可能变化的实现细节。
像平台后端、实现路径、排障分层这类内容，更适合放在像本文这样的专门文档里。
