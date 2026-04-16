# `fcodex` Shared Backend 运行时模型

英文原文：`docs/fcodex-shared-backend-runtime.md`

本文是 `feishu-codex` 当前 shared-backend / wrapper 运行时模型的实现说明。
如果你想知道 `fcodex`、shared backend、动态端口、cwd 代理这些机制为什么存在，应优先看本文。

本文解释下列能力背后的实现模型：

- `fcodex --cd`
- 本地 websocket 代理
- `feishu-codex` 使用的 shared Codex remote app-server

另见：

- `docs/session-profile-semantics.zh-CN.md`
- `docs/shared-backend-resume-safety.zh-CN.md`
- `docs/feishu-codex-design.zh-CN.md`

## 1. 上游基线

- 上游项目：[`openai/codex`](https://github.com/openai/codex.git)
- 当前本地验证基线：`codex-cli 0.118.0`（2026-04-03）
- 本文描述的是当前 `feishu-codex` 基于 stock Codex CLI / `codex app-server` / `--remote` 行为验证出的运行时模型；如果上游版本后续调整 remote 协议或 app-server 行为，本文也应随之更新。

## 2. 运行时组成

在稳定状态下，本地 / 共享路径如下：

```text
Feishu client
  -> feishu-codex service
     -> shared codex app-server
        （默认优先 ws://127.0.0.1:8765；冲突时自动切到空闲本地端口）

fcodex shell wrapper
  -> local thin proxy
     -> shared codex app-server
        -> upstream Codex TUI
```

关键点在于：飞书和 `fcodex` 预期应连接到同一个 live app-server backend。

## 3. 为什么需要 `fcodex`

裸 `codex` 通常自己管理 backend 生命周期。对于普通本地使用这没有问题，但当你希望飞书与本地 TUI 操作同一个 live thread 时，这不是合适的默认行为。

`fcodex` 存在的目的，是提供：

- 与飞书共享的单一 backend
- 由 `feishu-codex` 持有的本地默认 profile
- `/session`、`/resume <name>` 这类 wrapper 命令
- 一个用于修正 remote 模式工作目录行为的兼容层

## 4. 安装后的 Wrapper 环境

安装后的 `fcodex` wrapper 在启动 Python 入口前，会做三件重要的事：

1. 如果存在，则加载 `~/.config/environment.d/90-codex.conf`
2. 设置 `FC_CONFIG_DIR`
3. 设置 `FC_DATA_DIR`

这意味着 service 进程与本地 wrapper 可以共享：

- 同一份配置目录
- 同一份本地 profile 状态文件
- 同一份辅助本地状态

这里的“辅助本地状态”也包括 shared app-server 的运行时地址发现信息：当默认 `ws://127.0.0.1:8765` 被占用、服务自动切到其它空闲端口时，`fcodex` 会据此找到当前实际 backend 地址。

## 5. `--cd` 的真实工作方式

`fcodex` 每次启动时会解析出一个最终生效的工作目录：

- 如果用户传了 `--cd` 或 `-C`，就用它
- 否则使用当前 shell cwd
- 如果用户显式传了 `--cd` / `-C` 但缺少值，wrapper 应直接报错，而不是静默回退到当前 cwd

然后它会对这个值做两件彼此独立的事：

1. 把 `--cd` 继续透传给 upstream `codex`
2. 把同一个 cwd 传给本地代理

这种“双重处理”是有意为之。

## 6. 为什么需要本地代理

最初的问题是：

- 在 remote 模式下，upstream Codex TUI 不一定会稳定地在 `thread/start` 上发送 `cwd`
- shared app-server 于是会回退到它自己进程的工作目录
- 对 `feishu-codex` 而言，这个回退目录通常是 `~/.local/share/feishu-codex`

结果就是：

- 直接运行 `fcodex` 新开线程时，工作目录可能会错误落到 service data 目录，而不是调用者当前 shell 所在目录

本地代理正是为了解决这个非常具体的缺口：

- 它把 websocket 流量转发到 shared backend
- 当它看到 `thread/start` 且 `params.cwd` 缺失或为空时，会注入 wrapper 选定的最终 cwd
- 其它流量原样透传

这样可以把补丁面控制得非常窄。

## 7. 为什么代理生命周期跟随父进程

排查中我们确认，upstream 的 remote resume 并不是单连接流程。

`codex --remote ... resume <id>` 可能会：

1. 先连接一次，用于会话查找或启动准备
2. 断开
3. 再次连接，进入真正的 TUI 会话

因此，代理不能在第一个 websocket client 断开后就安全退出。

当前模型：

- 当由 `fcodex` 启动时，代理会拿到 wrapper 进程 PID
- 代理会一直存活，直到这个父进程退出
- 在测试中如果没有父 PID，它仍可退化为短空闲超时模式

这就是当前实现能稳住 resume 期间重连的原因。

## 8. 哪些路径使用 Shared Backend

默认情况下，下列入口都走 shared backend：

- 飞书命令
- 直接运行 `fcodex`
- `fcodex <prompt>`
- `fcodex resume <thread_id>`
- `fcodex /resume <name>` 在 wrapper 侧解析完成之后

像 `fcodex /session` 这样的 wrapper 命令虽然不会启动 TUI，但它们查询的仍然是同一个 backend 和同一份线程元数据。

## 9. 显式 `--remote` 是特例

如果用户显式给 `fcodex` 传了 `--remote`，wrapper 就不会再强行走 shared-backend 路径。

这意味着：

- 不会插入本地 cwd 修正代理
- 不再隐含 shared-backend 保证
- 用户是在明确选择一个自定义 remote 目标

这是有意设计的。显式 `--remote` 的语义就是“使用我指定的目标”。

## 10. 与裸 `codex` 的区别

相较于裸 Codex TUI，`fcodex` 增加了这些语义：

- 默认与飞书共享 backend
- 当缺少 `-p/--profile` 时注入本地默认 profile
- wrapper 命令：
  - `/help`
  - `/profile`
  - `/rm`
  - `/session`
  - `/resume`
- 通过一个轻量本地代理修补 cwd

但一旦进入运行中的 TUI，命令语义就回到 upstream Codex 的默认行为。

## 11. 已知注意事项

### Upstream remote 协议未来可能变化

cwd 代理之所以存在，是因为当前 upstream remote 模式的行为如此。如果 upstream 后续修改了：

- `thread/start` 的 payload 形状
- remote 会话启动顺序
- 重连时机

wrapper 可能需要跟着调整。相关上游实现与变更历史，应以 [`openai/codex`](https://github.com/openai/codex.git) 为准。

### 裸 `codex` 仍不在共享线程契约内

如果用户在飞书或 `fcodex` 正在写某个线程时，又用裸 `codex` 配合它自己的 backend 打开同一个线程，`feishu-codex` 无法把这件事变安全。

### TUI 内的发现逻辑仍是 upstream 的

在 TUI 里，`/resume` 的 picker 行为仍然由 upstream 决定，它可能不同于：

- 飞书 `/session`
- `fcodex /session`
- `fcodex /resume <name>`

### Shared backend 可用性是前提

如果 shared app-server 没有运行，或者不可达，`fcodex` 就无法完成它的职责。这时启动会快速失败，而不是悄悄退回一个隔离的本地 backend。

## 12. 开发者入口

相关实现文件：

- wrapper 参数处理与 shared-backend 启动：
  - `bot/fcodex.py`
- 代理传输与 cwd 注入：
  - `bot/fcodex_proxy.py`
- 飞书侧 adapter / handler：
  - `bot/codex_handler.py`
  - `bot/adapters/codex_app_server.py`
- shared discovery 逻辑：
  - `bot/session_resolution.py`
