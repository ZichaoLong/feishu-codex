# feishu-codex 技术设计

英文原文：`docs/feishu-codex-design.md`

另见：

- `docs/shared-backend-resume-safety.zh-CN.md`，其中描述了 backend 共享与带保护的 `/resume` 模型。

## 1. 背景

`feishu-cc` 之所以能工作，是因为它把 Claude Code CLI 包了一层，并通过一套本地约定填补了一些缺口：

- 通过扫描 `~/.claude/projects/*.jsonl` 发现 session
- 通过读写 Claude session JSONL 同步标题
- 通过 `PreToolUse` hook + 本地 HTTP server 拦截权限
- 把 Claude hook 的决策再翻译回 CLI 可见输出，以实现特殊交互流程

这种做法与 Claude Code 的内部机制耦合得很深。它虽然能用，但维护成本很高，原因在于：

- session 元数据来自私有的磁盘格式
- 权限处理依赖 shell hook 行为
- 某些 UX 流程是“重建”出来的，而不是基于原生远程协议

对 Codex 来说，本地调研表明有一条更好的路径。

已在本地于 2026-03-31 验证：

- `codex exec --json` 会输出结构化 JSONL 事件，例如 `thread.started`、`turn.started`、`item.completed`、`turn.completed`
- `codex exec resume` 支持以 id 或 thread name 继续非交互会话
- `codex app-server` 暴露出应用层协议，其中包括：
  - `thread/start`
  - `thread/resume`
  - `thread/list`
  - `thread/read`
  - `thread/name/set`
  - `turn/start`
  - `turn/interrupt`
  - 权限审批请求 / 响应对象

这意味着 `feishu-codex` 不应该只是一个把 `feishu-cc` 中二进制字符串替换掉的克隆品。
它应该是一个新的、以 adapter 为核心的设计：保留飞书这一层，替换 agent 集成层。

## 2. 目标

- 构建一个 `feishu-codex` 服务，提供与 `feishu-cc` 相同的核心用户价值：
  - 从飞书发 prompt
  - 把进度流式回传到飞书卡片
  - 管理长生命周期 session
  - 从当前目录恢复 session
  - 重命名 session
  - 中断活跃工作
  - 把审批路由到飞书
- 让 session 元数据只以 Codex 自身为单一事实来源。
- 避免解析 Codex 私有磁盘文件。
- 既然 Codex 协议已原生提供审批能力，就避免再做基于 shell hook 的权限拦截。
- 让实现比 `feishu-cc` 更易维护。

## 3. 非目标

- 不在飞书里模拟 Codex TUI 的屏幕渲染。
- 不依赖未公开文档的内部文件布局来发现或命名线程。
- v1 不尝试支持 Codex 的每一个实验特性。
- 第一版不构建通用多 agent bridge。

## 4. 设计原则

- 原生协议优先：优先使用 `codex app-server` API，而不是 CLI 抓取或磁盘扫描。
- 单一事实来源：thread id、cwd、title、preview 来自 Codex 协议，而不是本地缓存。
- 飞书特有状态留在本地：只存储 Codex 不拥有的数据，例如用户自己的收藏。
- 保持 transport 与 agent runtime 解耦，以便飞书层可以复用。
- 让 fallback 路径显式存在：`codex exec --json` 是验证与紧急降级路径，不是主架构。

## 5. 推荐架构

### 5.1 高层布局

`feishu-codex` 应拆成 4 层：

1. 飞书传输层
- 接收用户消息与卡片动作
- 发送文本 / 卡片 / patch 更新

2. 应用层
- 命令路由
- 按用户 / 按会话的状态
- 卡片渲染
- session 列表排序与匹配

3. Codex adapter 层
- 持有 Codex 运行时连接
- 将应用意图翻译成 Codex 协议请求
- 将 Codex 通知翻译成归一化事件

4. 持久化层
- 本地存储飞书独有元数据
- 不本地缓存 Codex thread 的标题 / cwd / preview

### 5.2 进程拓扑

使用一个长期运行的本地 `codex app-server` 子进程，通过 `stdio://` 通信。

原因：

- 比 remote websocket 鉴权更简单
- 部署形态更接近 `feishu-cc`
- 无需额外暴露网络端口
- 可以让单个 service 进程持有一条持久的 Codex 协议会话

未来扩展：

- 允许通过 websocket 连接远端 `codex app-server`
- 但保持相同的 adapter 接口

### 5.3 为什么 app-server 是主路径

app-server 协议已经暴露了我们需要的生命周期与审批原语。

与 `codex exec --json` 相比：

- session 控制更好
- 原生支持线程列表与读取
- 原生 rename API
- 原生 interrupt API
- 原生审批请求与响应
- 不需要只靠 stdout 去推断行为

`codex exec --json` 应保留为：

- smoke-test 工具
- 集成探针
- app-server 不可用时的窄环境 fallback

## 6. 核心抽象

### 6.1 AgentAdapter

引入一个 adapter 接口，而不是把 CLI 语义直接写死在 handler 里。

建议形态：

```python
class AgentAdapter(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...

    def create_thread(self, *, cwd: str, model: str | None, settings: TurnSettings) -> ThreadRef: ...
    def resume_thread(self, thread_id: str) -> ThreadRef: ...
    def list_threads(self, query: ThreadQuery) -> ThreadPage: ...
    def read_thread(self, thread_id: str, include_turns: bool = False) -> ThreadData: ...
    def rename_thread(self, thread_id: str, name: str) -> None: ...

    def start_turn(self, thread_id: str, input_items: list[InputItem], settings: TurnSettings) -> TurnRef: ...
    def interrupt_turn(self, thread_id: str, turn_id: str | None = None) -> None: ...

    def approve_permissions(self, request_id: str, decision: PermissionDecision) -> None: ...
    def approve_exec(self, request_id: str, decision: ExecDecision) -> None: ...
```

### 6.2 归一化领域对象

应用层不应依赖原始协议 JSON。

应把这些对象归一化：

- `ThreadSummary`
  - `id`
  - `cwd`
  - `name`
  - `preview`
  - `created_at`
  - `updated_at`
  - `source_kind`
  - `status`
- `TurnEvent`
  - `thread_id`
  - `turn_id`
  - `phase`
  - `text_delta`
  - `tool_call`
  - `command_execution`
  - `diff`
  - `completed`
  - `error`
- `PermissionRequest`
  - `request_id`
  - `thread_id`
  - `turn_id`
  - `kind`
  - `filesystem_read`
  - `filesystem_write`
  - `network_enabled`
  - `reason`

## 7. Session 与 Thread 模型

### 7.1 单一事实来源

以下信息由 Codex 持有：

- thread id
- cwd
- thread name
- preview text
- source kind
- timestamps

飞书本地存储只持有：

- favorites / starred 标记
- 每个聊天会话当前绑定到哪个 thread
- 短暂 UI 状态

这样就能避免 `feishu-cc` 当年必须清理的 `feishu_sessions.json.title` 歧义问题。

### 7.2 每个聊天会话的运行时状态

对每个 `(user_id, chat_id)`，维护：

- `current_thread_id`
- `current_cwd`
- `current_turn_id`
- `running`
- `session_model`
- `approval_mode`
- `message_queue`
- pending approval / form state

### 7.3 `/new`

行为：

- 通过 `thread/start` 创建新 Codex 线程
- 将当前飞书会话绑定到新的 thread id
- 保留当前 `cwd`、`model` 与审批设置

### 7.4 `/session`

行为：

- 调用 `thread/list`
- 按 `cwd = current_cwd` 过滤
- 显式传入 `source kinds`，而不是依赖协议默认值

要求的 source kinds：

- `cli`
- `appServer`
- 如果允许 fallback 创建线程，还应包含 `exec`

原因：

- 协议默认的 source 过滤不够
- 否则本地 CLI 线程和飞书创建的线程，可能像活在两个平行宇宙里一样互相看不见

展示策略：

- 收藏优先
- 之后展示最近更新的非收藏
- 如果 Codex 设置了 `name`，就显示 `name`
- 否则显示 Codex 的 `preview`
- 不允许使用本地缓存标题作为兜底

### 7.5 `/resume <arg>`

匹配顺序：

1. 精确 thread id
2. 唯一 thread id 前缀
3. 精确 thread name

候选范围：

- 与 `/session` 相同的允许 source kinds
- 卡片流默认优先当前 cwd
- 显式 `/resume` 时可配置更宽范围搜索

实现：

- 使用 `thread/list` / `thread/read`
- 如果线程当前未被 app-server 加载，则调用 `thread/resume`
- 然后把飞书会话绑定到该 thread id

### 7.6 `/rename`

使用原生 `thread/name/set`。

相较于 `feishu-cc`，这是一个显著改进：

- 不再 patch JSONL
- 不再有本地 title override
- 本地 Codex CLI 与飞书间的标题一致性由系统原生保证

### 7.7 `/cd`

对 v1 的推荐行为：

- 把当前 `cwd` 保持为飞书聊天级默认值
- 修改 cwd 时清空当前线程绑定，并为下一条用户消息准备一个新线程

v1 中不要试图跨目录迁移已有线程。

原因：

- session 浏览语义保持简单
- `thread/list(cwd=...)` 仍然有稳定含义
- 这与当前 `feishu-cc` 的心智模型一致

### 7.8 `/rm`

行为：

- 调用原生 `thread/archive`
- 不对持久化 rollout 做硬删除
- 通过把 rollout JSONL 从 `sessions/` 移到 `archived_sessions/` 来保留数据
- 依赖 Codex 的持久化元数据把线程标记为 archived

列表语义：

- 默认的 `thread/list` 应排除 archived 线程
- 只有在显式请求 archived 过滤时，归档线程才应出现

产品边界：

- v1 不需要立刻暴露 `thread/unarchive`
- 但必须把 `/rm` 明确写成“可恢复的归档语义”，而不是“破坏性删除”

## 8. 消息与流式输出模型

### 8.1 Turn 生命周期

针对当前线程的新用户输入，使用 `turn/start`。

`TurnStartParams` 已支持：

- `threadId`
- `input`
- `cwd`
- `model`
- `approvalPolicy`
- `sandboxPolicy`

它比手工拼 shell 命令更合适。

### 8.2 飞书流式卡片

每个 turn 维护一张 active Feishu execution card：

- assistant 文本增量更新 reply 区域
- command execution item 渲染为 tool / bash 进度
- diff item 渲染为简洁摘要
- turn 完成后封板

卡片更新路径应基于 adapter 输出的归一化事件，而不是原始 Codex JSON。

### 8.3 Interrupt

使用原生 `turn/interrupt`。

不要把 kill 进程当成第一层的 interrupt 机制。

只有在以下情况下，进程 kill 才作为最后的恢复手段：

- app-server 子进程卡死
- 协议连接丢失

## 9. 审批模型

Codex 的审批模型实质上优于 Claude 的 hook 拦截模型。

### 9.1 原生权限审批

已验证的协议对象：

- `PermissionsRequestApprovalParams`
  - `threadId`
  - `turnId`
  - `itemId`
  - 请求的文件系统与网络权限
- `PermissionsRequestApprovalResponse`
  - 授予后的 permissions profile
  - scope：`turn` 或 `session`

它与飞书按钮的映射非常自然：

- 允许一次
- 本 session 允许
- 拒绝

### 9.2 原生命令执行审批

已验证的协议对象：

- `ExecCommandApprovalResponse`
  - `approved`
  - `approved_for_session`
  - `denied`
  - `abort`
  - 协议特有的 policy amendment 变体

推荐飞书映射：

- 允许一次
- 本 session 允许
- 拒绝但继续
- 拒绝并停止当前 turn

这个协议比今天的 `feishu-cc` 更丰富。v1 不需要在 UI 中暴露所有高级 policy amendment 路径。

### 9.3 审批模式模型

不要原样照搬 Claude 特有的权限模式。

在 adapter 里使用 Codex 原生概念，再映射为飞书友好的 UI：

- `interactive`
  - 所有协议审批请求都透出到飞书
- `session_relaxed`
  - 可在卡片中授予 session 级审批，并自然由 Codex 缓存
- `dangerous`
  - 仅在显式配置时使用
  - 跳过正常审批摩擦

UI 标签可以兼容 `feishu-cc` 的习惯，但底层模型必须是 Codex 原生的。

## 10. 用户提问 / 选项卡片

`feishu-cc` 曾需要专门处理 `AskUserQuestion` 与 `ExitPlanMode`。

对于 Codex，v1 不应预先假设存在一模一样的特性模型。

设计选择：

- 先围绕以下核心桥接能力构建第一版：
  - turns
  - streaming
  - approvals
  - session list / resume / rename / interrupt
- 在 adapter 中保留通用的 `PromptRequest` / `UserResponse` 抽象
- 只有在确认 Codex 协议中存在稳定、且确实要求用户做选择的对象后，再添加专门的提问卡片

这样可以避免对 Claude 特有交互模式过拟合。

## 11. 持久化设计

### 11.1 本地存储内容

建议本地存储文件：`data/codex_threads.json`

按用户存储：

- `thread_id`
- `starred`
- 后续可选加飞书本地 tags

不要存储：

- thread title
- cwd
- preview
- timestamps

### 11.2 为什么收藏应保留在本地

Codex 原生支持线程命名，但目前没有明确验证过跨客户端的 favorites 概念。
收藏属于飞书 UX 关注点，因此保留在本地是可以接受的。

约束：

- 收藏绝不能覆盖或遮蔽 Codex 的线程元数据

## 12. 目录语义

`feishu-codex` 应保留当前 `feishu-cc` 行为中真正有用的那部分：

- 飞书聊天会话有“当前目录”概念
- `/session` 只列出当前目录候选
- `/resume` 可以恢复并切换当前目录

在 Codex 协议上的实现方式：

- `/session` 用 `thread.list(cwd=current_dir, sourceKinds=[...])`
- `thread.read` 返回权威 `cwd`
- 恢复线程时更新飞书会话的 `current_cwd`

## 13. 建议的仓库结构

建议新项目采用如下布局：

```text
feishu-codex/
  bot/
    feishu_bot.py
    cards.py
    handler.py
    codex_handler.py
    stores/
      favorites_store.py
      chat_state_store.py
    adapters/
      base.py
      codex_app_server.py
      codex_exec_fallback.py
    codex_protocol/
      client.py
      events.py
      models.py
      approvals.py
      threads.py
  config/
    codex.yaml.example
  docs/
    feishu-codex-design.md
```

如果未来确认需要复用 `feishu-cc` 代码，应在 `feishu-codex` 先验证稳定后，再把共用的飞书与卡片基础设施抽成共享包。

不要一开始就先做 shared library。

## 14. 实施计划

### Phase 0: 协议探测

- 通过 stdio 启动本地 `codex app-server`
- 构建一个最小 JSON-RPC client
- 验证：
  - initialize
  - thread/start
  - turn/start
  - streaming notifications
  - turn/interrupt
  - thread/list
  - thread/name/set
  - approval round-trip

### Phase 1: 核心飞书桥接

- 新建 `CodexAppServerAdapter`
- 新建 `codex_handler.py`
- prompt 进，stream 出
- `/new`
- `/status`
- `/cancel`

### Phase 2: Session 管理

- `/session`
- `/resume`
- `/rename`
- favorites
- current-directory 语义

### Phase 3: 原生审批

- permission approval cards
- exec approval cards
- session-scoped approval 支持

### Phase 4: 打磨

- 模型选择
- 更好的事件渲染
- 队列管理
- 基于 `codex exec --json` 的 fallback 路径

## 15. 风险

### 15.1 app-server 仍标记为 experimental

缓解方式：

- 保持 adapter 隔离
- 在更大规模实现前，先于 Phase 0 验证所需 API
- 保留 `codex exec --json` 作为 smoke test 与紧急降级路径

### 15.2 Source-kind 过滤可能把 session 宇宙切裂

缓解方式：

- 始终显式设置 `sourceKinds`
- 不依赖协议默认值

### 15.3 协议过于丰富，容易引诱过度设计

缓解方式：

- v1 只实现 thread lifecycle、turn lifecycle、streaming、rename、interrupt、approvals

## 16. 建议

将 `feishu-codex` 作为一个新项目来构建，以 `stdio` 上的 `codex app-server` 作为主集成路径。

不要 fork `feishu-cc` 再换二进制。
不要基于 Codex 的私有磁盘格式。
不要把 `codex exec --json` 当作主运行时。

这样可以得到：

- 更干净的 session 模型
- 原生 rename 与 resume
- 更干净的审批处理
- 更少的协议猜测
- 比 `feishu-cc` 更好的长期可维护性
