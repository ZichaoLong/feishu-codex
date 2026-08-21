#!/usr/bin/env bash
set -euo pipefail

PYTHON=""

select_supported_python() {
  local candidate="$1"
  local resolved=""
  if [[ "$candidate" == */* ]]; then
    resolved="$candidate"
  else
    resolved="$(command -v "$candidate" 2>/dev/null || true)"
  fi
  [[ -n "$resolved" && -x "$resolved" ]] || return 1
  "$resolved" -c \
    'import platform, sys; raise SystemExit(0 if platform.python_implementation() == "CPython" and sys.version_info >= (3, 11) else 1)' \
    >/dev/null 2>&1 || return 1
  PYTHON="$resolved"
}

if [[ -n "${FOCUS_INSTALL_PYTHON:-}" ]]; then
  if ! select_supported_python "$FOCUS_INSTALL_PYTHON"; then
    echo "FOCUS_INSTALL_PYTHON 指定的解释器不可用，或不是 CPython 3.11+：$FOCUS_INSTALL_PYTHON" >&2
    exit 1
  fi
else
  for candidate in python3.14 python3.13 python3.12 python3.11 python3 python; do
    if select_supported_python "$candidate"; then
      break
    fi
  done

  if [[ -z "$PYTHON" ]]; then
    IFS=: read -r -a focus_install_path_dirs <<< "${PATH:-}"
    for path_dir in "${focus_install_path_dirs[@]}"; do
      [[ -n "$path_dir" ]] || path_dir="."
      for candidate_path in "$path_dir"/python3.*; do
        candidate_name="${candidate_path##*/}"
        [[ "$candidate_name" =~ ^python3\.[0-9]+$ ]] || continue
        if select_supported_python "$candidate_path"; then
          break 2
        fi
      done
    done
  fi

  if [[ -z "$PYTHON" ]]; then
    echo "需要可执行的 CPython 3.11 或更高版本；也可设置 FOCUS_INSTALL_PYTHON=/path/to/python。" >&2
    exit 1
  fi
fi

exec "$PYTHON" "$(cd "$(dirname "$0")" && pwd)/install.py" "$@"
