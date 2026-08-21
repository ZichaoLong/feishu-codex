#!/usr/bin/env bash

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

failures=0

fail() {
  echo "doc-check: $*" >&2
  failures=1
}

check_legacy_doc_paths() {
  local matches
  matches="$(
    git grep -nE \
      'docs/(feishu-codex-design|feishu-thread-lifecycle|runtime-control-surface|thread-profile-semantics|shared-backend-resume-safety|fcodex-shared-backend-runtime|codex-handler-decomposition-plan|group-chat-manual-test-checklist|feishu-help-navigation|codex-permissions-model|group-chat-contract)(\.zh-CN)?\.md' \
      -- README.md docs ':(exclude)docs/_work/**' || true
  )"
  if [[ -n "$matches" ]]; then
    fail "found legacy top-level docs paths:"
    printf '%s\n' "$matches" >&2
  fi
}

check_docs_root_layout() {
  local root_files
  root_files="$(find docs -maxdepth 1 -type f | sort)"
  local expected=$'docs/doc-index.md\ndocs/doc-index.zh-CN.md'
  if [[ "$root_files" != "$expected" ]]; then
    fail "unexpected tracked-style docs root files; expected only doc-index files."
    printf 'found:\n%s\n' "$root_files" >&2
  fi
}

check_bilingual_pairs() {
  local dir
  for dir in docs/architecture docs/contracts docs/decisions docs/archive; do
    while IFS= read -r file; do
      [[ -n "$file" ]] || continue
      if [[ "$file" == *.zh-CN.md ]]; then
        local peer="${file%.zh-CN.md}.md"
        [[ -f "$peer" ]] || fail "missing English peer for $file"
      else
        local peer="${file%.md}.zh-CN.md"
        [[ -f "$peer" ]] || fail "missing Chinese peer for $file"
      fi
    done < <(find "$dir" -maxdepth 1 -type f -name '*.md' | sort)
  done
}

check_active_bilingual_roles() {
  local dir
  for dir in docs/architecture docs/contracts docs/decisions; do
    while IFS= read -r chinese; do
      [[ -n "$chinese" ]] || continue
      local english="${chinese%.zh-CN.md}.md"
      [[ -f "$english" ]] || continue

      local expected_chinese="文档角色：中文规范源。英文同步副本：\`${english}\`。"
      local expected_english="Document role: synchronized English peer. Canonical Chinese: \`${chinese}\`."
      local actual_chinese
      local actual_english
      actual_chinese="$(sed -n '3p' "$chinese")"
      actual_english="$(sed -n '3p' "$english")"

      if [[ "$actual_chinese" != "$expected_chinese" ]]; then
        fail "invalid Chinese canonical-role metadata in $chinese; expected: $expected_chinese"
      fi
      if [[ "$actual_english" != "$expected_english" ]]; then
        fail "invalid English peer-role metadata in $english; expected: $expected_english"
      fi
    done < <(find "$dir" -maxdepth 1 -type f -name '*.zh-CN.md' | sort)
  done

  local contradictory_roles
  contradictory_roles="$(
    grep -nHE \
      '^(英文原文|英文译文|英文版本|中文原文|中文译文|中文版本|Chinese original|Chinese version|English original|English version)[：:]' \
      docs/architecture/*.md docs/contracts/*.md docs/decisions/*.md || true
  )"
  if [[ -n "$contradictory_roles" ]]; then
    fail "found ambiguous or contradictory bilingual-role claims in active docs:"
    printf '%s\n' "$contradictory_roles" >&2
  fi
}

check_doc_indexes_cover_docs() {
  local file
  for file in docs/architecture/*.md docs/contracts/*.md docs/decisions/*.md docs/archive/*.md; do
    local relative="./${file#docs/}"
    if [[ "$file" == *.zh-CN.md ]]; then
      grep -Fq "$relative" docs/doc-index.zh-CN.md || fail "missing from docs/doc-index.zh-CN.md: $relative"
    else
      grep -Fq "$relative" docs/doc-index.md || fail "missing from docs/doc-index.md: $relative"
    fi
  done

  for file in docs/verification/*.md; do
    local relative="./${file#docs/}"
    grep -Fq "$relative" docs/doc-index.md || fail "missing from docs/doc-index.md: $relative"
    grep -Fq "$relative" docs/doc-index.zh-CN.md || fail "missing from docs/doc-index.zh-CN.md: $relative"
  done
}

check_readme_doc_targets_exist() {
  local target
  while IFS= read -r target; do
    [[ -n "$target" ]] || continue
    [[ -f "$target" ]] || fail "README references missing doc target: $target"
  done < <(grep -oE 'docs/[A-Za-z0-9_./-]+\.md' README.md | sort -u)
}

check_markdown_trailing_whitespace() {
  local matches
  matches="$(
    git grep -nI '[[:blank:]]$' -- README.md docs '*.md' ':(exclude)docs/_work/**' || true
  )"
  if [[ -n "$matches" ]]; then
    fail "found trailing whitespace in markdown files:"
    printf '%s\n' "$matches" >&2
  fi
}

check_portable_upstream_references() {
  if ! python scripts/check_portable_upstream_references.py; then
    failures=1
  fi
}

check_document_integrity() {
  if ! python scripts/check_document_integrity.py; then
    failures=1
  fi
}

check_legacy_doc_paths
check_docs_root_layout
check_bilingual_pairs
check_active_bilingual_roles
check_doc_indexes_cover_docs
check_readme_doc_targets_exist
check_markdown_trailing_whitespace
check_portable_upstream_references
check_document_integrity

if (( failures != 0 )); then
  exit 1
fi

echo "doc-check: ok"
