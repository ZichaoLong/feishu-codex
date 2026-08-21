# Feishu `/help` 导航合同

文档角色：中文规范源。英文同步副本：`docs/contracts/feishu-help-navigation.md`。

本文件只定义 `/help` 与 `/commands` 的导航合同。

## 1. 首页目标

`/help` 不是完整文档站，也不是把所有命令平铺罗列出来。

它的职责是：

1. 给出紧凑的当前状态摘要
2. 把用户路由到固定工作区
3. 让低频动作留在下级页面或结果卡里

## 2. 固定首页工作区

首页必须只暴露这六个工作区：

- `Start`
- `Thread Settings`
- `Turn Settings`
- `Connection Status`
- `Group Settings`
- `More`

首页摘要至少应包含：

- 当前工作目录
- 当前 thread
- 当前推送状态
- 当前 turn-setting 摘要

## 3. 页面合同

### 3.1 Start

负责：

- `/new`
- `/threads`
- `/resume`
- `/cd`

正文应提醒用户：

- 同一 thread 可以被多个端观察；飞书 next-turn/FIFO 与 review/compact 等 exclusive action 仍由 exact main-turn lease 串行化，普通 Web/`fcodex` prompt 则保持 upstream-routed start-or-steer，不取得 writer。live 且已 attach exact direct root 的 `fcodex` endpoint，或已连接并 materialize 该 root 的可信本机 Web document，可以 steer exact current regular turn；二者也可按 [canonical main-turn owner 合同](root-operation-owner.zh-CN.md) interrupt exact current/startup turn，但不会 takeover 或转移 writer；只有 exact pending-request capability 可以回答交互
- 本地接入同一个 live thread 使用 `focus resume <thread_id|thread_name>` 或 `fcodex resume <thread_id|thread_name>`；可能自主开工的 resume 与 exclusive action 仍走 blank-submission / active-main-turn admission，普通输入与 canonical steer/interrupt 走各自 effect-specific 边界，均不会自动授予 takeover

### 3.2 Thread Settings

负责：

- `/goal`
- `/compact`
- `/archive`
- rename form

正文应提醒用户：

- thread 创建、恢复与浏览属于 `Start`
- 这里已经不再有项目自管的 profile 或 thread-memory 控制面

### 3.3 Turn Settings

负责：

- `/permissions`
- `/model`
- `/effort`
- `/approval`
- `/last text`

正文应提醒用户：

- 这些设置只影响当前 Feishu binding 的后续 turn
- `/permissions` 是推荐的第一个入口

### 3.4 Connection Status

负责：

- `/status`
- `/preflight`
- `/detach`
- `/attach`
- 相关 attach 下级页

### 3.5 Group Settings

负责：

- `/group`
- `/group activate`
- `/group deactivate`
- `/group-mode`

### 3.6 More

负责：

- `/commands`
- `/whoami`
- `/bot-status`
- `/init`
- `/reset-backend`
- `/debug-contact`

## 4. 返回按钮规则

- 一级工作区页面必须暴露 `Back Home`
- 下级页面只暴露 `Back`
- 从 `/help` 打开的命令卡或结果卡，即使经过后续按钮操作或表单提交，也必须仍然能回到帮助首页
- 每个返回按钮各占一整行

## 5. 兼容入口
