"""让 `importlib.util.find_spec("astrbot")` 恒为 None。

用途：验证 `tests/conftest.py` 的 astrbot 桩**在真的没有框架时**是否可用。
由 tools/run_without_astrbot.sh 通过 PYTHONPATH 注入。
"""

import importlib.util

_real_find_spec = importlib.util.find_spec


def _fake_find_spec(name, *args, **kwargs):
    if name == "astrbot" or str(name).startswith("astrbot."):
        return None
    return _real_find_spec(name, *args, **kwargs)


importlib.util.find_spec = _fake_find_spec
