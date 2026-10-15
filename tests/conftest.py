"""测试前置：给 main.py 提供最小 astrbot 桩，并按包导入插件入口模块。

关键约束（ST-05 / R-IND-08）：
1. **真 astrbot 存在时绝不注册桩**——否则会遮蔽真框架，让 CI 与开发机的结论失真；
2. 桩只在本仓库目录名（含中文）下按包导入，避免与其它插件互相污染。
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
import types
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_NAME = PLUGIN_ROOT.name


def _module(name: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.__path__ = []  # 让子模块导入成立
    sys.modules[name] = mod
    return mod


class _Flag:
    """假枚举成员：支持 `|` 组合（真框架的 EventMessageType 是枚举）。"""

    def __init__(self, value):
        self.value = value

    def __or__(self, other):
        merged = _Flag(self.value)
        merged.members = set(getattr(self, "members", {self.value})) | set(
            getattr(other, "members", {getattr(other, "value", other)})
        )
        return merged

    def __ror__(self, other):
        return self.__or__(other)


class _FilterDecorator:
    """只提供被 main.py 用到的装饰器：原样返回函数（与真框架行为一致）。"""

    class PlatformAdapterType:
        QQOFFICIAL = 1
        QQOFFICIAL_WEBHOOK = 2
        AIOCQHTTP = 4

    class EventMessageType:
        GROUP_MESSAGE = _Flag("GroupMessage")
        PRIVATE_MESSAGE = _Flag("PrivateMessage")

    def llm_tool(self, *args, **kwargs):
        def wrapper(func):
            return func

        if args and callable(args[0]):
            return args[0]
        return wrapper

    def platform_adapter_type(self, *args, **kwargs):
        return self._passthrough

    def event_message_type(self, *args, **kwargs):
        return self._passthrough

    @staticmethod
    def _passthrough(func):
        return func


class _Sticker:
    def __init__(self, context=None, config=None) -> None:
        self.context = context


class _Image:
    def __init__(self, file: str = "", **kwargs) -> None:
        self.file = file

    @staticmethod
    def fromFileSystem(path, **kwargs):
        return _Image(str(path))

    @staticmethod
    def fromURL(url, **kwargs):
        return _Image(str(url))


class _Plain(_Image):
    def __init__(self, text: str = "", **kwargs) -> None:
        super().__init__(str(text))
        self.text = text


class _MessageChain:
    def __init__(self, chain=None, **kwargs) -> None:
        self.chain = list(chain or [])


class _Node(_Image):
    pass


class _Nodes(list):
    def __init__(self, nodes=None, **kwargs) -> None:
        super().__init__(nodes or [])


class _Request:
    """替掉 astrbot.api.web.request：测试里可替换为返回固定 JSON 的假对象。"""

    async def json(self, default=None):
        return default


def _install_astrbot_stub() -> bool:
    """没装真 astrbot 时才注册桩；返回是否注册了桩。"""
    if importlib.util.find_spec("astrbot") is not None:
        return False
    astrbot = _module("astrbot")
    api = _module("astrbot.api")
    api.AstrBotConfig = dict
    api.logger = logging.getLogger("astrbot-stub")
    astrbot.api = api

    event_mod = _module("astrbot.api.event")
    event_mod.filter = _FilterDecorator()
    event_mod.AstrMessageEvent = type("AstrMessageEvent", (), {})
    # 真框架里 MessageChain 由 astrbot.api.event 再导出（message_components 里没有）
    event_mod.MessageChain = _MessageChain
    api.event = event_mod

    components = _module("astrbot.api.message_components")
    components.Image = _Image
    components.Plain = _Plain
    components.Reply = type("Reply", (_Image,), {})
    components.Forward = type("Forward", (_Image,), {})
    components.Node = _Node
    components.Nodes = _Nodes
    api.message_components = components

    star = _module("astrbot.api.star")
    star.Star = _Sticker
    star.Context = type("Context", (), {})
    api.star = star

    web = _module("astrbot.api.web")
    web.error_response = lambda message="", **kwargs: {"status": "error", "message": message}
    # 真 json_response 把传入的对象原样序列化（插件自己带了 status/data 包装）
    # 真 json_response 把传入对象原样序列化（不额外包 status/data）
    web.json_response = lambda payload=None, **kwargs: payload
    web.file_response = lambda *args, **kwargs: {"status": "success", "file": True}
    web.request = _Request()
    api.web = web
    return True


USING_STUB = _install_astrbot_stub()


def import_main():
    """按包导入插件入口模块（目录名含中文，需要父目录在 sys.path 上）。"""
    parent = str(PLUGIN_ROOT.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    if str(PLUGIN_ROOT) not in sys.path:
        sys.path.insert(0, str(PLUGIN_ROOT))
    return importlib.import_module(f"{PLUGIN_NAME}.main")
