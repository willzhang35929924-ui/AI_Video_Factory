#!/bin/zsh
set -eu
FACTORY_ROOT="$(cd -- "$(dirname -- "$0")/.." && pwd)"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
FACTORY_PYTHON="$FACTORY_ROOT/backend/.venv/bin/python"
if [[ ! -x "$FACTORY_PYTHON" ]]; then
  print -u2 "缺少 $FACTORY_PYTHON；没有关闭任何进程。"
  exit 1
fi
"$FACTORY_PYTHON" "$FACTORY_ROOT/scripts/factory_launcher.py" stop "$@"
