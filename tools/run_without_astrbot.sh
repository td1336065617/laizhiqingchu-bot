#!/usr/bin/env bash
# 在"没有真 astrbot"的条件下跑测试：验证 tests/conftest.py 的桩是否真的可用。
# 用法：PYTHON=/path/to/python bash tools/run_without_astrbot.sh [--all] [pytest 参数...]
#   默认只跑 AI 相关用例（桩应完全覆盖 → 退出码 0）；
#   --all 跑整仓：仓库既有的 platform_compat 用例需要真框架组件，会失败——属已知边界。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
MODE=ai
if [ "${1:-}" = "--all" ]; then MODE=all; shift; fi
cd "$ROOT"
AI_TESTS=(tests/test_ai_tools_sticker.py tests/test_sticker_refactor.py tests/test_ai_compliance.py tests/test_ai_isolation.py tests/test_ai_contract.py)
echo "== 强制无 astrbot（注入 find_spec 钩子，verify: find_spec 应为 None）=="
PYTHONPATH="$ROOT/tools/no_astrbot_hook" "$PYTHON" -c "import importlib.util, sys; print('find_spec(astrbot) =', importlib.util.find_spec('astrbot'))"
if [ "$MODE" = all ]; then
  PYTHONPATH="$ROOT/tools/no_astrbot_hook" "$PYTHON" -m pytest -q "$@" || true
  echo "== 提示：platform_compat 用例需要真框架组件，失败属已知边界 =="
  exit 0
fi
PYTHONPATH="$ROOT/tools/no_astrbot_hook" "$PYTHON" -m pytest -q "${AI_TESTS[@]}" "$@"
