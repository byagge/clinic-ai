#!/usr/bin/env bash
# Синхронизация общего ядра между clinic-ai и edu-ai.
# См. docs/CORE_SYNC.md
set -euo pipefail

TARGET="${1:-}"
if [[ -z "$TARGET" ]]; then
  echo "usage: $0 <путь к другому репозиторию>  # например ../edu-ai" >&2
  exit 1
fi
if [[ ! -d "$TARGET/backend/arix" ]]; then
  echo "не похоже на репозиторий ARIX: $TARGET" >&2
  exit 1
fi

SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Каталоги и файлы, одинаковые в обоих репозиториях.
DIRS=(
  backend/arix/core
  backend/arix/db
  backend/arix/channels
  backend/arix/domain
  backend/arix/agent
  backend/arix/kb
  backend/arix/scheduler
  waweb/src
)
FILES=(
  backend/arix/config.py
  backend/arix/logging_setup.py
  backend/arix/ops.py
  backend/arix/payments.py
  backend/arix/stt.py
  backend/arix/templates/__init__.py
  waweb/package.json
  waweb/Dockerfile
  scripts/init-db.sql
  scripts/sync-core.sh
)

echo "ядро: $SOURCE → $TARGET"

for dir in "${DIRS[@]}"; do
  if [[ -d "$SOURCE/$dir" ]]; then
    mkdir -p "$TARGET/$dir"
    rsync -a --delete --exclude '__pycache__' "$SOURCE/$dir/" "$TARGET/$dir/"
    echo "  ✓ $dir"
  fi
done

for file in "${FILES[@]}"; do
  if [[ -f "$SOURCE/$file" ]]; then
    mkdir -p "$(dirname "$TARGET/$file")"
    cp "$SOURCE/$file" "$TARGET/$file"
    echo "  ✓ $file"
  fi
done

echo
echo "НЕ синхронизируются (они вертикальные):"
echo "  backend/arix/verticals/, backend/arix/seed/, backend/arix/eval/,"
echo "  backend/arix/bots/, backend/arix/api/, backend/alembic/, backend/tests/"
echo
echo "Изменения в $TARGET:"
git -C "$TARGET" status --short || true
echo
echo "Дальше: cd $TARGET && make test"
