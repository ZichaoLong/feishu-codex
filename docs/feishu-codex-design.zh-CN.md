# feishu-codex 技术设计

英文原文：`docs/feishu-codex-design.md`

另见：

- `docs/session-profile-semantics.zh-CN.md`
- `docs/fcodex-shared-backend-runtime.zh-CN.md`
- `docs/shared-backend-resume-safety.zh-CN.md`
- `docs/codex-handler-decomposition-plan.zh-CN.md`

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
- 飞书本地状态留在本地：本地默认 profile、线程/UI 绑定状态由 `feishu-codex` 管理
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
   - 私聊按用户维护运行时状态，群聊按 `chat_id` 维护共享运行时状态
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
- `bot/feishu_codexctl.py` 与 `bot/service_control_plane.py`：本地服务管理 CLI 与运行中服务控制面
- `bot/binding_identity.py`：admin-facing binding 标识规范
- `bot/execution_transcript.py`：执行卡片展示层的内部 transcript 组装器；负责 reply/log 片段拼装，不承担 thread、owner 或 binding 级状态职责
- `bot/stores/*.py`：本地默认 profile、shared backend 运行时发现状态、群聊状态

对飞书传输层还应补一条维护性约束：

- `FeishuBot` 这类 transport-boundary 模块，对飞书 SDK 的依赖面应尽量显式
- 不应长期依赖通配符导入来隐含“当前到底用了哪些 IM API 类型”

在 adapter 抽象层上，还有一条需要保持清晰的合同：

- `resume` 的请求输入不应只被抽象成一个 `profile`
- 对 unloaded thread，Feishu 当前已经把 `profile / model / model_provider` 作为恢复提示显式传给 adapter
- 对 loaded thread，这些输入即使被携带，也不表示 live runtime 一定会被改写

因此，adapter 边界必须准确表达“resume 可以接受哪些输入”，而不是把抽象层写成比真实调用面更窄的旧合同。

线程摘要读取也应保持两类合同分离：

- authoritative read：按 `thread_id` 直接向 backend 读取，供真正要落操作的路径使用
- bounded-list best-effort lookup：只从当前全局列表视图里补充上下文或错误提示，不能反过来当作 thread 一定不存在的证明

并发 ownership 也应继续收紧：

- `RuntimeLoop` 已是当前 handler 运行时状态变更的主要串行化原语
- binding 解析与 runtime state 的 hydrate/create 应走单一 resolver 入口，
  不应在多个调用点里继续手写“先挑 binding key，再决定是否建 state”的两段式流程
- `ThreadLeaseRegistry` 这类对象当前应视为 runtime-owned 内部状态，而不是通用线程安全组件
- `CodexHandler._lock` 仍然是一个覆盖面较大的共享状态兜底锁，但长期目标不应是继续围绕它细分锁，而应是减少必须共享、必须一起上锁的状态面

当前这一层拆分已经把 help/settings/group/session/file 等领域边界从单体逻辑里抽出来，但这还不是最终的“真正解耦”。

下一步的重点不应是继续把 `CodexHandler` 切成更多文件，而是继续拆状态 ownership：

- `binding` / `subscribe` / `attach` / `released` 这一组 Feishu runtime 管理
- Feishu 写入 owner 与 interaction owner 的 owner/lease 规则
- turn / execution 生命周期，以及 execution anchor、watchdog、follow-up 发送编排
- service control plane 管理
- adapter notification / request bridge

如果这些状态机继续共居在 `CodexHandler`，那只是把导航从一个大文件变成多个文件，维护时仍要依赖调用顺序记忆隐式约束；这不是我们要的长期架构方向。

继续推进这条 ownership 拆分路线时，推荐的实施顺序与阶段边界见
`docs/codex-handler-decomposition-plan.zh-CN.md`。

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

- 飞书与默认 `fcodex` 启动共用的本地默认 profile
- shared backend 的运行时地址发现状态
- 私聊当前绑定到哪个 thread，以及群聊按 `chat_id` 共享绑定到哪个 thread
- 群聊工作态、群 ACL、群上下文日志与上下文边界状态
- 审批、重命名、卡片等临时 UI 状态

其中，`binding` 默认是跨重启保留的本地 bookmark：

- 它解决的是“飞书会话下次默认继续哪个 thread”
- 它不等于 Feishu 是否仍附着该 thread
- 它也不等于 backend 当前是否仍 loaded

因此：

- `binding` 持久化是正式产品需求
- 显式清空一个或全部 binding 也是合理的本地管理需求
- 这类清理动作应归入 `feishu-codexctl` 的 binding 管理面
- 它不应继续以“单独删除 `chat_bindings.json` 文件”的方式被定义为一个独立架构概念
- 持久化 binding schema 也应 fail-closed：不再为旧半状态做隐式兼容
- 只要 `current_thread_id` 非空，就必须显式写出 `current_thread_runtime_state`
- `current_thread_runtime_state` 只能是 `attached` 或 `released`
- `released` 状态不得携带残留 `write_owner`
- 这类约束若不满足，应直接视为存储损坏并报错，而不是在 load 时静默补成 `attached` 或静默清理

`system.yaml.admin_open_ids` 也遵守单一事实源原则：

- 它是管理员集合的唯一权威源
- 运行中的内存管理员集合只是缓存，不是第二事实源
- `/init <token>` 只是一个受控的便捷写入口，写入的仍是 `system.yaml`
- 手工修改 `system.yaml` 后，不强求热更新；以重启服务或显式 reload 后的权威值为准
- 缓存不得反向刷新权威源，也不得通过“config + runtime 合并”重新把已删除管理员写回配置

### 6.3 Session 与目录语义

精确命令语义不在本文展开，而是交给专门文档：

- `docs/session-profile-semantics.zh-CN.md` 说明 `/session`、`/resume`、`/profile`、`/rm` 与 wrapper 语义
- `docs/shared-backend-resume-safety.zh-CN.md` 说明当前 `/resume` 合同与 backend 安全规则

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

### 6.5 群聊功能合同

以下行为应视为当前实现的正式合同：

#### 默认值

- 新群默认工作态是 `assistant`
- 新群默认 ACL 是 `admin-only`
- 群聊管理员来自 `system.yaml.admin_open_ids`
- `system.yaml.admin_open_ids` 是权威源；运行时管理员集合只是缓存
- 运行时身份判定统一使用 `open_id`；`user_id` 仅保留在日志与 `/whoami` 里做排障展示
- 若希望 `/whoami` 和日志稳定返回 `user_id`，需要额外开 `contact:user.employee_id:readonly`

#### 人类成员权限

- 人类成员是否具备某个群里的触发资格，由该群 ACL 决定
- ACL 只决定“谁有资格”，是否还需要显式 mention 由群工作态决定
- 群 ACL 只管理人类成员，不管理其他机器人
- ACL 策略包括：
  - `admin-only`
  - `allowlist`
  - `all-members`

#### 群聊工作态

- 严格群聊显式 mention 判定依赖 `system.yaml.bot_open_id`
- `/whoareyou` 与 `/init` 中的实时探测只用于诊断和初始化，不会替代运行时读取的 `system.yaml.bot_open_id`
- 如配置 `system.yaml.trigger_open_ids`，命中这些 `open_id` 的 mentions 也视为有效触发
- `trigger_open_ids` 只扩展“哪些 mentions 算触发”，不绕过 ACL，也不替代 `bot_open_id`
- 私聊底层会话按用户隔离；群聊底层会话按 `chat_id` 共享
- `assistant`
  - 接收并缓存群里消息
  - 只有被有效 mention 时才回复
  - 回复时附带自上次触发边界以来的群上下文
  - 主聊天流与每个群话题分别维护上下文边界；主聊天流不会自动读入话题回复，话题也不会自动读入主聊天流
  - 虽然上下文边界按主聊天流 / 话题分开，但底层仍是同一个群共享会话；模型可以记住本群其他讨论里已经明确的结论
- `mention-only`
  - 不缓存群上下文
  - 只有被有效 mention 时才触发
- `all`
  - 人类群消息可直接触发
  - 风险最高，容易刷屏

#### 群命令触发规则

- 私聊命令可直接发送
- 群里的所有 `/` 命令都只给管理员
- 群聊 `assistant` 和 `mention-only` 工作态下，管理员群命令本身也必须先显式 mention 触发对象
- 群聊 `all` 工作态下，管理员可直接发送群命令
- 群命令不会写入 `assistant` 上下文日志，也不会推进上下文边界

#### 助理模式上下文

- `assistant` 会把群消息写入本地日志
- 只有人类成员的有效触发 mention 会真正触发回复
- 由于飞书不会把其他机器人发言实时推给机器人，`assistant` 会在每次有效触发时按配置回捞最近历史消息
- 历史回捞与实时日志会合并成同一份上下文，而不是两套独立逻辑
- 下一次有效触发时，上下文由两部分组成：
  - 本地实时日志中，上次边界之后到本次触发之前的消息
  - 飞书历史接口返回、但本地日志里尚未出现的缺失消息
- 主聊天流（`chat` 容器）的历史回捞受 `group_history_fetch_limit` 和 `group_history_fetch_lookback_seconds` 限制
- 主聊天流在边界时间附近会向前留一个很小的冗余秒级窗口，再用边界 `message_id` 去重，避免时间窗卡边时漏消息
- `group_history_fetch_limit` 和 `group_history_fetch_lookback_seconds` 同时也是“是否启用任何历史回捞”的总开关；任一项为 `0` 都会关闭主聊天流和话题回捞
- 话题内（`thread` 容器）的历史回捞当前不承诺严格受 `group_history_fetch_lookback_seconds` 限制；因为飞书公开接口对 `thread` 容器不支持 `start_time/end_time`，当前实现只保证受上下文边界和 `group_history_fetch_limit` 约束
- 话题内优先按 `ByCreateTimeDesc` 倒序回捞，并在到达边界后尽早停止；只有在该排序方式不可用时才回退到升序扫描
- 当时间窗内缺失消息数量超过 `group_history_fetch_limit` 时，当前实现保留“最近的缺失消息”，而不是最早的一批
- 上下文边界同时记录：
  - 本地日志序号 `seq`
  - 边界时间戳 `created_at`
  - 边界时间戳下已消费的 `message_id` 集合
- 记录边界 `message_id` 集合的目的，是避免下一次有效触发时把“与上次边界同毫秒但尚未消费”的缺失消息误判为旧消息而漏掉
- 当前实现保证“不漏掉同毫秒未消费消息”和“不重复同毫秒已消费消息”，但不承诺把不同来源、同毫秒消息恢复成绝对全序
- 如果本次有效触发发生在群话题内，执行卡片、ACL 拒绝和过长文本 follow-up 会尽量留在原话题，而不是回到主聊天流

#### ACL 拒绝反馈

- 未获授权成员在 `assistant` / `mention-only` 中显式 mention 触发对象时，会收到拒绝提示
- 未获授权成员在 `all` 中直接发普通消息会静默忽略，以避免刷屏
- 未获授权成员在 `all` 中显式 mention 触发对象或发群命令时，仍会收到拒绝提示

#### 其他机器人与历史消息

- 其他机器人不会直接触发 `feishu-codex`
- 如果群消息历史对机器人可见，其他机器人消息可以通过每次有效触发时的历史回捞进入上下文
- 如果关闭历史回捞，其他机器人消息不会自动进入 `assistant` 上下文

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
    feishu_codexctl.py
    service_control_plane.py
    binding_identity.py
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
