#!/usr/bin/env bash
set -euo pipefail

EXPECTED_UV_VERSION="0.8.14"
LOCK_COMPILE_COMMAND="bash scripts/lock-python-dependencies.sh"

usage() {
  printf '%s\n' \
    "用法：bash scripts/lock-python-dependencies.sh [--upgrade]" \
    "" \
    "默认：按现有 lock 版本偏好重新解析，用于一致性检查。" \
    "--upgrade：忽略现有 pins，显式升级全部可升级依赖。"
}

upgrade=false
case "$#" in
  0)
    ;;
  1)
    case "$1" in
      --upgrade)
        upgrade=true
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

actual_uv_version=""
if command -v uv >/dev/null 2>&1; then
  uv_version_output="$(uv --version 2>/dev/null || true)"
  actual_uv_version="${uv_version_output#uv }"
  actual_uv_version="${actual_uv_version%% *}"
fi
if [[ "$actual_uv_version" != "$EXPECTED_UV_VERSION" ]]; then
  echo "需要 uv $EXPECTED_UV_VERSION 生成 Python lock；当前为 ${actual_uv_version:-missing}。" >&2
  exit 1
fi

repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo_dir"

compile_options=(
  --quiet
  --universal
  --python-version 3.11
  --no-emit-index-url
  --custom-compile-command "$LOCK_COMPILE_COMMAND"
)
if [[ "$upgrade" == true ]]; then
  compile_options+=(--upgrade)
fi

uv pip compile \
  "${compile_options[@]}" \
  --output-file requirements.lock \
  pyproject.toml \
  requirements-build.in

uv pip compile \
  "${compile_options[@]}" \
  --output-file requirements-dev.lock \
  pyproject.toml \
  requirements-build.in \
  requirements-dev.in
