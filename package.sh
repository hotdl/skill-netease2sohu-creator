#!/usr/bin/env bash
# 把技能打包到项目根目录：sohu-article-creator.zip
# 用法：./package.sh
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$ROOT_DIR/skills/sohu-article-creator"
OUTPUT="$ROOT_DIR/sohu-article-creator.zip"

# skills/sohu-article-creator 通常是软链；软链不存在时回退到真实目录。
if [[ ! -d "$SKILL_DIR" ]]; then
  SKILL_DIR="$ROOT_DIR/.claude/skills/sohu-article-creator"
fi

if [[ ! -f "$SKILL_DIR/SKILL.md" ]]; then
  echo "错误：找不到技能目录 $SKILL_DIR" >&2
  exit 1
fi

for cmd in rsync zip; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "错误：需要先安装 $cmd" >&2
    exit 1
  fi
done

tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

stage="$tmp_dir/sohu-article-creator"
mkdir -p "$stage"

# 先复制到临时目录再压缩，确保不把 tests、缓存和编译产物带进包里，
# 也不会因为旧 zip 已存在而残留上次的条目。
rsync -a \
  --exclude='tests/' \
  --exclude='__pycache__/' \
  --exclude='*.py[cod]' \
  --exclude='.pytest_cache/' \
  --exclude='.mypy_cache/' \
  --exclude='.ruff_cache/' \
  --exclude='.coverage' \
  --exclude='htmlcov/' \
  --exclude='*.egg-info/' \
  --exclude='build/' \
  --exclude='dist/' \
  --exclude='.venv/' \
  --exclude='venv/' \
  --exclude='node_modules/' \
  --exclude='.DS_Store' \
  "$SKILL_DIR/" "$stage/"

(
  cd "$tmp_dir"
  zip -q -r -X package.zip sohu-article-creator
)

mv -f "$tmp_dir/package.zip" "$OUTPUT"
echo "已打包：$OUTPUT"
