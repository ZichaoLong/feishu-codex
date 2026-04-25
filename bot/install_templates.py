"""
Installation templates bundled with the Python package.
"""

SYSTEM_YAML_TEMPLATE = """app_id: "your_cc_bot_app_id"
app_secret: "your_cc_bot_app_secret"

# 飞书 HTTP API 请求超时（秒）；默认值：10
# request_timeout_seconds: 10

# 群聊管理员 open_id 列表；这些人始终具备群聊触发资格，并可使用
# 所有群里的 `/` 命令（包括 `/group`、`/groupmode`、`/new`、`/session` 等）。
# 若群工作态是 `assistant` 或 `mention-only`，他们仍需先显式 mention
# 触发对象，才会触发对话或群命令。
# 也可在服务启动后，私聊机器人执行 `/init <token>` 自动写入。
# admin_open_ids:
#   - "ou_xxx 这样的 open_id；可先私聊机器人发送 /whoami 获取"

# 机器人自身的 open_id。群聊能力依赖它做严格 mention 判定。
# 若已开通 `application:application:self_manage`，也可通过 `/init <token>` 自动写入。
# bot_open_id: "ou_xxx"

# 初始化口令不放在 YAML 里；安装程序会在配置目录生成单独的 `init.token` 文件。
# 首次私聊机器人时，可执行 `/init <token>` 完成管理员和 `bot_open_id` 初始化。

# 额外的群聊触发 open_id 列表。若群消息 @到了这些 open_id，也会被视为
# 一次有效触发。常见场景：别人 @你本人，由机器人代答。
# 仍建议同时配置 `bot_open_id`；未配置 `bot_open_id` 时，这些 alias 不生效。
# trigger_open_ids:
#   - "ou_xxx"

# assistant 模式每次有效被 @ 时，最多回捞多少条历史消息。
# 设为 0 表示禁用历史回捞。默认值：50
# group_history_fetch_limit: 50

# assistant 模式每次历史回捞的时间窗口（秒）。
# 设为 0 表示禁用历史回捞。默认值：86400（24 小时）
# group_history_fetch_lookback_seconds: 86400
"""

CODEX_YAML_TEMPLATE = """# 默认工作目录；默认值：进程当前目录
# default_working_dir: /path/to/workspace

# Codex 可执行命令；默认值：codex
# 如果通过 fnm 管理 Node，建议填写稳定安装路径，不要写 /run/user/.../fnm_multishells/... 这类临时 shim。
# codex_command: codex

# app-server 连接模式；默认值：managed
# managed: feishu-codex 自己拉起并维护 shared app-server；
#          默认优先监听 ws://127.0.0.1:8765，若被占用会自动切到空闲本地端口
# remote:  feishu-codex 连接到一个已存在的 app-server endpoint
# app_server_mode: managed

# app-server 地址；默认值：ws://127.0.0.1:8765
# 说明：在 managed 模式下，这是默认目标地址；若默认端口不可用，
# feishu-codex 会自动改用空闲端口，并把实际地址写入本地运行时状态。
# 本地若希望与飞书安全共用同一线程，可使用 fcodex；当这里仍是默认值时，
# 它会自动发现当前实际运行的 shared backend 地址。
# app_server_url: ws://127.0.0.1:8765

# app-server 连接超时（秒）；默认值：15
# connect_timeout_seconds: 15

# app-server 请求超时（秒）；默认值：30
# request_timeout_seconds: 30

# 线程创建时声明的服务名；默认值：feishu-codex
# service_name: feishu-codex

# 默认沙箱模式；默认值：workspace-write
# 说明：这是“技术边界”，决定命令和工具最终在什么文件系统 / 网络权限下运行。
# 可选值：read-only、workspace-write、danger-full-access
# sandbox: workspace-write

# 默认审批策略；默认值：on-request
# 说明：这是“审批边界”，决定什么时候需要先经过审批才能继续。
# 可选值：untrusted、on-request、never
# 说明：仓库不再暴露 deprecated 的 on-failure；如旧配置里仍写了它，会自动按 on-request 处理。
# approval_policy: on-request

# 审批审阅者；默认值：user
# approvals_reviewer: user

# 默认 personality；默认值：pragmatic
# personality: pragmatic

# 默认模型；默认值：空，表示沿用 Codex 默认
# model: gpt-5.4

# 默认 model provider；默认值：空
# 说明：如需让 shared backend 通过 `/profile` 在多个 profile/provider 之间切换，建议保持为空。
# 说明：如果这里写死了 model_provider，feishu-codex 新建线程时会优先用这里的值，可能覆盖 shared profile 的作用。
# model_provider: openai

# 默认 service tier；默认值：空
# service_tier: fast

# 默认 reasoning effort；默认值：空
# reasoning_effort: high

# 协作模式；默认值：default
# 说明：设为 plan 时，会在 turn/start 中启用 Codex 原生 collaborationMode，并解锁原生 requestUserInput。
# 说明：这只是当前飞书会话的默认值；运行中可用 /mode 临时切到 plan 或 default。
# collaboration_mode: default

# /session 与 /resume 查询时显式纳入的线程来源；默认值：["cli", "vscode", "exec", "appServer"]
# source_kinds:
#   - cli
#   - vscode
#   - exec
#   - appServer

# /session 初始展示的线程数量上限；默认值：5
# 说明：点击卡片里的“更多”后会展开全部线程。
# session_recent_limit: 5

# 单次 thread/list 聚合查询的最大线程数；默认值：100
# thread_list_query_limit: 100

# /resume 后附带的历史预览轮数；默认值：3
# history_preview_rounds: 3

# /resume 成功后是否发送历史预览卡片；默认值：true
# show_history_preview_on_resume: true

# 飞书附件 pending 状态的过期秒数；默认值：1800（30 分钟）
# 说明：附件会先下载到当前工作目录下的 `_feishu_attachments/`，等待同一发送者的下一条文本消费。
# 说明：到期后不会再自动绑定到后续文本，且本地暂存文件会被清理。
# attachment_ttl_seconds: 1800

# 运行中主卡片最小 patch 间隔（毫秒）；默认值：700
# stream_patch_interval_ms: 700

# 主卡片中回复区最大字符数；默认值：12000
# 超出后卡片会截断，完整回复会额外以文本消息发送。
# card_reply_limit: 12000

# 终态结果卡正文预算；默认值：12000
# 只影响 terminal result card / 文本兜底，不影响 execution card 的 reply 面板截断。
# terminal_result_card_limit: 12000

# 主卡片中执行日志区最大字符数；默认值：8000
# card_log_limit: 8000
"""
