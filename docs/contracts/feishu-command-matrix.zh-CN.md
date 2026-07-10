# Feishu 命令矩阵

英文原文：`docs/contracts/feishu-command-matrix.md`

本文件是 Feishu 命令面的单一事实源矩阵。

它只回答四类问题：

- 当前正式支持哪些 slash 命令
- 哪些命令可从 `/help` 工作台进入
- 在私聊与群聊里分别由谁可执行
- 与哪一个本地入口最接近

它不重新定义：

- binding / attach / detach / backend 状态语义
- 剩余设置族的事实源与生效边界
- `assistant / mention-only / all` 的完整群聊合同

## 1. 阅读规则

- “可从 `/help` 进入” 指可通过工作台按钮、表单或后续结果卡片进入。
- “仅管理员” 指必须通过当前机器人的管理员检查。
- “任何人” 只表示该作用域下没有管理员检查；不表示所有群成员都可改共享状态。

## 2. 命令

### 2.1 导航、开始、线程设置与连接状态

| 命令 | 用途 | 可从 `/help` 进入 | 私聊 | 群聊 | 最接近的本地对应 |
| --- | --- | --- | --- | --- | --- |
| `/help [overview\|start\|thread-settings\|turn\|connection\|group\|more]` | 打开工作台或直接进入某个工作区；旧别名 `chat/thread/runtime/identity` 仍兼容 | 是 | 仅管理员 | 仅管理员 | 无 |
| `/commands` | 以纯文本列出常用命令 | 是；`More` 页 | 仅管理员 | 仅管理员 | 无 |
| `/h` | `/help` 别名 | 否 | 仅管理员 | 仅管理员 | 无 |
| `/pwd` | 查看当前工作目录 | 否 | 仅管理员 | 仅管理员 | 无 |
| `/status` | 查看当前 chat 的目录、当前 thread 与当前会话设置摘要 | 是；`Connection Status` 页 | 仅管理员 | 仅管理员 | 深度诊断可用 `focusctl binding status <binding_id>` |
| `/preflight` | 对下一条普通消息与当前 chat 的 detach 可用性做 dry-run | 是；`Connection Status` 页 | 仅管理员 | 仅管理员 | 与 `focusctl binding status <binding_id>` 部分重叠 |
| `/cd [path]` | 查看或切换当前目录，支持 `~` 展开为用户 home；切换时清空当前 thread 绑定 | 是；`Start` 表单 | 仅管理员 | 仅管理员 | 无 |
| `/new` | 创建新的当前 thread | 是；`Start` 页 | 仅管理员 | 仅管理员 | 无 |
| `/threads` | 浏览当前目录线程 | 是；`Start` 页 | 仅管理员 | 仅管理员 | `focusctl thread list --scope cwd` |
| `/resume <thread_id\|thread_name>` | 把目标线程恢复到当前 chat | 是；`Start` 表单 | 仅管理员 | 仅管理员 | 本地继续 live thread 用 `focus resume <thread_id\|thread_name>` 或 `fcodex resume <thread_id\|thread_name>` |
| `/goal [show\|text\|set <objective>\|pause\|resume\|clear]` | 查看或管理当前 thread 的 goal；`text` 导出纯文本 | 是；`Thread Settings` 页 | 仅管理员 | 仅管理员 | 无 |
| `/compact` | 压缩当前绑定 thread 的上下文历史 | 是；`Thread Settings` 页 | 仅管理员 | 仅管理员 | 无直接本地等价命令 |
| `/rename <title>` | 重命名当前 thread | 是；`Thread Settings` 表单 | 仅管理员 | 仅管理员 | 无 |
| `/archive [thread_id\|thread_name]` | 归档当前线程，或归档显式指定目标 | 是；`Thread Settings` 按钮或表单 | 仅管理员 | 仅管理员 | `focusctl thread archive --thread-id/--thread-name`；本地 CLI 额外支持重复 `--thread-id` 批量归档 |
| `/detach` | 保留 binding 记录，但停止当前 chat 接收当前 thread 的飞书推送 | 是；`Connection Status` 的动态按钮 | 仅管理员 | 仅管理员 | `focusctl binding detach <binding_id>`；thread 维度是 `focusctl thread detach ...` |
| `/attach [binding\|thread\|service]` | 恢复当前 chat、当前 thread 或当前实例的飞书推送 | 是；`Connection Status` 及其下级页，也会出现在上下文结果卡片里 | 仅管理员 | 仅管理员 | `focusctl binding/thread/service attach ...` |
| `/cancel` | 取消当前执行 | 否；主入口是执行卡按钮 | 仅管理员 | 仅管理员 | 无 |

### 2.2 Turn Settings 与 More

| 命令 | 用途 | 可从 `/help` 进入 | 私聊 | 群聊 | 最接近的本地对应 |
| --- | --- | --- | --- | --- | --- |
| `/permissions [read-only\|workspace\|danger-full-access]` | 独立于审批策略设置权限基线 | 是；`Turn Settings` 页 | 仅管理员 | 仅管理员 | 无 |
| `/model [name\|auto]` | 设置当前 Feishu 会话的 turn-time model override；无参数时打开共享 model/effort 卡 | 是；`Turn Settings` 页 | 仅管理员 | 仅管理员 | 无 |
| `/effort [auto\|value]` | 设置当前 Feishu binding 的 turn-time effort override；已知 model 按 metadata 约束，auto/未知 model 允许自定义值；无参数时打开共享 model/effort 卡 | 是；`Turn Settings` 页 | 仅管理员 | 仅管理员 | 无 |
| `/approval [untrusted\|on-request\|never]` | 设置 approval policy | 是；`Turn Settings` 页 | 仅管理员 | 仅管理员 | 无 |
| `/last text` | 导出当前会话最近的权威终态文本；优先 terminal result，其次回退最近执行卡 | 是；`Turn Settings` 页 | 仅管理员 | 仅管理员 | 无 |
| `/reset-backend` | 预览并重置当前实例 backend，用于恢复 | 是；`More -> Advanced Actions` | 仅管理员 | 仅管理员 | `focusctl service reset-backend` |
| `/whoami` | 查看调用者身份 | 是；`More` 页 | 任何人 | 不支持 | 无 |
| `/bot-status` | 查看 bot 身份与配置探测结果 | 是；`More` 页 | 任何人 | 仅管理员 | 无 |
| `/init <token>` | 初始化 admins 与 `bot_open_id` | 是；`More` 表单 | 任何人 | 不支持 | `focusctl config init-token` 仅显示 token |
| `/debug-contact <open_id>` | 排查联系人名称解析 | 是；`More -> Advanced Actions` 表单 | 仅管理员 | 不支持 | 无 |

### 2.3 群设置

| 命令 | 用途 | 可从 `/help` 进入 | 私聊 | 群聊 | 最接近的本地对应 |
| --- | --- | --- | --- | --- | --- |
| `/group` | 查看当前群是否已激活 | 是；`Group Settings` 页 | 不支持 | 仅管理员 | 无 |
| `/group activate` | 激活当前群 | 是；`Group Settings` 页 | 不支持 | 仅管理员 | 无 |
| `/group deactivate` | 关闭当前群 | 是；`Group Settings` 页 | 不支持 | 仅管理员 | 无 |
| `/group-mode` | 查看当前群工作模式 | 是；`Group Settings` 页 | 不支持 | 仅管理员 | 无 |
| `/group-mode assistant` | 切到 `assistant` | 是；`Group Settings` 页 | 不支持 | 仅管理员 | 无 |
| `/group-mode mention-only` | 切到 `mention-only` | 是；`Group Settings` 页 | 不支持 | 仅管理员 | 无 |
| `/group-mode all` | 切到 `all` | 是；`Group Settings` 页 | 不支持 | 仅管理员 | 无 |

## 3. 故意不放在工作台首页的命令

这些命令仍保持为别名、纯文本或结果卡入口，而不是固定首页按钮：

- `/h`
- `/pwd`
- `/cancel`

补充说明：

- `/commands` 现在可从 `More` 进入
- `/attach` 现在可从 `Connection Status` 进入
- `/debug-contact` 现在可从 `More -> Advanced Actions` 进入

## 4. 结果卡按钮

这些按钮属于正式 Feishu 用户面，必须与 slash 命令一并维护：

- execution card：`Cancel Execution`
- `/threads` 列表卡：`Resume/Current`、`Archive`、`More`、`Collapse`
- `/goal` 卡：`Refresh`、`Pause` / `Resume`、`Clear`
- `/reset-backend` 结果卡：后续 attach/detach 按钮，例如 `附着当前线程`、`附着当前实例`、`保持 detached`
- 共享 `/model` / `/effort` 卡，以及 `/permissions` / `/approval` 卡：turn-time runtime-setting toggle buttons
- approval / extra-input cards：各自请求类型对应的 allow / deny / submit 按钮

## 5. 边界

- FOCUS 负责 install、instance 与 service 生命周期；它不是 Feishu chat 命令面。
- `focusctl` 负责本地 binding、thread、service 的查看与管理；它不是第二前端。
- `focus` / `fcodex` 是本地 live-thread continuation 入口。

如果 Feishu 命令有新增、删除、改名、移动 `/help` 工作区，或按钮入口与权限边界发生变化，本文件必须随代码一起更新。
