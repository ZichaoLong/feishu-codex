# feishu-codex 技术设计

英文原文：`docs/feishu-codex-design.md`

另见：

- `docs/session-profile-semantics.zh-CN.md`
- `docs/fcodex-shared-backend-runtime.zh-CN.md`
- `docs/shared-backend-resume-safety.zh-CN.md`

## 1. 背景

`feishu-codex` 是一个独立的、面向 Codex 的项目，不是旧 Claude 集成的简单改名版本。

历史背景仍然重要：

- [`feishu-cc`](https://github.com/ZichaoLong/feishu-cc) 验证了“飞书消息 + 卡片 + 审批 + 会话管理”这条交互路径是有价值的
- 但它依赖 Claude 特有的本地文件格式和 hook 行为
- `feishu-codex` 保留飞书侧交互经验，同时把 agent/runtime 集成层切换到 Codex 原生能力

上游基线：

- Codex 源码仓库：[`openai/codex`](https://github.com/openai/codex.git)
- 当前本地验证基线：`codex-cli 0.118.0`（2026-04-03）

本项目的当前设计，建立在这些 Codex 能力之上：

- `codex app-server` 作为主要的应用侧运行时接口
- `codex exec --json` 作为结构化探针 / 调试辅助
- `codex exec resume` 以及 thread-oriented 的 CLI / app-server 路径，用于会话连续性

## 2. 目标

- 提供一个面向 Codex 的 Feishu bridge，覆盖 prompt、流式输出、审批和长生命周期线程管理
- 让 Codex 线程元数据继续以 Codex 自身为单一事实来源
- 尽量减少对私有磁盘格式或 shell hook 行为的依赖
- 让飞书层、本地 wrapper 层、Codex 协议层保持清晰分离
- 为“飞书与本地继续同一个 live thread”保留一条低认知负担的 shared-backend 路径

## 3. 非目标

- 不在飞书里重建 Codex TUI 屏幕
- 不依赖未公开的 Codex 磁盘布局来做线程发现或元数据同步
- 第一版不追求覆盖 Codex 的所有实验特性
- 不把 `feishu-cc` 代码复用当作当前架构前提
- 不把裸 `codex` 与 shared-backend `fcodex` 视为同一条运行路径

## 4. 当前设计原则

- 原生协议优先：优先使用 `codex app-server` 行为和 API，而不是本地抓取或重建状态
- 单一事实来源：thread id、cwd、title、preview、source、runtime config 来自 Codex
- 飞书本地状态留在本地：favorites、本地默认 profile、UI 绑定状态由 `feishu-codex` 管理
- shared-backend 路径显式存在：如果要和飞书继续同一个 live thread，应明确走同一个 backend
- 运行时假设要文档化：wrapper 与 shared-backend 行为不能只隐含在代码里

## 5. 当前架构

### 5.1 分层

`feishu-codex` 当前可分成四层：

1. 飞书传输层
   - 接收用户消息与卡片动作
   - 发送文本、卡片与 patch 更新
2. 应用层
   - 命令路由
   - 按用户 / 按会话维护运行时状态
   - 卡片渲染
   - `/session` 与 `/resume` 协调
3. Codex adapter / protocol 层
   - 持有 Codex 运行时连接
   - 将 handler 的意图翻译成 Codex 请求
   - 归一化 Codex 的通知与响应
4. 本地状态层
   - 存储飞书独有元数据与运行时发现状态
   - 不替代 Codex 的线程元数据

### 5.2 运行时拓扑

当前运行时行为：

- `feishu-codex` service 默认走 managed app-server 路径
- service 会启动本地 `codex app-server` 子进程，并通过 websocket 与之通信
- shared backend 默认优先 `ws://127.0.0.1:8765`
- 如果默认端口不可用，service 会自动切到空闲本地端口，并把当前实际地址写入本地运行时状态
- `fcodex` 与其它 remote-style 路径会发现这个实际地址，并附着到同一个 shared backend
- 当 upstream remote 模式需要 cwd 修正时，`fcodex` 会额外加一个很薄的本地 websocket 代理

shared backend 与 wrapper 的具体机制，见
`docs/fcodex-shared-backend-runtime.zh-CN.md`。

### 5.3 核心模块

当前主要模块分工：

- `bot/codex_handler.py`：飞书侧命令处理与线程绑定
- `bot/cards.py`：用户可见卡片渲染
- `bot/adapters/codex_app_server.py`：Codex adapter 边界
- `bot/codex_protocol/client.py`：`codex app-server` 的 websocket JSON-RPC client
- `bot/fcodex.py` 与 `bot/fcodex_proxy.py`：本地 wrapper 与轻量代理
- `bot/stores/*.py`：favorites、本地默认 profile、shared backend 运行时发现状态

## 6. 数据与行为边界

### 6.1 Codex 持有的数据

以下信息继续由 Codex 负责：

- thread id
- cwd
- 线程标题
- preview 文本
- source kind 与 status
- thread timestamps
- runtime config 与 model/provider 状态

### 6.2 Feishu 本地数据

`feishu-codex` 只保存飞书或集成侧专属的数据：

- favorites / starred 状态
- 飞书与默认 `fcodex` 启动共用的本地默认 profile
- shared backend 的运行时地址发现状态
- 每个飞书会话当前绑定到哪个 thread
- 审批、重命名、卡片等临时 UI 状态

### 6.3 Session 与目录语义

精确命令语义不在本文展开，而是交给专门文档：

- `docs/session-profile-semantics.zh-CN.md` 说明 `/session`、`/resume`、`/profile`、`/rm` 与 wrapper 语义
- `docs/shared-backend-resume-safety.zh-CN.md` 说明受保护的 `/resume` 与 backend 安全规则

本文只固定这些边界：

- 线程元数据来自 Codex
- 飞书聊天状态决定当前工作上下文
- shared-backend 继续路径必须显式，而不是隐式假设

### 6.4 审批模型

当前实现使用 Codex 原生审批与沙箱概念：

- app-server 的审批请求 / 响应
- Codex 的 approval policy 与 sandbox policy 字段
- 在这些原语之上，再叠加飞书侧用户友好的权限预设

整个集成不依赖 Claude 式 shell hook 拦截。

## 7. 当前仓库结构

当前仓库布局是：

```text
feishu-codex/
  bot/
    __main__.py
    standalone.py
    feishu_bot.py
    handler.py
    cards.py
    codex_handler.py
    fcodex.py
    fcodex_proxy.py
    config.py
    constants.py
    profile_resolution.py
    session_resolution.py
    adapters/
      base.py
      codex_app_server.py
    codex_protocol/
      client.py
    stores/
      app_server_runtime_store.py
      favorites_store.py
      profile_state_store.py
  config/
    system.yaml.example
    codex.yaml.example
  docs/
    *.md
    *.zh-CN.md
  tests/
    test_codex_app_server.py
    test_codex_handler.py
  install.sh
  pyproject.toml
  README.md
```

这套结构已经能支撑当前架构边界：

- 飞书传输与 handler 逻辑留在 `bot/`
- Codex 集成边界留在 `bot/adapters/` 与 `bot/codex_protocol/`
- 本地持久化状态留在 `bot/stores/`
- 语义、运行时、安全模型与设计约束留在 `docs/`

## 8. 演进边界

- 上游 Codex 的 app-server 与 remote 行为仍可能变化，因此 adapter 和 wrapper 的边界要继续保持隔离
- shared-backend wrapper 依赖当前 upstream remote 语义，尤其是 `thread/start`、`cwd`、重连时机这些细节
- `codex exec --json` 仍然适合作为探针、smoke check 和调试手段，但它不是当前主运行时路径
- 后续功能扩展，应继续保持当前的文档分工：语义、运行时、安全模型、设计约束分别说明，避免重新混成一篇大文档
