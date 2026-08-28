#!/usr/bin/env bash

set -euo pipefail

usage() {
  printf '%s\n' \
    "用法：bash scripts/install_workspace.sh [--migrate-from-feishu-codex]" \
    "" \
    "构建当前 workspace 的 Web production assets 与临时 local bundle，" \
    "再通过仓库正式 install.sh 安装；临时 bundle 会在安装返回后删除。" \
    "" \
    "可选迁移参数会原样转发给 install.sh；artifact 来源始终是当前 workspace。" \
    "首次 clone、web/package-lock.json 变化或 node_modules 缺失时，请先运行：" \
    "  npm --prefix web ci"
}

installer_args=()
case "$#" in
  0)
    ;;
  1)
    case "$1" in
      --migrate-from-feishu-codex)
        installer_args=("$1")
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        usage >&2
        exit 2
        ;;
    esac
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

workspace_python=""
select_workspace_python() {
  local candidate
  local -a candidates
  if [[ -n "${FOCUS_INSTALL_PYTHON:-}" ]]; then
    candidates=("$FOCUS_INSTALL_PYTHON")
  else
    candidates=(python python3 python3.14 python3.13 python3.12 python3.11)
  fi

  for candidate in "${candidates[@]}"; do
    if "$candidate" -c \
      'import platform, sys; raise SystemExit(0 if platform.python_implementation() == "CPython" and sys.version_info >= (3, 11) else 1)' \
      >/dev/null 2>&1; then
      workspace_python="$candidate"
      return 0
    fi
  done
  return 1
}

if ! select_workspace_python; then
  echo "构建 workspace bundle 需要可执行的 CPython 3.11+；也可设置 FOCUS_INSTALL_PYTHON。" >&2
  exit 1
fi

npm --prefix "$repo_root/web" run build

workspace_artifact_dir="$(mktemp -d "${TMPDIR:-/tmp}/focus-workspace-install.XXXXXX")"
cleanup_workspace_artifacts() {
  if [[ -n "${workspace_artifact_dir:-}" && -d "$workspace_artifact_dir" ]]; then
    rm -rf -- "$workspace_artifact_dir"
  fi
}
trap cleanup_workspace_artifacts EXIT

"$workspace_python" "$repo_root/scripts/build_install_bundle.py" \
  --output-dir "$workspace_artifact_dir"

shopt -s nullglob
workspace_bundles=("$workspace_artifact_dir"/focus-install-*.zip)
shopt -u nullglob
if [[ "${#workspace_bundles[@]}" -ne 1 || ! -f "${workspace_bundles[0]}" ]]; then
  echo "workspace bundle 构建后必须恰好产生一个 Focus ZIP。" >&2
  exit 1
fi

bash "$repo_root/install.sh" --artifact "${workspace_bundles[0]}" "${installer_args[@]}"
