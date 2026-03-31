# feishu-codex

`feishu-codex` 通过 Feishu 机器人把消息、审批和会话管理接到 `codex app-server`，不依赖 Claude 风格 hook，也不扫描私有会话文件。

当前状态是可安装、可启动、核心链路可用的 MVP，架构已经切到 Codex 原生协议，但功能成熟度仍低于 `feishu-cc`。

## 前置条件

- Python 3.10+
- 本机已安装 `codex` CLI，且 `codex --help` 可正常执行
- 飞书开放平台已创建应用，获取 `app_id` 和 `app_secret`

## 安装

```bash
cd /path/to/feishu-codex
bash install.sh
```

`install.sh` 会自动完成：

- 创建 Python 虚拟环境到 `~/.local/share/feishu-codex/.venv/`
- 安装代码包与依赖
- 初始化配置文件到 `~/.config/feishu-codex/`
- 注册 systemd 用户服务并安装 `feishu-codex` 管理命令

安装后填写飞书凭证：

```bash
nano ~/.config/feishu-codex/system.yaml
```

可选地调整飞书 API 请求超时：

```yaml
# ~/.config/feishu-codex/system.yaml
app_id: "..."
app_secret: "..."
# request_timeout_seconds: 10
```

按需调整 Codex 参数：

```bash
nano ~/.config/feishu-codex/codex.yaml
```

## 配置

运行时环境变量：

- `FC_CONFIG_DIR`: 配置目录
- `FC_DATA_DIR`: 数据目录

未设置时，开发态默认读取项目内 `config/`，数据默认写到 `data/feishu_codex/`。

## 使用

```bash
feishu-codex start
feishu-codex stop
feishu-codex restart
feishu-codex status
feishu-codex log
feishu-codex run
feishu-codex config
feishu-codex uninstall
feishu-codex purge
```

如果你只是临时调试，也可以直接：

```bash
python -m bot
```

## 设计要点

- Codex 线程元数据以 app-server 为单一事实源
- 本地只持久化 Feishu 特有状态，例如收藏
- `/session` 显示当前目录线程，收藏优先
- `/resume` 先按 thread id 原生恢复，失败后再按 thread name 精确匹配

## 当前功能

- 直接发送普通文本给当前线程；若未绑定线程，会在当前目录自动新建
- `/new`、`/session`、`/resume <thread_id|thread_name>`、`/rename <title>`、`/star`
- `/cd`、`/pwd`、`/status`、`/cancel`
- `/approval` 查看或切换原生 Codex 审批策略
- 原生 Codex 审批卡片：
  - `item/commandExecution/requestApproval`
  - `item/fileChange/requestApproval`
  - `item/permissions/requestApproval`
  - `item/tool/requestUserInput`

## 与 feishu-cc 的现状差距

当前还没有这些能力：

- `feishu-cc` 的 workspace 系列命令和 `/run`
- `feishu-cc` 的 `/model` 和更多会话控制能力
- 更完整的降级、重试、异常恢复和可观测性
- 更完整的 MCP 交互支持
