# feishu-codex 技术设计

英文原文：`docs/architecture/feishu-codex-design.md`

另见：

- `docs/contracts/session-profile-semantics.zh-CN.md`
- `docs/architecture/fcodex-shared-backend-runtime.zh-CN.md`
- `docs/decisions/shared-backend-resume-safety.zh-CN.md`
- `docs/archive/codex-handler-decomposition-plan.zh-CN.md`

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
- 允许同一台机器上的同一位本地操作者同时运行多个 Feishu 实例，同时继续共享一套 `CODEX_HOME`

## 3. 非目标

- 不在飞书里重建 Codex TUI 屏幕
- 不依赖未公开的 Codex 磁盘布局来做线程发现或元数据同步
- 第一版不追求覆盖 Codex 的所有实验特性
- 不把 `feishu-cc` 代码复用当作当前架构前提
- 不把裸 `codex` 与 shared-backend `fcodex` 视为同一条运行路径

## 4. 当前设计原则

- 原生协议优先：优先使用 `codex app-server` 行为和 API，而不是本地抓取或重建状态
- 单一事实来源：thread id、cwd、title、preview、source、runtime config 来自 Codex
- 飞书本地状态留在本地：每实例本地默认 profile、线程/UI 绑定状态由 `feishu-codex` 管理
- shared-backend 路径显式存在：如果要和飞书继续同一个 live thread，应明确走同一个**实例 backend**
- `CODEX_HOME` 与 Feishu 运行时边界分离：前者共享，后者按实例隔离
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

- 所有实例共享同一个 `CODEX_HOME`
- 每个实例各自持有：
  - `FC_CONFIG_DIR`
  - `FC_DATA_DIR`
  - service owner
  - control plane
  - managed `codex app-server` backend
- `shared backend` 在当前仓库里表示“实例内共享 backend”，不是“全系统只存在一个 backend”
- 某实例的 backend 默认优先 `ws://127.0.0.1:8765`
- 如果默认端口不可用，该实例 service 会自动切到空闲本地端口，并把当前实际地址写入该实例自己的运行时状态
- `fcodex` 会先选择目标实例，再发现该实例的实际 backend 地址，并附着到同一个实例 backend
- 当 upstream remote 模式需要 cwd 修正时，`fcodex` 会额外加一个很薄的本地 websocket 代理
- 机器级还维护两份全局协调状态：
  - 运行中实例注册表
  - thread live runtime lease

shared backend 与 wrapper 的具体机制，见
`docs/architecture/fcodex-shared-backend-runtime.zh-CN.md`。

### 5.3 核心模块

当前主要模块分工：

- `bot/codex_handler.py`：飞书侧命令处理与线程绑定
- `bot/cards.py`：用户可见卡片渲染
- `bot/card_text_projection.py`：卡片文本投影边界；负责终态 `final_reply_text` 结果载体约定，以及入站 `interactive` 的强合同 / best-effort 文本提取
- `bot/adapters/codex_app_server.py`：Codex adapter 边界
- `bot/codex_protocol/client.py`：`codex app-server` 的 websocket JSON-RPC client
- `bot/fcodex.py` 与 `bot/fcodex_proxy.py`：本地 wrapper 与轻量代理
- `bot/feishu_codexctl.py` 与 `bot/service_control_plane.py`：本地服务管理 CLI 与运行中服务控制面
- `bot/instance_layout.py` 与 `bot/instance_resolution.py`：多实例目录布局、当前/目标实例解析
- `bot/binding_identity.py`：admin-facing binding 标识规范
- `bot/binding_runtime_manager.py`：binding / subscribe / attach / released 与本地 runtime snapshot 的 owner
- `bot/thread_access_policy.py`：线程共享、Feishu 写入 owner、interaction owner 的准入 policy 边界
- `bot/thread_runtime_coordination.py`：跨实例 live runtime lease 获取、自动转移与拒绝
- `bot/turn_execution_coordinator.py`、`bot/execution_output_controller.py`、`bot/execution_recovery_controller.py`：turn / execution 生命周期、执行卡片发布、终态结果载体发送、watchdog / reconcile / degrade 处理
- `bot/runtime_admin_controller.py`：`/status`、`/release-feishu-runtime` 与 control-plane 查询/管理
- `bot/inbound_surface_controller.py`：入站命令面、卡片 action 路由、help 卡片命令复用
- `bot/prompt_turn_entry_controller.py`：prompt 进入、lease 抢占、released -> attached 恢复编排
- `bot/adapter_notification_controller.py`：adapter notification 的 method 路由、语义解释与下游分发
- `bot/interaction_request_controller.py`：审批 / 用户输入这类交互请求的 pending 状态与 fail-close 收口
- `bot/codex_session_ui_domain.py`：session 卡片 UI 流程，包括重命名表单这类瞬时 UI 状态
- `bot/execution_transcript.py`：执行卡片展示层的内部 transcript 组装器；负责 display-only 的 `reply_segments` / `process_log` 片段拼装，并支持在权威终态结果已经单独送达后，把最后一段最终答案从 execution card 的 reply 面板里剔除；它不承担 thread、owner 或 binding 级状态职责
- `bot/stores/thread_admission_store.py`：每实例 Feishu 可见线程的 admission
- `bot/stores/instance_registry_store.py`：机器级运行中实例注册表
- `bot/stores/thread_runtime_lease_store.py`：机器级 thread live runtime lease
- `bot/stores/*.py`：每实例本地默认 profile、shared backend 运行时发现状态、群聊状态

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

并发 ownership 这一轮已经完成了主要收口；当前仍需继续保持清晰、并在后续增量功能中继续收紧的边界是：

- `RuntimeLoop` 已是当前 handler 运行时状态变更的主要串行化原语
- binding 解析与 runtime state 的 hydrate/create 应走单一 resolver 入口，
  不应在多个调用点里继续手写“先挑 binding key，再决定是否建 state”的两段式流程
- `ThreadLeaseRegistry` 这类对象当前应视为 runtime-owned 内部状态，而不是通用线程安全组件
- 线程共享、Feishu 写入 owner、interaction owner 这组准入规则，应集中在单一 policy 边界；
  目前对应为 `ThreadAccessPolicy`，而不是继续散落在 handler / prompt / group 入口里
- `BindingRuntimeManager` 对其他组件应优先暴露 snapshot / inventory / iteration 这类显式读取接口，
  而不是再把整份可变 runtime-state map 直接交给外层持有
- 像 `PromptTurnEntryController` 这类编排组件，对外依赖面应通过显式 ports 装配，
  不应继续扩大匿名 callback 列表
- `CodexHandler._lock` 仍然是一个覆盖面较大的共享状态兜底锁，但长期目标不应是继续围绕它细分锁，而应是减少必须共享、必须一起上锁的状态面

当前这一层拆分已经不只是“把 help/settings/group/session/file 等领域从单体逻辑里抽出去”。历史计划里提出的 ownership 拆分主线，目前已经大体落地：

- `BindingRuntimeManager` 已持有 `binding` / `subscribe` / `attach` / `released` 这一组 Feishu runtime 管理
- `ThreadAccessPolicy` 与 lease store 已持有 Feishu 写入 owner / interaction owner 的准入规则
- `TurnExecutionCoordinator`、`ExecutionOutputController`、`ExecutionRecoveryController`、`InteractionRequestController`、`AdapterNotificationController` 已共同持有 turn / execution / request bridge 这一组生命周期状态机
- `RuntimeAdminController` 已持有 runtime admin / control-plane 查询与管理面
- `InboundSurfaceController` 与 `PromptTurnEntryController` 已把入站 surface 和 prompt 进入编排从总 handler 中拆开

因此，这里原本那句“下一步重点不应是继续把 `CodexHandler` 切成更多文件，而是继续拆状态 ownership”，在当前仓库状态下应理解为一条**已经执行过的架构方向**，而不是仍未开始的 roadmap。

当前仍然保留在 `CodexHandler` 顶层的 ownership，主要是：

- runtime 顶层生命周期：bootstrap / shutdown / service-instance lease / adapter 生命周期
- controller / domain / adapter 的装配，以及跨域 orchestration
- 少量合理保留在总编排层的 helper 与兜底同步面

所以，后续重点已经不是“继续把计划里的 ownership 再拆一次”，而是：

- 继续缩小 `CodexHandler` 作为总编排层必须直接持有的共享状态面
- 避免把新的跨域规则重新堆回顶层 handler
- 让新增功能优先落到已有 owner 边界，而不是重新制造隐式调用顺序约束

历史 rollout 顺序与阶段边界仍保存在
`docs/archive/codex-handler-decomposition-plan.zh-CN.md`，但那份文档现在应被视为归档计划，而不是“当前还未完成的下一步说明”。

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

- 每实例飞书与该实例 `fcodex` 启动共用的本地默认 profile
- 每实例 shared backend 的运行时地址发现状态
- 私聊当前绑定到哪个 thread，以及群聊按 `chat_id` 共享绑定到哪个 thread
- 群聊工作态、群 ACL、群上下文日志与上下文边界状态
- 审批、重命名、卡片等临时 UI 状态
- 每实例 thread admission 集合

另外还有两份机器级共享协调状态：

- 运行中实例注册表
- thread live runtime lease

它们都位于共享的 `FC_GLOBAL_DATA_DIR` 下。
这两份状态不属于任何单个 Feishu chat，也不属于 Codex 线程元数据；
它们只用于本地 CLI 和多实例运行时协调。

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

- `docs/contracts/session-profile-semantics.zh-CN.md` 说明 `/session`、`/resume`、`/profile`、`/rm` 与 wrapper 语义
- `docs/decisions/shared-backend-resume-safety.zh-CN.md` 说明当前 `/resume` 合同与 backend 安全规则

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

群聊已不再埋在本设计文档里定义细则。

当前设计层只保留几条架构边界：

- 群底层会话按 `chat_id` 共享，而不是按群成员拆分
- `assistant` 的主聊天流与群话题分别维护上下文边界，但共享同一个群 backend 会话
- ACL 只决定人类成员“是否有资格”，是否仍需显式 mention 由群工作态决定
- 其他机器人不会直接触发当前机器人；如其消息要进入上下文，依赖历史回捞路径

正式行为合同见：

- `docs/contracts/group-chat-contract.zh-CN.md`
- 手测清单见 `docs/verification/group-chat-manual-test-checklist.zh-CN.md`

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
    instance_layout.py
    instance_resolution.py
    thread_runtime_coordination.py
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
      instance_registry_store.py
      profile_state_store.py
      thread_admission_store.py
      thread_runtime_lease_store.py
  config/
    system.yaml.example
    codex.yaml.example
  docs/
    contracts/
    architecture/
    decisions/
    verification/
    archive/
    doc-index.md
    doc-index.zh-CN.md
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
- 正式功能合同留在 `docs/contracts/`
- 当前架构与实现边界留在 `docs/architecture/`
- 上游调查结论与安全决策留在 `docs/decisions/`
- 手测清单留在 `docs/verification/`
- 已完成 rollout 与历史计划留在 `docs/archive/`

## 8. 演进边界

- 上游 Codex 的 app-server 与 remote 行为仍可能变化，因此 adapter 和 wrapper 的边界要继续保持隔离
- shared-backend wrapper 依赖当前 upstream remote 语义，尤其是 `thread/start`、`cwd`、重连时机这些细节
- `codex exec --json` 仍然适合作为探针、smoke check 和调试手段，但它不是当前主运行时路径
- 后续功能扩展，应继续保持当前的文档分工：语义、运行时、安全模型、设计约束分别说明，避免重新混成一篇大文档
