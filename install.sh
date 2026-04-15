#!/usr/bin/env bash
# feishu-codex 安装脚本
# 用法: bash install.sh
# 功能: 创建 venv、安装代码包与依赖、注册 systemd 用户服务、安装管理命令 feishu-codex / feishu-codexctl

set -euo pipefail

INSTALL_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_NAME="feishu-codex"
SYSTEMD_DIR="$HOME/.config/systemd/user"
SCRIPT_DEST="$HOME/.local/bin/$SERVICE_NAME"
CODEXCTL_DEST="$HOME/.local/bin/feishu-codexctl"
FCODEX_DEST="$HOME/.local/bin/fcodex"
CONFIG_DIR="$HOME/.config/$SERVICE_NAME"
ENV_DIR="$HOME/.config/environment.d"
ENV_FILE="$ENV_DIR/90-codex.conf"
DATA_DIR="$HOME/.local/share/$SERVICE_NAME"
VENV_DIR="$HOME/.local/share/$SERVICE_NAME/.venv"
INIT_TOKEN_FILE="$CONFIG_DIR/init.token"

_green()  { echo -e "\033[32m$*\033[0m"; }
_yellow() { echo -e "\033[33m$*\033[0m"; }
_red()    { echo -e "\033[31m$*\033[0m"; }
_bold()   { echo -e "\033[1m$*\033[0m"; }

_is_transient_fnm_shim() {
    [[ "${1:-}" == /run/user/*/fnm_multishells/*/bin/codex ]]
}

_normalize_codex_path() {
    local path="${1:-}"
    local resolved=""
    local candidate=""

    if [ -z "$path" ]; then
        return 0
    fi

    if _is_transient_fnm_shim "$path"; then
        resolved="$(readlink -f "$path" 2>/dev/null || true)"
        if [[ "$resolved" == */lib/node_modules/@openai/codex/bin/codex.js ]]; then
            candidate="${resolved%/lib/node_modules/@openai/codex/bin/codex.js}/bin/codex"
            if [ -x "$candidate" ]; then
                printf '%s\n' "$candidate"
                return 0
            fi
        fi
    fi

    printf '%s\n' "$path"
}

_read_configured_codex_path() {
    local config_file="${1:-}"
    if [ -z "$config_file" ] || [ ! -f "$config_file" ]; then
        return 0
    fi
    sed -nE 's/^[[:space:]]*codex_command:[[:space:]]*"?([^"#]+)"?[[:space:]]*$/\1/p' "$config_file" | head -n 1
}

echo ""
_bold "=== feishu-codex 安装程序 ==="
echo "安装目录: $INSTALL_DIR"
echo "配置目录: $CONFIG_DIR"
echo "数据目录: $DATA_DIR"
echo ""

echo "[ 1/6 ] 检查前置条件..."

PYTHON=""
for py in python3.12 python3.11 python3.10 python3; do
    if command -v "$py" &>/dev/null; then
        ver=$("$py" -c "import sys; print(sys.version_info >= (3,10))" 2>/dev/null || echo "False")
        if [ "$ver" = "True" ]; then
            PYTHON="$py"
            break
        fi
    fi
done
if [ -z "$PYTHON" ]; then
    _red "  [错误] 需要 Python 3.10 或更高版本"
    exit 1
fi
_green "  ✓ Python: $($PYTHON --version)"

CODEX_PATH=""
RAW_CODEX_PATH=""
if command -v codex &>/dev/null; then
    RAW_CODEX_PATH="$(command -v codex)"
    CODEX_PATH="$(_normalize_codex_path "$RAW_CODEX_PATH")"
fi
if [ -z "$CODEX_PATH" ]; then
    _yellow "  [警告] 未找到 codex CLI，请确认已安装 Codex 并可在 PATH 中执行"
    _yellow "         安装后可在 ~/.config/feishu-codex/codex.yaml 中手动设置 codex_command"
    CODEX_PATH="codex"
else
    _green "  ✓ Codex CLI: $CODEX_PATH"
    if [ -n "$RAW_CODEX_PATH" ] && [ "$RAW_CODEX_PATH" != "$CODEX_PATH" ]; then
        _yellow "    检测到临时 shell shim，已归一化为稳定路径"
    fi
fi

if ! systemctl --user status &>/dev/null; then
    _yellow "  [警告] systemd --user 不可用，将跳过服务注册"
    SKIP_SYSTEMD=true
else
    SKIP_SYSTEMD=false
    _green "  ✓ systemd --user 可用"
fi

echo ""
echo "[ 2/6 ] 创建 Python 虚拟环境..."
if [ -d "$VENV_DIR" ]; then
    _yellow "  已存在 venv，跳过创建: $VENV_DIR"
else
    mkdir -p "$(dirname "$VENV_DIR")"
    "$PYTHON" -m venv "$VENV_DIR"
    _green "  ✓ 虚拟环境创建完成: $VENV_DIR"
fi

echo "       安装软件包..."
"$VENV_DIR/bin/pip" install -q --upgrade pip
"$VENV_DIR/bin/pip" install -q "$INSTALL_DIR"
_green "  ✓ 安装完成（代码已部署到 venv，源码目录可保留或删除）"

echo ""
echo "[ 3/6 ] 初始化配置文件..."

mkdir -p "$CONFIG_DIR" "$DATA_DIR" "$ENV_DIR"

cp "$INSTALL_DIR/config/system.yaml.example" "$CONFIG_DIR/system.yaml.example"
_green "  ✓ 已刷新 ~/.config/feishu-codex/system.yaml.example（本地默认模板）"

cp "$INSTALL_DIR/config/codex.yaml.example" "$CONFIG_DIR/codex.yaml.example"
_green "  ✓ 已刷新 ~/.config/feishu-codex/codex.yaml.example（本地默认模板）"

if [ -f "$CONFIG_DIR/system.yaml" ]; then
    _yellow "  ~/.config/feishu-codex/system.yaml 已存在，跳过（保护现有配置）"
else
    cp "$CONFIG_DIR/system.yaml.example" "$CONFIG_DIR/system.yaml"
    _green "  ✓ 已创建 ~/.config/feishu-codex/system.yaml（请填入飞书应用凭证）"
fi

if [ -s "$INIT_TOKEN_FILE" ]; then
    chmod 600 "$INIT_TOKEN_FILE" 2>/dev/null || true
    _yellow "  ~/.config/feishu-codex/init.token 已存在，跳过（保护现有口令）"
else
    "$PYTHON" - "$INIT_TOKEN_FILE" <<'PY'
import os
import pathlib
import secrets
import sys

path = pathlib.Path(sys.argv[1])
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(secrets.token_urlsafe(24) + "\n", encoding="utf-8")
os.chmod(path, 0o600)
PY
    _green "  ✓ 已创建 ~/.config/feishu-codex/init.token"
fi

if [ -f "$CONFIG_DIR/codex.yaml" ]; then
    EXISTING_CODEX_PATH="$(_read_configured_codex_path "$CONFIG_DIR/codex.yaml")"
    if _is_transient_fnm_shim "$EXISTING_CODEX_PATH" && [ "$CODEX_PATH" != "codex" ] && [ "$CODEX_PATH" != "$EXISTING_CODEX_PATH" ]; then
        sed -i -E "s|^#?[[:space:]]*codex_command:.*|codex_command: \"$CODEX_PATH\"|" "$CONFIG_DIR/codex.yaml" 2>/dev/null || true
        _green "  ✓ 已将 ~/.config/feishu-codex/codex.yaml 中的临时 shim 更新为稳定路径"
    else
        _yellow "  ~/.config/feishu-codex/codex.yaml 已存在，跳过（保护现有配置）"
    fi
else
    cp "$CONFIG_DIR/codex.yaml.example" "$CONFIG_DIR/codex.yaml"
    if [ "$CODEX_PATH" != "codex" ]; then
        sed -i -E "s|^#?[[:space:]]*codex_command:.*|codex_command: \"$CODEX_PATH\"|" "$CONFIG_DIR/codex.yaml" 2>/dev/null || true
    fi
    _green "  ✓ 已创建 ~/.config/feishu-codex/codex.yaml"
fi

if [ -f "$ENV_FILE" ]; then
    _yellow "  ~/.config/environment.d/90-codex.conf 已存在，跳过（保护现有配置）"
else
    cat > "$ENV_FILE" << 'EOF'
# Codex provider 所需环境变量。
# 修改后请重启 feishu-codex 服务：
#   systemctl --user restart feishu-codex
#
# 示例：
# provider1_api_key=your-provider1-key
# provider2_api_key=your-provider2-key
EOF
    _green "  ✓ 已创建 ~/.config/environment.d/90-codex.conf"
fi

echo ""
echo "[ 4/6 ] 注册 systemd 用户服务..."

if [ "$SKIP_SYSTEMD" = true ]; then
    _yellow "  跳过（systemd --user 不可用）"
else
    mkdir -p "$SYSTEMD_DIR"

    EXTRA_PATHS="$HOME/.local/bin:$HOME/.npm-global/bin"
    NVM_NODE_BIN=$(ls -d "$HOME/.nvm/versions/node/"*/bin 2>/dev/null | tail -1 || true)
    [ -n "$NVM_NODE_BIN" ] && EXTRA_PATHS="$EXTRA_PATHS:$NVM_NODE_BIN"
    SERVICE_PATH="${EXTRA_PATHS}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

    cat > "$SYSTEMD_DIR/$SERVICE_NAME.service" << EOF
[Unit]
Description=Feishu Codex Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$DATA_DIR
ExecStart=$VENV_DIR/bin/python -m bot
Environment=FC_CONFIG_DIR=$CONFIG_DIR
Environment=FC_DATA_DIR=$DATA_DIR
EnvironmentFile=-%h/.config/environment.d/90-codex.conf
Environment=PATH=$SERVICE_PATH
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
EOF

    systemctl --user daemon-reload
    systemctl --user enable "$SERVICE_NAME" 2>/dev/null || true
    _green "  ✓ 服务已注册并设为开机自启: $SYSTEMD_DIR/$SERVICE_NAME.service"

    if loginctl enable-linger "$USER" 2>/dev/null; then
        _green "  ✓ 已开启 lingering（开机自动启动，无需等待登录）"
    else
        _yellow "  [警告] loginctl enable-linger 失败，服务将在用户登录后才启动"
    fi
fi

echo ""
echo "[ 5/6 ] 安装管理命令 feishu-codex / feishu-codexctl..."

mkdir -p "$(dirname "$SCRIPT_DEST")"

cat > "$SCRIPT_DEST" << SCRIPT
#!/usr/bin/env bash
# feishu-codex 管理脚本（由 install.sh 生成）
SERVICE_NAME="$SERVICE_NAME"
CONFIG_DIR="$CONFIG_DIR"
DATA_DIR="$DATA_DIR"
VENV_DIR="$VENV_DIR"
ENV_FILE="$ENV_FILE"

_needs_systemd() {
    if ! systemctl --user status &>/dev/null; then
        echo "[feishu-codex] systemd --user 不可用，请使用: feishu-codex run"
        exit 1
    fi
}

case "\${1:-}" in
    start)
        _needs_systemd
        systemctl --user start "\$SERVICE_NAME"
        ;;
    stop)
        _needs_systemd
        systemctl --user stop "\$SERVICE_NAME"
        ;;
    restart)
        _needs_systemd
        systemctl --user restart "\$SERVICE_NAME"
        ;;
    status)
        _needs_systemd
        systemctl --user status "\$SERVICE_NAME"
        ;;
    log)
        _needs_systemd
        journalctl --user -u "\$SERVICE_NAME" -f --output=cat
        ;;
    run)
        echo "[feishu-codex] 前台运行模式（Ctrl+C 退出）"
        cd "$DATA_DIR"
        export FC_CONFIG_DIR="$CONFIG_DIR"
        export FC_DATA_DIR="$DATA_DIR"
        if [ -f "$ENV_FILE" ]; then
            set -a
            # shellcheck disable=SC1090
            . "$ENV_FILE"
            set +a
        fi
        exec "$VENV_DIR/bin/python" -m bot
        ;;
    config)
        EDITOR="\${VISUAL:-\${EDITOR:-nano}}"
        echo "配置目录: $CONFIG_DIR"
        echo "  system.yaml — 飞书应用凭证"
        echo "  codex.yaml  — Codex 运行参数"
        echo "  init.token  — 私聊 \`/init <token>\` 使用的初始化口令"
        echo ""
        read -r -p "打开哪个文件？[1] system.yaml  [2] codex.yaml  [3] init.token  (Enter 取消) " _choice
        case "\$_choice" in
            1) exec "\$EDITOR" "$CONFIG_DIR/system.yaml" ;;
            2) exec "\$EDITOR" "$CONFIG_DIR/codex.yaml" ;;
            3) exec "\$EDITOR" "$CONFIG_DIR/init.token" ;;
            *) echo "已取消。" ;;
        esac
        ;;
    clear-bindings)
        echo "即将清空 feishu-codex 已保存的 Feishu 聊天绑定。"
        echo "这不会删除 Codex 线程本身，只会让飞书侧当前会话忘记自己绑定到哪个 thread。"
        echo ""
        read -r -p "确认清空？[y/N] " _confirm
        if [[ "\$_confirm" != "y" && "\$_confirm" != "Y" ]]; then
            echo "已取消。"
            exit 0
        fi
        export FC_CONFIG_DIR="$CONFIG_DIR"
        export FC_DATA_DIR="$DATA_DIR"
        exec "$VENV_DIR/bin/feishu-codex-clear-bindings"
        ;;
    uninstall)
        echo "即将卸载 feishu-codex，将删除以下内容："
        echo "  systemd 服务:  ~/.config/systemd/user/\$SERVICE_NAME.service"
        echo "  管理命令:      ~/.local/bin/feishu-codex"
        echo "  本地管理 CLI:  ~/.local/bin/feishu-codexctl"
        echo ""
        echo "以下内容不会删除（使用 purge 可一并清除）："
        echo "  配置目录:      $CONFIG_DIR"
        echo "  数据目录:      $DATA_DIR  （含 venv）"
        echo "  项目源码:      $INSTALL_DIR"
        echo ""
        read -r -p "确认卸载？[y/N] " _confirm
        if [[ "\$_confirm" != "y" && "\$_confirm" != "Y" ]]; then
            echo "已取消。"
            exit 0
        fi
        echo ""
        systemctl --user stop "\$SERVICE_NAME" 2>/dev/null || true
        systemctl --user disable "\$SERVICE_NAME" 2>/dev/null || true
        rm -f "\$HOME/.config/systemd/user/\$SERVICE_NAME.service"
        systemctl --user daemon-reload 2>/dev/null || true
rm -f "\$HOME/.local/bin/feishu-codex"
        rm -f "\$HOME/.local/bin/feishu-codexctl"
        rm -f "\$HOME/.local/bin/fcodex"
        echo "卸载完成。配置和数据已保留，重新安装只需运行："
        echo "  bash $INSTALL_DIR/install.sh"
        ;;
    purge)
        echo "即将彻底清除 feishu-codex 所有数据，将删除以下内容："
        echo "  systemd 服务:  ~/.config/systemd/user/\$SERVICE_NAME.service"
        echo "  管理命令:      ~/.local/bin/feishu-codex"
        echo "  本地管理 CLI:  ~/.local/bin/feishu-codexctl"
        echo "  配置目录:      $CONFIG_DIR  （含飞书凭证）"
        echo "  数据目录:      $DATA_DIR  （含 venv）"
        echo ""
        echo "以下内容不会删除："
        echo "  项目源码:      $INSTALL_DIR"
        echo ""
        read -r -p "确认彻底清除？此操作不可恢复 [y/N] " _confirm
        if [[ "\$_confirm" != "y" && "\$_confirm" != "Y" ]]; then
            echo "已取消。"
            exit 0
        fi
        echo ""
        systemctl --user stop "\$SERVICE_NAME" 2>/dev/null || true
        systemctl --user disable "\$SERVICE_NAME" 2>/dev/null || true
        rm -f "\$HOME/.config/systemd/user/\$SERVICE_NAME.service"
        systemctl --user daemon-reload 2>/dev/null || true
        rm -f "\$HOME/.local/bin/feishu-codex"
        rm -f "\$HOME/.local/bin/feishu-codexctl"
        rm -f "\$HOME/.local/bin/fcodex"
        rm -rf "$CONFIG_DIR"
        rm -rf "$DATA_DIR"
        echo "已彻底清除。项目源码保留在 $INSTALL_DIR"
        ;;
    *)
        echo "用法: feishu-codex {start|stop|restart|status|log|run|config|clear-bindings|uninstall|purge}"
        echo ""
        echo "  start     启动服务（通过 systemd）"
        echo "  stop      停止服务"
        echo "  restart   重启服务"
        echo "  status    查看服务状态"
        echo "  log       查看实时日志（Ctrl+C 退出）"
        echo "  run       前台运行（调试用，直接执行不走 systemd）"
        echo "  config    打开配置文件（飞书凭证 / Codex 参数）"
        echo "  clear-bindings 清空飞书侧已保存的聊天绑定"
        echo "  uninstall 卸载服务和管理命令（保留配置、数据、源码）"
        echo "  purge     彻底清除（在 uninstall 基础上额外删除配置和数据）"
        ;;
esac
SCRIPT

chmod +x "$SCRIPT_DEST"
_green "  ✓ 管理命令已安装: $SCRIPT_DEST"

cat > "$CODEXCTL_DEST" << SCRIPT
#!/usr/bin/env bash
# feishu-codexctl 本地管理入口（由 install.sh 生成）
ENV_FILE="$ENV_FILE"
if [ -f "\$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    . "\$ENV_FILE"
    set +a
fi
export FC_CONFIG_DIR="$CONFIG_DIR"
export FC_DATA_DIR="$DATA_DIR"
exec "$VENV_DIR/bin/feishu-codexctl" "\$@"
SCRIPT

chmod +x "$CODEXCTL_DEST"
_green "  ✓ 本地管理 CLI 已安装: $CODEXCTL_DEST"

cat > "$FCODEX_DEST" << SCRIPT
#!/usr/bin/env bash
# fcodex 本地 wrapper（由 install.sh 生成）
ENV_FILE="$ENV_FILE"
if [ -f "\$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    . "\$ENV_FILE"
    set +a
fi
export FC_CONFIG_DIR="$CONFIG_DIR"
export FC_DATA_DIR="$DATA_DIR"
exec "$VENV_DIR/bin/fcodex" "\$@"
SCRIPT

chmod +x "$FCODEX_DEST"
_green "  ✓ 本地共享 backend wrapper 已安装: $FCODEX_DEST"

echo ""
echo "[ 6/6 ] 安装完成！"
echo ""
_bold "下一步操作："
echo ""
echo "  1. 编辑飞书应用凭证："
echo "     \$ nano ~/.config/feishu-codex/system.yaml"
echo "     （填入 app_id 和 app_secret）"
echo ""
echo "  2. 按需调整 Codex 配置："
echo "     \$ nano ~/.config/feishu-codex/codex.yaml"
echo ""
echo "  3. 如需 provider API Key，请写入："
echo "     \$ nano ~/.config/environment.d/90-codex.conf"
echo ""
echo "  4. 查看初始化口令："
echo "     \$ cat ~/.config/feishu-codex/init.token"
echo ""
echo "  5. 启动服务："
echo "     \$ feishu-codex start"
echo ""
echo "  6. 私聊机器人执行："
echo "     \$ /init <上一步 token>"
echo ""
echo "  7. 查看日志确认连接成功："
echo "     \$ feishu-codex log"
echo ""
echo "  8. 本地若要与飞书安全共用同一线程，请使用："
echo "     \$ fcodex"
echo "     或 \$ fcodex resume <thread_id>"
echo ""

if [[ ":$PATH:" != *":$HOME/.local/bin:"* ]]; then
    _yellow "注意: ~/.local/bin 不在您的 PATH 中。"
    _yellow "请将以下内容添加到 ~/.bashrc 或 ~/.zshrc："
    _yellow "  export PATH=\"\$HOME/.local/bin:\$PATH\""
    _yellow "然后运行: source ~/.bashrc"
    echo ""
fi
