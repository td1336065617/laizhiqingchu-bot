"""AstrBot QQ 群表情包管理插件（StickerPlugin）。

指令（精确前缀触发，不匹配的消息完全忽略）：
- 批量添加合并转发{关键词}  先发送命令，90 秒内本人发送的合并转发自动批量入库（QQ 官方推送为摘要文本，自动解析其中的图片）
- 来只{关键词}        随机发送一张该关键词下的图片
- 列表{关键词}{页码}   仅管理员，分页查看（每页 10 张）
- 删图                仅管理员，回复图片按 MD5 自动定位删除；或 删图{关键词}{序号} 按序号删除
- 删除{关键词}        仅管理员，二次确认（60 秒）后删除整个关键词目录
- 统计              仅管理员，查看关键词数、图片总数、Top 3
- 表情包管理菜单      所有人，查看指令与权限说明
- 屏蔽{关键词}       仅管理员，屏蔽关键词（禁止添加）
- 屏蔽列表          仅管理员，查看屏蔽关键词列表
- 解除屏蔽{关键词}    仅管理员，解除关键词屏蔽

WebUI 后台：在 AstrBot 插件页面的“表情包管理”页中统一管理管理员、存储上限与备份恢复。

数据存储：
- data/stickers/index.json           关键词 -> 文件名列表索引
- data/stickers/{关键词}/{文件名}     图片文件
"""

import asyncio
import hashlib
import json
import os
import random
import re
import shutil
import time
import zipfile
from pathlib import Path
from typing import List, Optional, Tuple

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Reply
from astrbot.api.star import Context, Star
from astrbot.api.web import error_response, file_response, json_response, request

try:  # 同目录模块：包内导入时用相对导入，脚本/测试直跑时回落到绝对导入
    from . import sticker_store as _store_mod
except ImportError:  # pragma: no cover
    import sticker_store as _store_mod

from . import platform_compat
from .menu_renderer import StickerMenuRenderer

try:                      # AI 公共件（纯逻辑，四仓库各一份）
    from . import ai_common, ai_impl
except ImportError:       # 测试里以顶层模块导入
    import ai_common
    import ai_impl

# 与 metadata.yaml 的 name 保持一致；注册的 Web API 路由必须带插件名前缀
PLUGIN_NAME = "sticker_plugin"

# 与指令名重名的关键词一律不合法
RESERVED_KEYWORDS = {
    "添加",
    "批量添加合并转发",
    "来只",
    "列表",
    "删图",
    "删除",
    "统计",
    "表情包管理菜单",
    "屏蔽",
    "屏蔽列表",
    "解除屏蔽",
    "绑定",
    "解绑",
    "别名",
    "授予管理员",
    "撤销管理员",
    "管理员列表",
}
# 二次删除确认的有效期（秒）
# 平台占位 ID：官方适配器在取不到真实 id 时会塞这些值（不可当管理员）
ADMIN_ID_PLACEHOLDERS = {"", "all", "qq_official", "unknown_selfid"}
# 正文里的 @ 记号（官方通道 @他人不产生 At 组件，只留字面量）
AT_ID_RE = re.compile(r"<@!?([A-Za-z0-9_-]{6,})>")
CONFIRM_TTL = 60
# “先发添加指令、再发合并转发”模式的等待有效期（秒）
FORWARD_ADD_TTL = 90
# QQ 官方机器人“聊天记录（合并转发）”消息类型
QQ_FORWARD_MESSAGE_TYPE = 102
# 102 摘要文本中的附件行：URL 提取正则
FORWARD_URL_RE = re.compile(r"URL:\s*(https?://\S+)")
# 102 摘要文本中的附件行标记（[附件1] / [图片]）
FORWARD_SUMMARY_MARKERS = ("[附件", "[图片]")
# 合并转发元素中需要递归展开的嵌套键
FORWARD_NESTED_KEYS = ("msg_elements", "elements", "messages", "nodes", "records", "record")
# 列表分页大小
PAGE_SIZE = 10
# 默认存储上限（MB），可通过 _conf_schema.json 的 max_storage_mb 覆盖
DEFAULT_MAX_STORAGE_MB = 200
# 默认备份保留：份数上限与总量上限（MB）。pre-restore_* 永不参与自动清理
# 单图上限（MB）：线上 93% 的图 <1MB，10MB 只影响 3 个文件（0.13%）
DEFAULT_MAX_IMAGE_MB = 10
DEFAULT_BACKUP_KEEP = 2
DEFAULT_BACKUP_TOTAL_MB = 1024
# 允许保留的图片扩展名（convert_to_file_path 可能返回无后缀的临时文件）
ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

#: AI 能力默认值（插件自管配置，读写都在 WebUI 页面）
AI_CONFIG_DEFAULTS: dict = {
    "sticker_ai_tools_enabled": True,
    "sticker_ai_read_enabled": True,      # 统计 / 列表
    "sticker_ai_send_enabled": True,      # 随机发表情包（会改变聊天风格，可关）
    "sticker_ai_throttle_limit": 3,       # 发送类节流：窗口内最多几次
    "sticker_ai_throttle_window": 60,
    "sticker_ai_send_dedup_window": 10,   # 同会话同关键词多少秒内不重复发图
}
AI_BOOL_KEYS = (
    "sticker_ai_tools_enabled",
    "sticker_ai_read_enabled",
    "sticker_ai_send_enabled",
)
AI_INT_BOUNDS: dict = {
    "sticker_ai_throttle_limit": (1, 60),
    "sticker_ai_throttle_window": (5, 3600),
    "sticker_ai_send_dedup_window": (0, 600),
}

MENU_TEXT = "\n".join(
    (
        "🎴 表情包管理菜单",
        "━━━━━━━━━━━━",
        "👥 所有人可用",
        "• 添加{关键词} ─ 回复图片批量入库",
        "• 批量添加合并转发{关键词} ─ 先发命令，90秒内本人发合并转发自动批量入库",
        "• 来只{关键词} ─ 随机发送一张该关键词的图片",
        "• 表情包管理菜单 ─ 显示本菜单",
        "━━━━━━━━━━━━",
        "🔑 仅管理员",
        "• 屏蔽{关键词} ─ 屏蔽关键词（禁止添加/发送/查看）",
        "• 屏蔽列表 ─ 查看屏蔽关键词",
        "• 解除屏蔽{关键词} ─ 解除屏蔽",
        "• 列表{关键词}{页码} ─ 分页查看（每页10张）",
        "• 删图 ─ 回复图片，按 MD5 自动定位并删除",
        "• 删图{关键词} ─ 仅在该关键词下按 MD5 删除",
        "• 删图{关键词}{序号} ─ 按序号删除单张",
        "• 删除{关键词} ─ 二次确认后删除整个关键词",
        "• 统计 ─ 关键词数 / 图片总数 / Top3",
        "━━━━━━━━━━━━",
        "⚙️ 备份/恢复/管理员设置：WebUI 表情包管理页",
        "🔗 开源：https://github.com/td1336065617/laizhiqingchu-bot",
    )
)


def _resolve_data_dir() -> Path:
    """优先按官方规范解析 AstrBot 数据目录，最终落到 <数据目录>/stickers。"""
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        base = Path(get_astrbot_data_path())
    except Exception:
        base = Path("data")
    return base / "stickers"


def _resolve_config_path() -> Path:
    """插件自管理配置路径，与 AstrBot 原生插件配置位置一致。"""
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        base = Path(get_astrbot_data_path())
    except Exception:
        base = Path("data")
    return base / "config" / "sticker_plugin_config.json"


def _atomic_write_json(path: Path, payload) -> None:
    """原子写 JSON：同目录 tmp + fsync + os.replace。

    为什么不用 write_text：直接截断覆盖时，崩溃/磁盘满/并发写会把文件留成半截，
    索引也就跟着坏了。os.replace 在同一文件系统上是原子操作，读到的永远是
    完整的旧版本或完整的新版本。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # tmp 名必须唯一：固定名会让两个写者共用一个临时文件，
    # 一个截断另一个的中间结果，rename 后就可能落地半截内容。
    tmp = path.with_name(f"{path.name}.{os.getpid()}-{time.time_ns()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        # 失败时保留 tmp 供诊断，但绝不覆盖目标文件
        raise


class _PluginConfig(dict):
    """无 _conf_schema.json 时插件自管理的配置对象，读写 data/config 下的配置文件。"""

    def __init__(self, config_path: Path):
        super().__init__()
        self.config_path = config_path
        try:
            if config_path.is_file():
                with open(config_path, encoding="utf-8-sig") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self.update(data)
        except Exception as exc:
            logger.error("加载插件配置失败: %s", exc)

    def save_config(self) -> None:
        try:
            _atomic_write_json(self.config_path, dict(self))
        except Exception as exc:
            logger.error("保存插件配置失败: %s", exc)
            raise


class StickerPlugin(Star):
    def __init__(self, context: Context, config: Optional[AstrBotConfig] = None):
        super().__init__(context)
        # 插件不再提供 _conf_schema.json（与内置插件一致，后台集中在 WebUI 页面）；
        # AstrBot 此时不会传入 config，插件自行读写配置文件。
        self.config = config if isinstance(config, dict) else _PluginConfig(
            _resolve_config_path()
        )
        self.data_dir = _resolve_data_dir()
        self.index_path = self.data_dir / "index.json"
        self.backup_root = self.data_dir.parent / "sticker_backups"
        self.blocked_path = self.data_dir / "blocked_keywords.json"
        self.menu_renderer = StickerMenuRenderer(
            cache_dir=self.data_dir.parent / "sticker_menu_cache"
        )
        # 索引结构：{"关键词": ["文件名1.png", ...]}
        self.index: dict = {}
        # 批量删除二次确认：{user_id: {"keyword": str, "time": float}}
        self.pending_delete: dict = {}
        # 屏蔽关键词集合
        self.blocked_keywords: set = set()
        # 等待合并转发的添加状态：{key: {"keyword": str, "time": float}}
        self.pending_forward_add: dict = {}
        # AI 工具：节流器 + 发送幂等记录（同会话同关键词窗口内不重复发图）
        self._ai_throttle = None
        self._ai_send_log: dict = {}
        self._store = None
        # 顺序要紧：先读 JSON（迁移源），再决定用 SQLite 还是继续纯 JSON
        self._load_index()
        # 别名组：{主词: [别名...]}（旁挂文件，不进 index.json）
        self.alias_path = self.data_dir / "keyword_groups.json"
        self.alias_groups: dict = {}
        self.alias_map: dict = {}
        self._load_groups()
        self._init_storage()
        self._load_blocked()
        self._check_storage()
        logger.info(
            "StickerPlugin 管理员列表: %s",
            [str(a) for a in (self.config.get("admin_users", []) or [])],
        )
        try:
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/backup",
                self._web_backup,
                ["GET"],
                "表情包数据备份（生成压缩包）",
            )
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/maintenance/scan",
                self._web_maintenance_scan,
                ["GET"],
                "孤儿对账：磁盘与索引双向差异（只读）",
            )
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/maintenance/dedupe",
                self._web_maintenance_dedupe,
                ["POST"],
                "按内容去重（默认干跑，apply=true 才执行硬链接归并）",
            )
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/backup/progress",
                self._web_backup_progress,
                ["GET"],
                "备份进度（供页面轮询）",
            )
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/backup/export",
                self._web_export,
                ["GET"],
                "按关键词导出备份（小卷，便于下载）",
            )
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/backups",
                self._web_backups,
                ["GET"],
                "表情包备份列表",
            )
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/backup/download",
                self._web_download,
                ["GET"],
                "下载表情包备份压缩包",
            )
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/backup/delete",
                self._web_backup_delete,
                ["POST"],
                "删除表情包备份压缩包",
            )
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/restore",
                self._web_restore,
                ["POST"],
                "上传表情包备份压缩包并恢复",
            )
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/config",
                self._web_config_get,
                ["GET"],
                "获取插件后台配置（管理员列表/存储上限）",
            )
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/config",
                self._web_config_set,
                ["POST"],
                "保存插件后台配置（管理员列表/存储上限）",
            )
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/clear",
                self._web_clear,
                ["POST"],
                "清空全部表情包数据",
            )
        except Exception as exc:
            logger.error("StickerPlugin 注册 Web API 失败: %s", exc)

    # ------------------------------------------------------------------
    # 统一入口：精确前缀分发，未匹配的指令完全无视、不回复
    # ------------------------------------------------------------------
    @filter.platform_adapter_type(
        filter.PlatformAdapterType.QQOFFICIAL
        | filter.PlatformAdapterType.QQOFFICIAL_WEBHOOK
        | filter.PlatformAdapterType.AIOCQHTTP
    )
    @filter.event_message_type(
        filter.EventMessageType.GROUP_MESSAGE
        | filter.EventMessageType.PRIVATE_MESSAGE
    )
    async def on_message(self, event: AstrMessageEvent):
        message_str = (event.message_str or "").strip()
        # QQ 官方指令面板可能自动补上“/”；统一去掉一个前缀后再匹配。
        if message_str.startswith("/"):
            message_str = message_str[1:].lstrip()
        logger.info(
            "StickerPlugin 事件: platform=%s type=%s sender=%s group=%s self=%s msg=%r",
            event.get_platform_name(),
            event.get_message_type(),
            event.get_sender_id(),
            event.get_group_id(),
            event.get_self_id(),
            message_str,
        )
        chain_types = [
            type(c).__name__
            for c in (getattr(event.message_obj, "message", None) or [])
        ]
        logger.info("StickerPlugin 消息链: %s", chain_types)
        for comp in (getattr(event.message_obj, "message", None) or []):
            if isinstance(comp, Reply):
                reply_chain = getattr(comp, "chain", None) or []
                logger.info(
                    "StickerPlugin Reply: id=%s sender=%s chain=%s",
                    getattr(comp, "id", None),
                    getattr(comp, "sender_id", None),
                    [
                        f"{type(x).__name__}(file={getattr(x, 'file', None)},url={getattr(x, 'url', None)})"
                        for x in reply_chain
                    ],
                )
        raw = getattr(event.message_obj, "raw_message", None)
        if raw is not None:
            logger.info(
                "StickerPlugin 原始消息: type=%s ref=%s elements=%r",
                getattr(raw, "message_type", None),
                getattr(raw, "message_reference", None),
                getattr(raw, "msg_elements", None),
            )
        # 合并转发（message_type=102）到达时，若该群处于“等待合并转发”状态则批量添加
        if self._is_forward_event(event):
            async for result in self._process_forward_add(event):
                yield result
            return
        if not message_str:
            return

        try:
            if message_str.startswith("批量添加合并转发"):
                async for result in self._handle_batch_add_forward(event, message_str):
                    yield result
            elif message_str.startswith("添加"):
                async for result in self._handle_add(event, message_str):
                    yield result
            elif message_str.startswith("来只"):
                async for result in self._handle_send(event, message_str):
                    yield result
            elif message_str.startswith("列表"):
                async for result in self._handle_list(event, message_str):
                    yield result
            elif message_str.startswith("删图"):
                async for result in self._handle_delete_one(event, message_str):
                    yield result
            elif message_str.startswith("删除"):
                async for result in self._handle_delete_all(event, message_str):
                    yield result
            elif message_str.startswith("统计"):
                async for result in self._handle_stats(event, message_str):
                    yield result
            elif message_str.startswith("表情包管理菜单"):
                async for result in self._handle_menu(event, message_str):
                    yield result
            elif message_str.startswith("授予管理员"):
                async for result in self._handle_grant_admin(event, message_str):
                    yield result
            elif message_str.startswith("撤销管理员"):
                async for result in self._handle_revoke_admin(event, message_str):
                    yield result
            elif message_str.startswith("管理员列表"):
                async for result in self._handle_admin_list(event):
                    yield result
            elif message_str.startswith("绑定"):
                async for result in self._handle_bind(event, message_str):
                    yield result
            elif message_str.startswith("解绑"):
                async for result in self._handle_unbind(event, message_str):
                    yield result
            elif message_str.startswith("别名"):
                async for result in self._handle_alias_list(event, message_str):
                    yield result
            elif message_str.startswith("屏蔽列表"):
                async for result in self._handle_block_list(event, message_str):
                    yield result
            elif message_str.startswith("解除屏蔽"):
                async for result in self._handle_unblock(event, message_str):
                    yield result
            elif message_str.startswith("屏蔽"):
                async for result in self._handle_block(event, message_str):
                    yield result
            # 其余消息：完全忽略
        except Exception as exc:
            logger.error("StickerPlugin 处理消息异常: %s", exc, exc_info=True)
            yield event.plain_result("表情包插件处理出错，请查看日志")

    # ------------------------------------------------------------------
    # A. 添加（回复消息中包含图片才触发，否则完全无视）
    # ------------------------------------------------------------------
    async def _handle_add(self, event: AstrMessageEvent, message_str: str):
        keyword = message_str[2:].strip()
        raw_keyword = keyword
        keyword, _how = self._resolve_keyword(raw_keyword)
        if not keyword:
            yield event.plain_result('关键词 ' + raw_keyword + ' 不存在')
            return
        if not keyword:
            yield event.plain_result("用法：添加{关键词}（请回复包含图片的消息）")
            return
        if not self._is_valid_keyword(keyword):
            yield event.plain_result("关键词不合法（不能是 Windows 保留名，结尾也不能带点或空格）")
            return
        if keyword in self.blocked_keywords:
            yield event.plain_result(f"关键词 {keyword} 已被屏蔽，无法添加")
            return

        images = self._extract_reply_images(event)
        logger.info(
            "StickerPlugin 添加: 提取到引用图片 %s",
            None if images is None else len(images),
        )
        # 兜底：引用里没有图片（平台可能不下发引用内容）时，
        # 尝试使用当前消息自带的图片，例如“添加蓝色大肥鱼”+ 图片 同一条消息发送。
        if not images:
            message_chain = getattr(event.message_obj, "message", None) or []
            images = [c for c in message_chain if isinstance(c, Image)]
            if images:
                logger.info(
                    "StickerPlugin 添加: 引用无可用图片，改用当前消息中的 %s 张图片",
                    len(images),
                )
        if not images:
            images = self._extract_quoted_forward_images(event)
            if images:
                logger.info(
                    "StickerPlugin 添加: 从引用消息中解析出 %s 张合并转发图片",
                    len(images),
                )
        if not images:
            yield event.plain_result(
                "添加失败：未获取到图片。请回复包含图片的消息，"
                "或使用“批量添加合并转发{关键词}”后发送合并转发批量入库"
            )
            return

        added, skipped, failed, blocked = await self._add_images(keyword, images)
        if blocked:
            yield event.plain_result(f"添加失败：{blocked}")
            return
        if failed:
            yield event.plain_result(
                f"成功添加 {added} 张图片（跳过 {skipped} 张重复，{failed} 张下载失败）"
            )
        else:
            yield event.plain_result(f"成功添加 {added} 张图片（跳过 {skipped} 张重复）")

    async def _handle_batch_add_forward(
        self, event: AstrMessageEvent, message_str: str
    ):
        """批量添加合并转发：先发送命令，90 秒内本人发送的合并转发自动入库。"""
        raw_keyword = message_str[len("批量添加合并转发") :].strip()
        keyword, _how = self._resolve_keyword(raw_keyword)
        if not keyword:
            yield event.plain_result("关键词 " + raw_keyword + " 不存在，请先用 添加 建立")
        if not keyword:
            yield event.plain_result("用法：批量添加合并转发{关键词}")
            return
        if not self._is_valid_keyword(keyword):
            yield event.plain_result("关键词不合法（不能是 Windows 保留名，结尾也不能带点或空格）")
            return
        if keyword in self.blocked_keywords:
            yield event.plain_result(f"关键词 {keyword} 已被屏蔽，无法添加")
            return

        key = self._forward_key(event)
        self.pending_forward_add[key] = {"keyword": keyword, "time": time.time()}
        yield event.plain_result(
            f"已准备批量添加关键词 {keyword}：请在 {FORWARD_ADD_TTL} 秒内"
            "发送合并转发，将自动提取其中的图片入库"
        )

    async def _add_images(
        self, keyword: str, images: List[Image]
    ) -> Tuple[int, int, int, Optional[str]]:
        """把图片批量写入关键词目录（MD5 去重）。

        返回 (新增, 跳过重复, 下载失败, 拒绝原因)。拒绝原因随返回值走，
        不用实例变量：实例变量在并发请求下会串味（审计 M2）。
        """
        reason = self._enforce_storage()
        if reason:
            # 拒绝写入：不建目录、不落盘、不动索引
            logger.warning("拒绝添加图片（%s）：%s", keyword, reason)
            return 0, 0, 0, reason

        folder = self.data_dir / keyword
        folder.mkdir(parents=True, exist_ok=True)
        # 目录级 MD5 缓存：原来每次添加都要把整个关键词目录重算一遍（线上 637MB 约 2.5s）
        cached_md5 = self._folder_md5s(keyword)
        existing_md5 = set(cached_md5.values())

        added = 0
        skipped = 0
        failed = 0
        refused = 0
        for img in images:
            try:
                # 统一媒体处理：本地路径 / file:// / URL / base64 均会落地为本地文件
                tmp_path = Path(await img.convert_to_file_path())
            except Exception as exc:
                logger.error("图片本地化失败: %s", exc)
                failed += 1
                continue
            if not tmp_path.is_file():
                logger.warning("图片本地化后文件不存在: %s", tmp_path)
                failed += 1
                continue
            limit_mb = self._max_image_mb()
            if limit_mb:
                try:
                    size_mb = tmp_path.stat().st_size / 1048576
                except OSError:
                    size_mb = 0
                if size_mb > limit_mb:
                    refused += 1
                    logger.warning(
                        "跳过超大图片（%s）：%.1fMB > 上限 %dMB",
                        tmp_path.name,
                        size_mb,
                        limit_mb,
                    )
                    continue
            try:
                file_md5 = self._md5(tmp_path)
            except OSError as exc:
                logger.error("计算图片 MD5 失败: %s", exc)
                continue

            # MD5 去重：与同关键词下已有文件比对，重复则跳过并记录日志
            if file_md5 in existing_md5:
                skipped += 1
                logger.info("跳过重复图片（关键词=%s, MD5=%s）", keyword, file_md5)
                continue

            ext = tmp_path.suffix.lower()
            if ext not in ALLOWED_EXTS:
                ext = ".jpg"
            filename = f"{int(time.time() * 1000)}_{random.randint(1000, 9999)}{ext}"
            target = folder / filename
            try:
                # 复制而非移动：临时文件由 AstrBot 事件生命周期管理，复制更安全
                shutil.copy2(tmp_path, target)
            except OSError as exc:
                logger.error("保存图片失败: %s", exc)
                continue
            existing_md5.add(file_md5)
            self._remember_md5(keyword, filename, file_md5)
            self.index.setdefault(keyword, []).append(filename)
            added += 1

        if refused:
            self.__dict__["_last_refused_count"] = refused
        self._save_index()
        reason = None
        if refused and not added and not skipped:
            reason = f"全部 {refused} 张都超过单图上限（{self._max_image_mb()}MB），已跳过"
        return added, skipped, failed, reason

    # ------------------------------------------------------------------
    # B. 发送（来只{关键词}）
    # ------------------------------------------------------------------
    async def _handle_send(self, event: AstrMessageEvent, message_str: str):
        keyword = message_str[2:].strip()
        raw_keyword = keyword
        keyword, _how = self._resolve_keyword(raw_keyword)
        if not keyword:
            yield event.plain_result('关键词 ' + raw_keyword + ' 不存在')
            return
        if not keyword:
            yield event.plain_result("用法：来只{关键词}")
            return
        if not self._is_valid_keyword(keyword):
            yield event.plain_result("关键词不合法（不能是 Windows 保留名，结尾也不能带点或空格）")
            return
        if keyword in self.blocked_keywords:
            yield event.plain_result(f"关键词 {keyword} 已被屏蔽，无法发送")
            return

        # 自修复：索引中存在但文件已丢失的条目自动清理
        files = self._prune_missing(keyword)
        if not files:
            yield event.plain_result(f"关键词 {keyword} 下还不存在图片")
            return

        filename = ai_impl.pick_sticker(files)   # 与 AI 工具同源
        if not filename:
            yield event.plain_result("关键词 %s 下还不存在图片" % keyword)
            return
        image_path = (self.data_dir / keyword / filename).resolve()
        yield event.image_result(str(image_path))

    # ------------------------------------------------------------------
    # C. 列表（仅管理员，分页）
    # ------------------------------------------------------------------
    async def _handle_list(self, event: AstrMessageEvent, message_str: str):
        if not self._is_admin(event):
            yield event.plain_result("此指令仅限管理员")
            return

        keyword, page = self._parse_list_command(message_str[2:].strip())
        if not keyword:
            yield event.plain_result("用法：列表{关键词}{页码}（页码可选）")
            return
        if not self._is_valid_keyword(keyword):
            yield event.plain_result("关键词不合法（不能是 Windows 保留名，结尾也不能带点或空格）")
            return
        if keyword in self.blocked_keywords:
            yield event.plain_result(f"关键词 {keyword} 已被屏蔽")
            return

        files = self._prune_missing(keyword)
        if not files:
            yield event.plain_result("该关键词下没有图片")
            return

        result = ai_impl.list_lines(files, page, PAGE_SIZE)   # 与 AI 工具同源
        if not result.get("ok"):
            yield event.plain_result(result.get("reason") or "没有数据")
            return
        lines = list(result["lines"])
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------------
    # D. 精准删除（仅管理员）
    # ------------------------------------------------------------------
    async def _handle_delete_one(self, event: AstrMessageEvent, message_str: str):
        if not self._is_admin(event):
            yield event.plain_result("此指令仅限管理员")
            return

        # 优先走“回复图片按 MD5 删除”：回复/同消息包含图片时，图片即删除依据
        images = self._extract_reply_images(event)
        if not images:
            message_chain = getattr(event.message_obj, "message", None) or []
            images = [c for c in message_chain if isinstance(c, Image)]
        if images:
            async for result in self._delete_by_md5(event, message_str, images):
                yield result
            return

        rest = message_str[2:].strip()
        raw_keyword = rest
        rest, _how = self._resolve_keyword(raw_keyword)
        if not rest:
            yield event.plain_result('关键词 ' + raw_keyword + ' 不存在')
            return
        match = re.search(r"(\d+)\s*$", rest)
        if not match:
            yield event.plain_result("用法：删图（回复要删除的图片），或删图{关键词}{序号}")
            return
        keyword = rest[: match.start()].strip()
        index = int(match.group(1))
        if not keyword:
            yield event.plain_result("用法：删图（回复要删除的图片），或删图{关键词}{序号}")
            return
        if not self._is_valid_keyword(keyword):
            yield event.plain_result("关键词不合法（不能是 Windows 保留名，结尾也不能带点或空格）")
            return

        files = self._prune_missing(keyword)
        if not files:
            yield event.plain_result(f"关键词 {keyword} 下不存在图片")
            return
        if index < 1 or index > len(files):
            yield event.plain_result(f"序号无效，该关键词下共 {len(files)} 张图片")
            return

        filename = files[index - 1]
        target = self.data_dir / keyword / filename
        try:
            if target.exists():
                target.unlink()
        except OSError as exc:
            logger.error("删除图片失败: %s", exc)
            yield event.plain_result(f"删除失败：{exc}")
            return

        files.pop(index - 1)
        if files:
            self.index[keyword] = files
        else:
            self.index.pop(keyword, None)
        self._save_index()
        yield event.plain_result(f"已删除第 {index} 张图片：{filename}")

    async def _delete_by_md5(
        self,
        event: AstrMessageEvent,
        message_str: str,
        images: List[Image],
    ):
        """按回复图片的 MD5 删除匹配图片（仅管理员）。

        不带关键词时自动在所有关键词中定位；带关键词时仅在该关键词下删除。
        """
        keyword = message_str[2:].strip()
        raw_keyword = keyword
        keyword, _how = self._resolve_keyword(raw_keyword)
        if not keyword:
            yield event.plain_result('关键词 ' + raw_keyword + ' 不存在')
            return
        if keyword and not self._is_valid_keyword(keyword):
            yield event.plain_result("关键词不合法（不能是 Windows 保留名，结尾也不能带点或空格）")
            return

        # 待扫描的关键词目录：限定关键词，或全部已索引关键词
        if keyword:
            folders = [(keyword, self.data_dir / keyword)]
        else:
            folders = [
                (k, self.data_dir / k)
                for k in self.index
                if (self.data_dir / k).is_dir()
            ]
        if not folders:
            yield event.plain_result(
                f"关键词 {keyword} 下不存在图片"
                if keyword
                else "表情包库中还没有任何关键词"
            )
            return

        # 计算回复图片的 MD5（支持一次回复多张图片）
        target_md5s = set()
        failed = 0
        for img in images:
            try:
                tmp_path = Path(await img.convert_to_file_path())
            except Exception as exc:
                logger.error("图片本地化失败: %s", exc)
                failed += 1
                continue
            if not tmp_path.is_file():
                logger.warning("图片本地化后文件不存在: %s", tmp_path)
                failed += 1
                continue
            try:
                target_md5s.add(self._md5(tmp_path))
            except OSError as exc:
                logger.error("计算图片 MD5 失败: %s", exc)
                failed += 1

        if not target_md5s:
            yield event.plain_result("删除失败：未能获取回复图片的内容")
            return

        # 在各关键词目录中按 MD5 匹配（同 MD5 的多份文件会一并删除）
        deleted = 0
        failed_delete = 0
        deleted_by_keyword = {}
        for kw, folder in folders:
            matched = []
            for f in folder.iterdir():
                if not f.is_file():
                    continue
                try:
                    if self._md5(f) in target_md5s:
                        matched.append(f)
                except OSError:
                    continue
            if not matched:
                continue
            for f in matched:
                try:
                    f.unlink()
                    deleted += 1
                except OSError as exc:
                    logger.error("删除图片失败 %s: %s", f, exc)
                    failed_delete += 1
            self._prune_missing(kw)
            deleted_by_keyword[kw] = len(matched)

        if deleted == 0 and failed_delete == 0:
            yield event.plain_result(
                f"关键词 {keyword} 下没有与回复图片匹配的文件"
                if keyword
                else "该图未在表情包库中找到，未删除任何文件"
            )
            return

        if keyword:
            parts = [f"已按 MD5 删除 {deleted} 张图片（关键词 {keyword}）"]
        else:
            summary = "、".join(
                f"{kw} {n} 张" for kw, n in deleted_by_keyword.items()
            )
            parts = [f"已按 MD5 删除 {deleted} 张图片（{summary}）"]
        if failed_delete:
            parts.append(f"{failed_delete} 张删除失败")
        if failed:
            parts.append(f"{failed} 张回复图片未能读取")
        yield event.plain_result("；".join(parts))

    # ------------------------------------------------------------------
    # E. 批量删除（仅管理员，60 秒二次确认）
    # ------------------------------------------------------------------
    async def _handle_delete_all(self, event: AstrMessageEvent, message_str: str):
        if not self._is_admin(event):
            yield event.plain_result("此指令仅限管理员")
            return

        keyword = message_str[2:].strip()
        raw_keyword = keyword
        keyword, _how = self._resolve_keyword(raw_keyword)
        if not keyword:
            yield event.plain_result('关键词 ' + raw_keyword + ' 不存在')
            return
        if not keyword:
            yield event.plain_result("用法：删除{关键词}")
            return
        if not self._is_valid_keyword(keyword):
            yield event.plain_result("关键词不合法（不能是 Windows 保留名，结尾也不能带点或空格）")
            return

        sender_id = event.get_sender_id()
        now = time.time()
        pending = self.pending_delete.get(sender_id)

        if (
            pending
            and pending["keyword"] == keyword
            and now - pending["time"] <= CONFIRM_TTL
        ):
            self.pending_delete.pop(sender_id, None)
            folder = self.data_dir / keyword
            try:
                if folder.exists():
                    shutil.rmtree(folder)
            except OSError as exc:
                logger.error("删除关键词目录失败: %s", exc)
                yield event.plain_result(f"删除失败：{exc}")
                return
            self.index.pop(keyword, None)
            self._save_index()
            yield event.plain_result(f"已删除关键词 {keyword} 的全部图片")
            return

        # 有挂起请求但超时或关键词不匹配：忽略本次确认
        if pending:
            self.pending_delete.pop(sender_id, None)
            yield event.plain_result("确认超时或关键词不匹配，请重新发送")
            return

        self.pending_delete[sender_id] = {"keyword": keyword, "time": now}
        yield event.plain_result(f"请再次发送 `删除{keyword}` 确认（60秒内）")

    # ------------------------------------------------------------------
    # F. 统计（仅管理员）
    # ------------------------------------------------------------------
    async def _handle_stats(self, event: AstrMessageEvent, message_str: str):
        if not self._is_admin(event):
            yield event.plain_result("此指令仅限管理员")
            return

        if not self.index:
            yield event.plain_result("暂无任何表情包数据")
            return

        result = ai_impl.stats_lines(self.index, top=3)   # 与 AI 工具同源
        if not result.get("ok"):
            yield event.plain_result(result.get("reason") or "暂无数据")
            return
        lines = list(result["lines"])
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------------
    # 菜单（所有人）
    # ------------------------------------------------------------------
    async def _handle_menu(self, event: AstrMessageEvent, message_str: str):
        try:
            image_path = await asyncio.to_thread(
                self.menu_renderer.render,
                MENU_TEXT,
            )
        except Exception as exc:
            logger.error("表情包管理菜单转图片失败：%s", exc, exc_info=True)
            image_path = None
        if image_path is not None and image_path.is_file():
            yield event.image_result(str(image_path))
            return
        for piece in self.menu_renderer.text_chunks(MENU_TEXT):
            yield event.plain_result(piece)

    # ------------------------------------------------------------------
    # G. WebUI 备份 / 恢复（AstrBot 管理面板后台）
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # AI 能力：sticker_send（所有人；屏蔽校验 + 节流 + 10s 幂等窗口）
    #           sticker_stats / sticker_list（管理员只读）
    #   取数逻辑与「来只{}」「列表」「统计」指令同源（ai_impl），不写盘、不改索引。
    # ------------------------------------------------------------------
    def _ai_settings(self) -> dict:
        """读 AI 配置：缺键回落默认值，越界值回默认（页面侧还会再校验一次）。"""
        values: dict = {}
        for key, default in AI_CONFIG_DEFAULTS.items():
            values[key] = self.config.get(key, default)
        for key in AI_BOOL_KEYS:
            values[key] = ai_common.to_bool(values.get(key), default=AI_CONFIG_DEFAULTS[key])
        for key, (low, high) in AI_INT_BOUNDS.items():
            parsed = ai_common.parse_int(values.get(key), low, high)
            values[key] = parsed if parsed is not None else AI_CONFIG_DEFAULTS[key]
        return values

    def _apply_ai_payload(self, patch: dict) -> dict:
        """保存页面传来的 AI 配置：非法/越界一律报错，不静默改数。"""
        updated: dict = {}
        for key in AI_BOOL_KEYS:
            if key in patch:
                value = ai_common.to_bool(patch.get(key), default=AI_CONFIG_DEFAULTS[key])
                self.config[key] = value
                updated[key] = value
        for key, (low, high) in AI_INT_BOUNDS.items():
            if key in patch:
                value = ai_common.parse_int(patch.get(key), low, high)
                if value is None:
                    raise ValueError("%s 必须是 %d~%d 的整数" % (key, low, high))
                self.config[key] = value
                updated[key] = value
        return updated

    async def _ai_guard(self, event: AstrMessageEvent, tool: str, *, send: bool = False):
        """统一门禁：开关（读/发分开）+ 节流（按调用者）；被拒返回错误 JSON 文本。"""
        settings = self._ai_settings()
        if not settings["sticker_ai_tools_enabled"]:
            return ai_common.fail("disabled", "表情包的 AI 工具已在配置里关闭", hint="管理员可在插件页打开")
        if send and not settings["sticker_ai_send_enabled"]:
            return ai_common.fail("disabled", "AI 主动发表情包已关闭（只读工具仍可用）")
        if not send and not settings["sticker_ai_read_enabled"]:
            return ai_common.fail("disabled", "表情包的 AI 只读工具已在配置里关闭")
        limit = int(settings["sticker_ai_throttle_limit"])
        window = int(settings["sticker_ai_throttle_window"])
        throttle = getattr(self, "_ai_throttle", None)
        if throttle is None or (throttle.limit, throttle.window) != (limit, window):
            throttle = ai_common.ToolThrottle(limit=limit, window=window)
            self._ai_throttle = throttle
        sender = "-"
        try:
            sender = str(event.get_sender_id() or "-")
        except Exception:
            sender = "-"
        key = "%s:%s" % (str(getattr(event, "unified_msg_origin", "") or "unknown"), sender)
        wait = throttle.check(tool, key)
        if wait:
            return ai_common.fail("throttled", "发得太快了，请 %d 秒后再试" % wait, retry_after=wait)
        return None

    def _read_index_files(self, keyword: str) -> list:
        """只读取数：索引里的文件名 → 过滤掉文件已丢的条目（**不写盘**）。"""
        folder = self.data_dir / keyword
        files = (self.index or {}).get(keyword, []) or []
        return ai_impl.existing_files(files, lambda name: (folder / name).is_file())

    def _ai_recent_send(self, session: str, keyword: str, window: int) -> Optional[dict]:
        """同会话同关键词在窗口内的上次发送结果（GR-12 幂等）；无则 None。"""
        if window <= 0:
            return None
        entry = self._ai_send_log.get((session, keyword))
        if not entry:
            return None
        stamp, payload = entry
        if time.time() - float(stamp) > float(window):
            return None
        return dict(payload)

    def _ai_note_send(self, session: str, keyword: str, payload: dict) -> None:
        self._ai_send_log[(session, keyword)] = (time.time(), dict(payload))
        if len(self._ai_send_log) > 512:   # 长跑进程的简单上限
            for key in list(self._ai_send_log)[:256]:
                self._ai_send_log.pop(key, None)

    @filter.llm_tool(name="sticker_send")
    async def sticker_send(self, event: AstrMessageEvent, **kwargs) -> str:
        """随机发一张表情包（直接把图片发到当前会话）。

        何时使用：用户说"发个猫的表情包/来个乐子"时。
        何时别用：不确定关键词时先用 sticker_stats 看看有什么；短时间不要重复调用。

        Args:
            keyword(string): 必填，表情包关键词（如 猫、点赞）。
        """
        blocked = await self._ai_guard(event, "sticker_send", send=True)
        if blocked:
            return blocked
        keyword = ai_impl.keyword_or_empty((kwargs or {}).get("keyword"))
        resolved, _alias_how = self._resolve_keyword(keyword)
        keyword = resolved or keyword  # 未知关键词保留原值，交给下游给候选提示
        if not keyword:
            return ai_common.fail("invalid_argument", "缺少 keyword（表情包关键词）", field="keyword")
        reason = ai_impl.block_reason(keyword, self.blocked_keywords)
        if reason:
            # 设计 §7.2：屏蔽关键词按 disabled 回报（便于 AI 区分"功能被关"与"关键词被屏蔽"）
            return ai_common.fail("disabled", "该关键词不可用", keyword=keyword)
        if len(keyword) > ai_impl.KEYWORD_MAX_LEN:
            return ai_common.fail(
                "invalid_argument",
                "关键词过长（最多 %d 字）" % ai_impl.KEYWORD_MAX_LEN,
                field="keyword",
            )
        if not self._is_valid_keyword(keyword):
            return ai_common.fail("invalid_argument", "关键词不合法（保留名/结尾点号等）", field="keyword")
        files = self._read_index_files(keyword)
        if not files:
            hint = ai_impl.keyword_hint(self.index, keyword)
            return ai_common.fail(
                "not_found",
                "关键词「%s」下没有图片" % keyword,
                hint=("可用关键词：" + "、".join(hint)) if hint else "还没有任何表情包",
            )
        session = str(getattr(event, "unified_msg_origin", "") or "")
        window = int(self._ai_settings()["sticker_ai_send_dedup_window"])
        recent = self._ai_recent_send(session, keyword, window)
        if recent is not None:
            recent.update({"deduped": True, "note": "%d 秒内已经发过同关键词的表情包，没有重复发送" % window})
            return ai_common.ok(recent)
        filename = ai_impl.pick_sticker(files)
        if not filename:
            return ai_common.fail("not_found", "没有挑到可用的图片")
        path = (self.data_dir / keyword / filename).resolve()
        payload = {
            "keyword": keyword,
            "file": filename,
            "count": len(files),
            "deduped": False,
            "text": "已发送关键词「%s」下的 1 张表情包" % keyword,
        }
        # 先占位再发送：并发同会话同关键词只有一路真正发出去（GR-12）
        self._ai_note_send(session, keyword, payload)
        try:
            await event.send(MessageChain([Image.fromFileSystem(str(path))]))
        except Exception as exc:
            self._ai_send_log.pop((session, keyword), None)   # 失败回滚，允许重试
            logger.warning("AI 发表情包失败: %s", exc)
            return ai_common.fail("capability_unavailable", "表情包没能发出去（平台拒绝或文件不可读）")
        logger.info("AI 工具 sticker_send 命中（keyword=%s）", keyword)
        return ai_common.ok(payload)

    @filter.llm_tool(name="sticker_stats")
    async def sticker_stats(self, event: AstrMessageEvent, **kwargs) -> str:
        """查看表情包库统计：关键词数、图片总数、图片最多的前 3 个关键词（管理员）。

        何时使用：管理员问"表情包有多少/哪些关键词最多"。
        何时别用：要看具体文件名用 sticker_list。
        """
        blocked = await self._ai_guard(event, "sticker_stats")
        if blocked:
            return blocked
        if not self._is_admin(event):
            return ai_common.fail("permission_denied", "统计仅管理员可用")
        result = ai_impl.stats_lines(self.index, top=3)
        if not result.get("ok"):
            return ai_common.fail("not_found", result.get("reason") or "暂无数据")
        logger.info("AI 工具 sticker_stats 命中")
        return ai_common.ok(
            {
                "total_keywords": result["total_keywords"],
                "total_images": result["total_images"],
                "top": result["top"],
                "text": "\n".join(result["lines"]),
            }
        )

    @filter.llm_tool(name="sticker_list")
    async def sticker_list(self, event: AstrMessageEvent, **kwargs) -> str:
        """分页列出某个关键词下的表情包文件名（管理员，每页 10 张）。

        何时使用：管理员问"猫这个关键词里都有哪些图"。
        何时别用：只看数量用 sticker_stats。

        Args:
            keyword(string): 必填，表情包关键词。
            page(number): 可选，第几页，默认 1。
        """
        blocked = await self._ai_guard(event, "sticker_list")
        if blocked:
            return blocked
        if not self._is_admin(event):
            return ai_common.fail("permission_denied", "列表仅管理员可用")
        keyword = ai_impl.keyword_or_empty((kwargs or {}).get("keyword"))
        resolved, _alias_how = self._resolve_keyword(keyword)
        keyword = resolved or keyword  # 未知关键词保留原值，交给下游给候选提示
        if not keyword:
            return ai_common.fail("invalid_argument", "缺少 keyword（表情包关键词）", field="keyword")
        reason = ai_impl.block_reason(keyword, self.blocked_keywords)
        if reason:
            return ai_common.fail("disabled", reason, field="keyword")   # 与 sticker_send 统一：屏蔽按「功能被关」回报
        if len(keyword) > ai_impl.KEYWORD_MAX_LEN:
            return ai_common.fail(
                "invalid_argument",
                "关键词过长（最多 %d 字）" % ai_impl.KEYWORD_MAX_LEN,
                field="keyword",
            )
        if not self._is_valid_keyword(keyword):
            return ai_common.fail("invalid_argument", "关键词不合法（保留名/路径字符等）", field="keyword")
        raw_page = (kwargs or {}).get("page")
        if str(raw_page or "").strip():
            page = ai_common.parse_int(raw_page, 1, 1000)
            if page is None:
                return ai_common.fail("invalid_argument", "page 只能是正整数", field="page")
        else:
            page = 1
        files = self._read_index_files(keyword)
        result = ai_impl.list_lines(files, page, PAGE_SIZE)
        if not result.get("ok"):
            total_pages = int(result.get("total_pages") or 0)
            if total_pages and page > total_pages:
                return ai_common.fail(
                    "invalid_argument",
                    "页码超出范围：该关键词只有第 1~%d 页（每页 %d 张）" % (total_pages, ai_impl.PAGE_SIZE),
                    field="page",
                    hint="该关键词共 %d 张图；请用 page=1 重新查" % int(result.get("total") or 0),
                )
            hint = ai_impl.keyword_hint(self.index, keyword)
            return ai_common.fail(
                "not_found",
                result.get("reason") or "没有数据",
                hint=("可用关键词：" + "、".join(hint)) if hint else "",
            )
        logger.info("AI 工具 sticker_list 命中（keyword=%s, page=%s）", keyword, page)
        return ai_common.ok(
            {
                "keyword": keyword,
                "page": result["page"],
                "total_pages": result["total_pages"],
                "total": result["total"],
                "items": result["items"],
                "text": "\n".join(result["lines"]),
            }
        )
    async def _web_config_get(self):
        return json_response(
            {
                "status": "success",
                "data": {
                    "admin_users": [
                        str(a) for a in (self.config.get("admin_users", []) or [])
                    ],
                    "max_storage_mb": self._max_storage_mb(),
                    "backup_keep": self._backup_keep(),
                    "backup_total_mb": self._backup_total_mb(),
                    "storage": self._storage_usage(),
                    "ai": self._ai_settings(),
                },
            }
        )

    async def _web_config_set(self):
        payload = await request.json(default=None)
        if not isinstance(payload, dict):
            return error_response("请求体格式不正确")
        try:
            updated = self._apply_config_payload(payload)
        except ValueError as exc:
            return error_response(str(exc))
        return json_response({"status": "success", "data": updated})

    def _max_storage_mb(self) -> int:
        try:
            return int(
                self.config.get("max_storage_mb", DEFAULT_MAX_STORAGE_MB)
                or DEFAULT_MAX_STORAGE_MB
            )
        except (TypeError, ValueError):
            return DEFAULT_MAX_STORAGE_MB

    def _max_image_mb(self) -> int:
        try:
            value = int(self.config.get("max_image_mb", DEFAULT_MAX_IMAGE_MB))
        except (TypeError, ValueError):
            return DEFAULT_MAX_IMAGE_MB
        return value if 0 <= value <= 50 else DEFAULT_MAX_IMAGE_MB

    def _backup_keep(self) -> int:
        try:
            value = int(self.config.get("backup_keep", DEFAULT_BACKUP_KEEP))
        except (TypeError, ValueError):
            return DEFAULT_BACKUP_KEEP
        return value if 1 <= value <= 20 else DEFAULT_BACKUP_KEEP

    def _backup_total_mb(self) -> int:
        try:
            value = int(self.config.get("backup_total_mb", DEFAULT_BACKUP_TOTAL_MB))
        except (TypeError, ValueError):
            return DEFAULT_BACKUP_TOTAL_MB
        return value if 100 <= value <= 20000 else DEFAULT_BACKUP_TOTAL_MB

    def _storage_usage(self) -> dict:
        """全仓唯一的用量口径：贴纸库 + 备份目录分开统计。

        贴纸库按 (st_dev, st_ino) 去重——P3 的硬链接归并会让同一份内容出现多个目录项，
        按文件累加会把用量算高。备份目录是 data_dir 的兄弟目录（见 __init__），
        原来用 rglob(data_dir) 统计永远漏掉它，导致 955MB 备份不参与上限判断。
        """
        limit_mb = self._max_storage_mb()
        seen = set()
        used = 0
        files = 0
        if self.data_dir.exists():
            for path in self.data_dir.rglob("*"):
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if not path.is_file():
                    continue
                key = (stat.st_dev, stat.st_ino)
                if key in seen:
                    continue
                seen.add(key)
                used += stat.st_size
                files += 1

        backup_bytes = 0
        backup_count = 0
        try:
            for path in self.backup_root.glob("*.zip"):
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                backup_bytes += size
                backup_count += 1
        except OSError:
            pass

        return {
            "used_mb": round(used / 1048576, 1),
            "limit_mb": limit_mb,
            "files": files,
            "backup_mb": round(backup_bytes / 1048576, 1),
            "backup_count": backup_count,
        }

    def _enforce_storage(self) -> Optional[str]:
        """超上限时返回可直接回给用户的文案，否则 None。

        以前只在启动时 warning 一次，运行中涨到多少都不管；线上 636MB/2010MB 从未触发。
        现在每次新增前都判，超了就拒绝写入（不落盘、不动索引）。
        """
        usage = self._storage_usage()
        if usage["used_mb"] > usage["limit_mb"]:
            return (
                f"存储已满（当前 {usage['used_mb']}MB / 上限 {usage['limit_mb']}MB，"
                f"共 {usage['files']} 张）：请先删除部分关键词，或调高上限后重试"
            )
        return None

    def _apply_config_payload(self, payload: dict) -> dict:
        """校验并保存后台配置，返回实际更新的字段。"""
        # 先整体校验到临时表，全部通过再落内存：避免"admin 已改、ai 报错"的半状态
        snapshot = dict(self.config)
        updated: dict = {}

        try:
            return self._apply_config_payload_inner(payload, updated)
        except Exception:
            self.config.clear()
            self.config.update(snapshot)   # 回滚内存，磁盘本来就没写
            raise

    def _apply_config_payload_inner(self, payload: dict, updated: dict) -> dict:
        if "admin_users" in payload:
            admins = payload["admin_users"]
            if not isinstance(admins, list):
                raise ValueError("admin_users 必须是列表")
            normalized: list = []
            for item in admins:
                text = str(item).strip()
                if text and text not in normalized:
                    normalized.append(text)
            self.config["admin_users"] = normalized
            updated["admin_users"] = normalized
        if "max_storage_mb" in payload:
            try:
                limit = int(payload["max_storage_mb"])
            except (TypeError, ValueError):
                raise ValueError("max_storage_mb 必须是整数")
            if limit <= 0:
                raise ValueError("max_storage_mb 必须大于 0")
            self.config["max_storage_mb"] = limit
            updated["max_storage_mb"] = limit
        if "backup_keep" in payload:
            try:
                keep = int(payload["backup_keep"])
            except (TypeError, ValueError):
                raise ValueError("backup_keep 必须是整数")
            if not 1 <= keep <= 20:
                raise ValueError("backup_keep 必须在 1~20 之间")
            self.config["backup_keep"] = keep
            updated["backup_keep"] = keep
        if "backup_total_mb" in payload:
            try:
                total_mb = int(payload["backup_total_mb"])
            except (TypeError, ValueError):
                raise ValueError("backup_total_mb 必须是整数")
            if not 100 <= total_mb <= 20000:
                raise ValueError("backup_total_mb 必须在 100~20000 之间")
            self.config["backup_total_mb"] = total_mb
            updated["backup_total_mb"] = total_mb
        ai_patch = payload.get("ai")
        if isinstance(ai_patch, dict):
            updated["ai"] = self._apply_ai_payload(ai_patch)
        try:
            self.config.save_config()
        except Exception as exc:
            logger.error("保存插件配置失败: %s", exc)
            raise ValueError(f"保存配置失败：{exc}") from exc
        return updated

    def _backup_progress_init(self) -> dict:
        """初始化一次备份的进度对象（同一次备份里被原地更新）。"""
        total_files = sum(len(names) for names in self.index.values())
        return {
            "state": "running",
            "percent": 0,
            "done_files": 0,
            "total_files": total_files,
            "done_bytes": 0,
            "filename": None,
            "error": None,
            "started_at": time.time(),
        }

    def _run_backup_thread(self, progress: dict) -> None:
        """在线程里跑备份，并把结果写回 progress（异常必须落进 state，不能静默）。"""
        try:
            info = self._backup_to_zip(progress=progress)
            progress.update(
                {
                    "state": "done",
                    "percent": 100,
                    "filename": info["filename"],
                    "size": info["size"],
                    "pruned": info.get("pruned", []),
                }
            )
            logger.info(
                "备份完成：%s（%.1fMB，%d 张，清理旧包 %d 份）",
                info["filename"],
                info["size"] / 1048576,
                info["images"],
                len(info.get("pruned", [])),
            )
        except Exception as exc:
            logger.error("备份失败: %s", exc, exc_info=True)
            progress.update({"state": "failed", "error": str(exc)})

    def _on_backup_task_done(self, task) -> None:
        """后台任务收尾：异常必须落到 state，否则前端会永远停在"备份中"。"""
        state = self.__dict__.get("_backup_state") or {}
        try:
            exc = None if task.cancelled() else task.exception()
        except Exception:  # pragma: no cover - 取异常本身失败
            exc = None
        if state.get("state") == "running" and (task.cancelled() or exc is not None):
            state.update(
                {
                    "state": "failed",
                    "error": "任务被取消" if task.cancelled() else str(exc),
                }
            )
            logger.error("备份任务异常结束: %s", state.get("error"))

    async def _web_backup(self):
        """受理备份请求：后台线程执行，立刻返回，避免把事件循环卡住。

        原实现直接在 handler 里同步压缩（线上 637MB 约 15s），这期间整个机器人不响应，
        前端也拿不到任何进度。
        """
        state = self.__dict__.get("_backup_state") or {}
        if state.get("state") == "running":
            return json_response(
                {
                    "status": "running",
                    "message": (
                        f"备份中 {state.get('percent', 0)}%"
                        f"（{state.get('done_files', 0)}/{state.get('total_files', 0)} 个文件）"
                    ),
                    "data": state,
                }
            )

        progress = self._backup_progress_init()
        self._backup_state = progress
        task = asyncio.create_task(asyncio.to_thread(self._run_backup_thread, progress))
        # 必须持引用：不持引用时 task 可能被 GC，异常也会丢
        self._backup_task = task
        task.add_done_callback(self._on_backup_task_done)
        return json_response(
            {
                "status": "accepted",
                "message": (
                    f"备份已开始（{progress['total_files']} 个文件），"
                    "可在备份列表查看进度"
                ),
                "data": progress,
            }
        )

    async def _web_backups(self):
        items = []
        try:
            for p in sorted(
                self.backup_root.glob("sticker_backup_*.zip"), reverse=True
            ):
                st = p.stat()
                items.append(
                    {
                        "filename": p.name,
                        "size": st.st_size,
                        "created": time.strftime(
                            "%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)
                        ),
                    }
                )
        except OSError as exc:
            logger.error("读取备份列表失败: %s", exc)
            return error_response(f"读取备份列表失败：{exc}")
        return json_response({"status": "success", "data": {"backups": items}})

    async def _web_maintenance_scan(self):
        try:
            report = await asyncio.to_thread(self._scan_orphans)
        except Exception as exc:
            logger.error("孤儿扫描失败: %s", exc, exc_info=True)
            return error_response(f"扫描失败：{exc}")
        return json_response({"status": "success", "data": report})

    async def _web_maintenance_dedupe(self):
        payload = await request.json(default=None)
        dry_run = True if not isinstance(payload, dict) else bool(payload.get("apply") is not True)
        try:
            report = await asyncio.to_thread(self._dedupe_by_hardlink, dry_run)
        except Exception as exc:
            logger.error("去重失败: %s", exc, exc_info=True)
            return error_response(f"去重失败：{exc}")
        return json_response({"status": "success", "data": report})

    async def _web_backup_progress(self):
        state = self.__dict__.get("_backup_state") or {"state": "idle", "percent": 0}
        return json_response({"status": "success", "data": state})

    async def _web_export(self):
        """按关键词导出：产物是标准备份包，单卷小、下载不易中断。

        为什么加这个：完整备份线上已 600MB 级，而前端下载走的是"整包读进浏览器内存"
        的通道（框架行为，插件改不了），所以把"可下载单元"做小是当前唯一稳妥的解。
        """
        keyword = (request.query.get("keyword") or "").strip()
        if not keyword:
            return error_response("缺少关键词")
        if keyword not in self.index:
            return error_response(f"关键词 {keyword} 不存在")
        state = self.__dict__.get("_backup_state") or {}
        if state.get("state") == "running":
            return error_response("备份进行中，请稍后再导出")
        try:
            # 走线程：导出可能上百 MB，在协程里同步压缩会堵住事件循环（审计 M1）
            info = await asyncio.to_thread(
                self._backup_to_zip, "export", None, keyword
            )
        except Exception as exc:
            logger.error("导出关键词失败: %s", exc, exc_info=True)
            return error_response(f"导出失败：{exc}")
        return json_response(
            {
                "status": "success",
                "message": (
                    f"已导出 {keyword}：{info['images']} 张 / "
                    f"{info['size'] / 1048576:.1f}MB（{info['filename']}）"
                ),
                "data": info,
            }
        )

    async def _web_download(self):
        name = request.query.get("name", "")
        if not name:
            return error_response("缺少备份文件名")
        # 防止路径穿越：只允许 sticker_backups 目录内、名为原文件名的 zip
        target = (self.backup_root / Path(name).name).resolve()
        if (
            target.parent != self.backup_root.resolve()
            or not target.is_file()
            or target.suffix != ".zip"
        ):
            return error_response("备份文件不存在")
        return file_response(
            target, filename=target.name, content_type="application/zip"
        )

    async def _web_backup_delete(self):
        """删除备份列表中的指定压缩包（仅删除备份文件，不影响现有表情包数据）。"""
        payload = await request.json(default=None)
        if not isinstance(payload, dict):
            return error_response("请求体格式不正确")
        name = str(payload.get("name") or "").strip()
        if not name:
            return error_response("缺少备份文件名")
        # 与下载一致：只允许 sticker_backups 目录内、名为原文件名的 zip
        target = (self.backup_root / Path(name).name).resolve()
        if (
            target.parent != self.backup_root.resolve()
            or not target.is_file()
            or target.suffix != ".zip"
        ):
            return error_response("备份文件不存在")
        try:
            target.unlink()
        except OSError as exc:
            logger.error("删除备份文件失败: %s", exc)
            return error_response(f"删除失败：{exc}")
        logger.info("StickerPlugin 已删除备份文件: %s", target.name)
        return json_response(
            {
                "status": "success",
                "data": {
                    "filename": target.name,
                    "message": f"已删除备份文件 {target.name}",
                },
            }
        )

    async def _web_restore(self):
        files = await request.files()
        upload = files.get("file")
        if upload is None:
            return error_response("请上传备份压缩包")
        tmp_path = (
            self.backup_root.parent
            / f"restore_upload_{int(time.time() * 1000)}_{random.randint(1000, 9999)}.zip"
        )
        try:
            await upload.save(tmp_path)
            # 恢复是不可逆操作：先留一份快照，快照失败就中止（宁可不恢复）
            try:
                pre = await asyncio.to_thread(self._backup_to_zip, "pre-restore")
                logger.info(
                    "恢复前已自动备份：%s（%.1fMB）",
                    pre["filename"],
                    pre["size"] / 1048576,
                )
            except Exception as exc:
                logger.error("恢复前备份失败，已中止恢复: %s", exc, exc_info=True)
                return error_response(f"恢复已中止：恢复前备份失败（{exc}）")
            stats = self._restore_from_zip(tmp_path)
        except Exception as exc:
            logger.error("恢复备份失败: %s", exc, exc_info=True)
            return error_response(f"恢复失败：{exc}")
        finally:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass
        return json_response({"status": "success", "data": stats})

    async def _web_clear(self):
        """清空全部表情包数据（删除所有关键词目录与索引，不影响管理员/屏蔽列表）。"""
        try:
            cleared_keywords = len(self.index)
            cleared_images = sum(
                len(names)
                for names in self.index.values()
                if isinstance(names, list)
            )
            for child in list(self.data_dir.iterdir()):
                if child.is_dir():
                    shutil.rmtree(child)
            self.index.clear()
            self.pending_delete.clear()
            self._save_index()
        except Exception as exc:
            logger.error("清空表情包失败: %s", exc, exc_info=True)
            return error_response(f"清空失败：{exc}")
        return json_response(
            {
                "status": "success",
                "data": {
                    "cleared_keywords": cleared_keywords,
                    "cleared_images": cleared_images,
                    "message": (
                        f"已清空 {cleared_keywords} 个关键词、"
                        f"{cleared_images} 张图片"
                    ),
                },
            }
        )

    def _backup_to_zip(
        self,
        prefix: str = "sticker_backup",
        progress: Optional[dict] = None,
        only_keyword: Optional[str] = None,
    ) -> dict:
        """把当前全部表情包数据打包为 zip（含 manifest 校验清单）。

        prefix 用于区分自动备份（sticker_backup_*）与恢复前快照（pre-restore_*）。
        用 ZIP_STORED：图片本身已压缩，实测压缩比 0.96，DEFLATE 只烧 CPU（637MB 约 15s → 约 1.3s）。
        """
        self.backup_root.mkdir(parents=True, exist_ok=True)
        filename = (
            f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}_{random.randint(1000, 9999)}.zip"
        )
        target = self.backup_root / filename
        # 立刻快照索引：备份期间有新图入库时，直接遍历活字典会抛
        # RuntimeError: dictionary changed size during iteration（审计 S1）。
        if only_keyword and only_keyword in self.index:
            payload_index = {only_keyword: list(self.index[only_keyword])}
        else:
            payload_index = {k: list(v) for k, v in self.index.items()}
        # 先写 .part，成功后原子改名：中途失败不会留下能被列表/下载/用量统计
        # 当有效包的半截 zip（审计 S3）。
        part = target.with_suffix(target.suffix + ".part")
        try:
            info = self._write_backup_zip(part, payload_index, progress, only_keyword)
            os.replace(part, target)
        except BaseException:
            try:
                if part.exists():
                    part.unlink()
            except OSError:
                pass
            raise
        info["filename"] = filename
        info["size"] = target.stat().st_size
        info["pruned"] = self._prune_backups(keep_name=filename)
        return info

    def _write_backup_zip(
        self,
        target: Path,
        payload_index: dict,
        progress: Optional[dict],
        only_keyword: Optional[str],
    ) -> dict:
        """把 payload_index 写进 target（调用方负责 .part + 原子改名）。"""
        files_meta = []
        with zipfile.ZipFile(target, "w", zipfile.ZIP_STORED) as zf:
            zf.writestr(
                "index.json",
                json.dumps(payload_index, ensure_ascii=False, indent=2),
            )
            zf.writestr(
                "blocked_keywords.json",
                json.dumps(sorted(self.blocked_keywords), ensure_ascii=False, indent=2),
            )
            # 别名表：只导单个关键词时也带上它的组，恢复后别名不丢
            groups = self.__dict__.get("alias_groups") or {}
            if only_keyword:
                groups = (
                    {only_keyword: groups[only_keyword]}
                    if only_keyword in groups
                    else {}
                )
            zf.writestr(
                "aliases.json",
                json.dumps(
                    {
                        "version": 1,
                        "groups": {
                            primary: {"primary": primary, "aliases": list(aliases)}
                            for primary, aliases in groups.items()
                        },
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
            done_since_flush = 0
            last_flush = time.time()
            for keyword, names in payload_index.items():
                folder = self.data_dir / keyword
                for fname in names:
                    fpath = folder / fname
                    if not fpath.is_file():
                        continue
                    arc = f"{keyword}/{fname}"
                    zf.write(fpath, arc)
                    files_meta.append({"path": arc, "md5": self._md5(fpath)})
                    if progress is not None:
                        done_since_flush += 1
                        progress["done_files"] = len(files_meta)
                        try:
                            progress["done_bytes"] = progress.get("done_bytes", 0) + fpath.stat().st_size
                        except OSError:
                            pass
                        total = progress.get("total_files") or 0
                        if total:
                            progress["percent"] = int(len(files_meta) * 100 / total)
                        now = time.time()
                        if done_since_flush >= 50 or now - last_flush >= 0.5:
                            done_since_flush = 0
                            last_flush = now
            zf.writestr(
                "manifest.json",
                json.dumps(
                    {
                        "version": 2,
                        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "keywords": len(payload_index),
                        "only_keyword": only_keyword,
                        "images": len(files_meta),
                        "files": files_meta,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        return {
            "keywords": len(payload_index),
            "images": len(files_meta),
        }

    def _prune_backups(self, keep_name: Optional[str] = None) -> List[str]:
        """按份数与总量清理备份，返回被删文件名列表。

        保护名单：keep_name（本次刚生成）与 pre-restore_*（恢复前快照，只能人工删）。
        """
        # 逐条容错：某一份 stat 失败（被外部删除/权限变化）不能让整份清理静默失效（审计 M3）
        backups = []
        try:
            candidates = list(self.backup_root.glob("*.zip"))
        except OSError:
            return []
        for candidate in candidates:
            # 只淘汰自动备份与恢复快照：export_* 是用户主动导出的产物，不动
            if not (
                candidate.name.startswith("sticker_backup")
                or candidate.name.startswith("pre-restore")
            ):
                continue
            try:
                if not candidate.is_file():
                    continue
            except OSError:
                continue
            backups.append(candidate)
        protected = {name for name in (keep_name,) if name}
        removed: List[str] = []

        def _mtime(path: Path) -> float:
            # stat 可能失败（文件被外部删除/权限变化）：排序与淘汰都不能因此整份崩掉（审计 M3）
            try:
                return path.stat().st_mtime
            except OSError:
                return 0.0

        # pre-restore_* 不参与常规淘汰，但只保留最新一份：
        # 否则每次恢复都多 600MB 级快照，磁盘会被快照吃满（线上剩余空间 <5GB）。
        snapshots = sorted(
            (p for p in backups if p.name.startswith("pre-restore")),
            key=_mtime,
        )
        for stale_snapshot in snapshots[:-1]:
            try:
                stale_snapshot.unlink()
            except OSError as exc:
                logger.warning("删除旧恢复快照失败（%s）: %s", stale_snapshot.name, exc)
                continue
            backups.remove(stale_snapshot)
            removed.append(stale_snapshot.name)
        protected.update(p.name for p in backups if p.name.startswith("pre-restore"))

        def _size(path: Path) -> int:
            try:
                return path.stat().st_size
            except OSError:
                return 0

        backups.sort(key=_mtime)
        keep = self._backup_keep()
        total_limit = self._backup_total_mb() * 1024 * 1024

        def _victim() -> Optional[Path]:
            for candidate in backups:
                if candidate.name not in protected:
                    return candidate
            return None

        def _countable() -> int:
            # 份数上限只约束自动备份：pre-restore_* 受单独策略管，export_* 完全不参与（审计 M4）
            return sum(
                1
                for path in backups
                if path.name.startswith("sticker_backup")
            )

        while _countable() > keep:
            victim = _victim()
            if victim is None:
                break
            backups.remove(victim)
            try:
                victim.unlink()
            except OSError as exc:
                logger.warning("删除旧备份失败（%s）: %s", victim.name, exc)
                continue
            removed.append(victim.name)

        while sum(_size(p) for p in backups) > total_limit:
            victim = _victim()
            if victim is None:
                break
            backups.remove(victim)
            try:
                victim.unlink()
            except OSError as exc:
                logger.warning("删除旧备份失败（%s）: %s", victim.name, exc)
                continue
            removed.append(victim.name)

        if removed:
            logger.info("按保留策略清理备份 %d 份: %s", len(removed), ", ".join(removed))
        elif not backups:
            logger.info("保留策略：无备份可清理")
        return removed

    def _merge_aliases(self, groups: dict, restored_keywords: set) -> int:
        """把备份里的别名合并进当前别名表；主词必须存在（本次恢复或已有）。"""
        if not isinstance(groups, dict):
            return 0
        merged = 0
        self.alias_groups = self.__dict__.get("alias_groups") or {}
        known = set(self.index) | set(restored_keywords)
        for primary, payload in groups.items():
            primary = str(primary)
            if primary not in known:
                logger.warning("备份里的别名组主词 %s 不存在，跳过", primary)
                continue
            aliases = []
            if isinstance(payload, dict):
                aliases = [str(a).strip() for a in (payload.get("aliases") or [])]
            elif isinstance(payload, list):
                aliases = [str(a).strip() for a in payload]
            bucket = self.alias_groups.setdefault(primary, [])
            for alias in aliases:
                if not alias or alias == primary or alias in self.index:
                    continue
                if alias not in bucket:
                    bucket.append(alias)
                    merged += 1
        self._refresh_alias_map()
        if merged:
            try:
                self._save_groups()
            except OSError as exc:
                logger.error("保存合并后的别名表失败: %s", exc)
        return merged

    def _restore_from_zip(self, zip_path: Path) -> dict:
        """校验备份压缩包并合并恢复；重叠图片按 MD5 跳过。"""
        try:
            zip_file = zipfile.ZipFile(zip_path)
        except zipfile.BadZipFile as exc:
            raise ValueError(f"压缩包已损坏或不是有效的 zip 文件：{exc}") from exc
        with zip_file as zf:
            bad = zf.testzip()
            if bad is not None:
                raise ValueError(f"压缩包已损坏：{bad}")
            members = set(zf.namelist())
            if "index.json" not in members or "manifest.json" not in members:
                raise ValueError("压缩包格式不正确：缺少 index.json 或 manifest.json")
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
            if manifest.get("version") not in (1, 2):
                raise ValueError("不支持的备份版本")
            archive_index = json.loads(zf.read("index.json").decode("utf-8"))
            if not isinstance(archive_index, dict):
                raise ValueError("index.json 格式不正确")

            expected: set = set()
            archive_images = 0
            for keyword, names in archive_index.items():
                if not isinstance(names, list) or not names:
                    raise ValueError(f"index.json 关键词 {keyword} 格式不正确")
                if not self._is_valid_keyword(keyword):
                    raise ValueError(f"index.json 包含非法关键词：{keyword}")
                for fname in names:
                    if not isinstance(fname, str) or not fname:
                        raise ValueError(f"index.json 关键词 {keyword} 包含非法文件名")
                    arc = f"{keyword}/{fname}"
                    if arc not in members:
                        raise ValueError(f"压缩包缺少文件：{arc}")
                    expected.add(arc)
                    archive_images += 1
            if archive_images != manifest.get("images"):
                raise ValueError(
                    f"数据校验失败：manifest 记录 {manifest.get('images')} 张图片，"
                    f"实际索引 {archive_images} 张"
                )
            if len(archive_index) != manifest.get("keywords"):
                raise ValueError(
                    f"数据校验失败：manifest 记录 {manifest.get('keywords')} 个关键词，"
                    f"实际索引 {len(archive_index)} 个"
                )
            # 顺序硬约束：先把合法的单段元数据文件放行，再校验其余成员路径。
            # 反过来的话 v2 包会被判成"包含未索引的文件"直接抛错。
            meta_members = (
                "index.json",
                "manifest.json",
                "blocked_keywords.json",
                "aliases.json",
                "admin.json",
            )
            for member in members:
                if member in meta_members:
                    continue
                parts = Path(member).parts
                if len(parts) != 2 or ".." in parts or parts[0] in (".", ".."):
                    raise ValueError(f"压缩包包含非法路径：{member}")
                if member not in expected:
                    raise ValueError(f"压缩包包含未索引的文件：{member}")

            blocked_archived: list = []
            if "blocked_keywords.json" in members:
                raw = json.loads(zf.read("blocked_keywords.json").decode("utf-8"))
                if isinstance(raw, list):
                    blocked_archived = [str(x) for x in raw]

            added = skipped = overwritten = failed = 0
            for keyword, names in archive_index.items():
                folder = self.data_dir / keyword
                folder.mkdir(parents=True, exist_ok=True)
                for fname in names:
                    arc = f"{keyword}/{fname}"
                    tmp = folder / (
                        f".restore_tmp_{int(time.time() * 1000)}_"
                        f"{random.randint(1000, 9999)}"
                    )
                    try:
                        tmp.write_bytes(zf.read(arc))
                        md5 = self._md5(tmp)
                        target = folder / fname
                        if target.is_file():
                            if self._md5(target) == md5:
                                skipped += 1
                                tmp.unlink(missing_ok=True)
                                continue
                            shutil.move(str(tmp), str(target))
                            overwritten += 1
                        else:
                            shutil.move(str(tmp), str(target))
                            added += 1
                        if fname not in self.index.setdefault(keyword, []):
                            self.index[keyword].append(fname)
                    except Exception as exc:
                        failed += 1
                        logger.error("恢复图片失败 %s: %s", arc, exc)
                        try:
                            tmp.unlink(missing_ok=True)
                        except OSError:
                            pass

            old_blocked = set(self.blocked_keywords)
            self.blocked_keywords.update(blocked_archived)
            if self.blocked_keywords != old_blocked:
                self._save_blocked()
            self._save_index()

            # 别名表：v2 包才可能有；缺失视为"无别名"，不影响 v1 恢复
            aliases_merged = 0
            if "aliases.json" in members:
                try:
                    payload = json.loads(zf.read("aliases.json").decode("utf-8"))
                    groups = payload.get("groups") if isinstance(payload, dict) else None
                    aliases_merged = self._merge_aliases(groups or {}, set(archive_index))
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    logger.warning("备份里的 aliases.json 无法解析，已跳过：%s", exc)

        return {
            "archive_keywords": len(archive_index),
            "aliases_merged": aliases_merged,
            "archive_images": archive_images,
            "added": added,
            "skipped": skipped,
            "overwritten": overwritten,
            "failed": failed,
            "blocked_merged": len(blocked_archived),
            "message": (
                f"恢复完成：归档 {len(archive_index)} 个关键词 / {archive_images} 张图片；"
                f"新增 {added} 张，覆盖 {overwritten} 张，跳过重复 {skipped} 张，"
                f"失败 {failed} 张"
            ),
        }

    # ------------------------------------------------------------------
    # H. 屏蔽关键词（仅管理员）
    # ------------------------------------------------------------------
    # ---------------- 管理员授予 ----------------
    def _admin_entries(self) -> list:
        return [
            str(a).strip()
            for a in (self.config.get("admin_users", []) or [])
            if str(a).strip()
        ]

    def _extract_admin_targets(self, event) -> list:
        """三路兜底取目标 ID：消息链 At → 正文 <@id> → 引用消息发送者。

        官方通道 @他人不产生 At 组件（只留字面量），所以正文正则不是可选项。
        """
        found: list = []
        self_id = ""
        try:
            self_id = str(event.get_self_id() or "")
        except Exception:
            self_id = ""

        def _push(value) -> None:
            text = str(value or "").strip()
            if not text or text in ADMIN_ID_PLACEHOLDERS:
                return
            if self_id and text == self_id:
                return
            if text not in found:
                found.append(text)

        for component in platform_compat.message_components(event):
            if type(component).__name__ == "At":
                _push(getattr(component, "qq", None))

        raw_text = ""
        try:
            raw_text = str(event.message_str or "")
        except Exception:
            raw_text = ""
        for match in AT_ID_RE.finditer(raw_text):
            _push(match.group(1))

        try:
            reply = getattr(getattr(event, "message_obj", None), "message", None) or []
        except Exception:
            reply = []
        for component in reply:
            if type(component).__name__ == "Reply":
                _push(getattr(component, "sender_id", None))

        return found

    def _audit_admin(self, action: str, actor: str, target: str, result: str) -> None:
        """管理员变更留痕（jsonl，一行一条；写失败只警告不阻断）。"""
        line = json.dumps(
            {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "action": action,
                "actor": actor,
                "target": target,
                "result": result,
            },
            ensure_ascii=False,
        )
        try:
            path = self.data_dir / "admin_audit.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError as exc:
            logger.warning("写入管理员审计失败: %s", exc)

    def _write_admins(self, entries: list, *, actor: str, action: str, target: str) -> None:
        self.config["admin_users"] = entries
        self.config.save_config()
        self._audit_admin(action, actor, target, "ok")

    def _mask_id(self, value: str) -> str:
        text = str(value or "")
        if len(text) <= 8:
            return text
        return f"{text[:4]}…{text[-4:]}"

    async def _handle_grant_admin(self, event: AstrMessageEvent, message_str: str):
        admins = self._admin_entries()
        actor = str(event.get_sender_id() or "")
        if not admins:
            # 决策 A：首个管理员只能来自 WebUI，避免"谁先说谁是管理员"
            yield event.plain_result(
                "当前还没有管理员：请先在 AstrBot WebUI 的「表情包管理」页配置管理员，"
                "之后才能用本指令授予他人"
            )
            self._audit_admin("grant", actor, "-", "no-admin-bootstrap")
            return
        if not self._is_admin(event):
            yield event.plain_result("此指令仅限管理员")
            self._audit_admin("grant", actor, "-", "denied")
            return

        raw = message_str[len("授予管理员") :].strip()
        targets = self._extract_admin_targets(event)
        if raw:
            targets.append(raw)
        targets = [t for t in dict.fromkeys(targets) if t not in ADMIN_ID_PLACEHOLDERS]
        if not targets:
            yield event.plain_result(
                "用法：@某人 后发「授予管理员」，或直接发「授予管理员{ID}」"
            )
            return

        platform_id = str(event.get_platform_id() or "")
        added, existed = [], []
        entries = list(admins)
        for target in targets:
            entry = platform_compat.scoped_key(platform_id, target)
            if any(
                platform_compat.admin_entry_matches(existing, platform_id, target)
                for existing in entries
            ):
                existed.append(target)
                continue
            entries.append(entry)
            added.append(entry)
        if added:
            try:
                self._write_admins(entries, actor=actor, action="grant", target=",".join(added))
            except Exception as exc:
                logger.error("保存管理员失败: %s", exc)
                yield event.plain_result(f"保存失败：{exc}")
                return
        parts = []
        if added:
            parts.append(f"已授予 {len(added)} 人管理员：" + "、".join(self._mask_id(x) for x in added))
        if existed:
            parts.append("已是管理员：" + "、".join(self._mask_id(x) for x in existed))
        yield event.plain_result("；".join(parts) or "没有变化")

    async def _handle_revoke_admin(self, event: AstrMessageEvent, message_str: str):
        admins = self._admin_entries()
        actor = str(event.get_sender_id() or "")
        if not admins:
            yield event.plain_result(
                "当前还没有管理员：请先在 AstrBot WebUI 的「表情包管理」页配置管理员"
            )
            return
        if not self._is_admin(event):
            yield event.plain_result("此指令仅限管理员")
            self._audit_admin("revoke", actor, "-", "denied")
            return

        raw = message_str[len("撤销管理员") :].strip()
        targets = self._extract_admin_targets(event)
        if raw:
            targets.append(raw)
        targets = [t for t in dict.fromkeys(targets) if t not in ADMIN_ID_PLACEHOLDERS]
        if not targets:
            yield event.plain_result(
                "用法：@某人 后发「撤销管理员」，或直接发「撤销管理员{ID}」"
            )
            return

        platform_id = str(event.get_platform_id() or "")
        entries = list(admins)
        removed = []
        for target in targets:
            keep = [
                entry
                for entry in entries
                if not platform_compat.admin_entry_matches(entry, platform_id, target)
            ]
            if len(keep) == len(entries):
                continue
            if not keep:
                yield event.plain_result("不能撤销最后一个管理员（否则没人能再管理）")
                self._audit_admin("revoke", actor, target, "last-admin")
                return
            entries = keep
            removed.append(target)
        if not removed:
            yield event.plain_result("目标不是管理员")
            return
        try:
            self._write_admins(entries, actor=actor, action="revoke", target=",".join(removed))
        except Exception as exc:
            logger.error("保存管理员失败: %s", exc)
            yield event.plain_result(f"保存失败：{exc}")
            return
        yield event.plain_result(f"已撤销 {len(removed)} 人管理员权限")

    async def _handle_admin_list(self, event: AstrMessageEvent):
        admins = self._admin_entries()
        if not admins:
            yield event.plain_result(
                "当前还没有管理员：请先在 AstrBot WebUI 的「表情包管理」页配置"
            )
            return
        lines = [f"管理员（{len(admins)}）："]
        for entry in admins:
            if ":" in entry:
                pid, uid = entry.split(":", 1)
                lines.append(f"- {self._mask_id(uid)}（平台 {pid}）")
            else:
                lines.append(f"- {self._mask_id(entry)}（全平台）")
        yield event.plain_result("\n".join(lines))

    # ---------------- 维护：孤儿对账 / 去重 ----------------
    def _scan_orphans(self) -> dict:
        """只读对账：磁盘有但索引没有、索引有但磁盘没有、空目录。"""
        disk = {}
        try:
            for entry in self.data_dir.iterdir():
                if not entry.is_dir() or entry.name in ("__pycache__",):
                    continue
                try:
                    files = [f.name for f in entry.iterdir() if f.is_file()]
                except OSError:
                    continue
                disk[entry.name] = files
        except OSError as exc:
            logger.error("扫描贴纸目录失败: %s", exc)
            return {"error": str(exc)}

        indexed = {k: list(v) for k, v in self.index.items()}
        disk_only_keywords = sorted(set(disk) - set(indexed))
        index_only_keywords = sorted(set(indexed) - set(disk))
        missing_files = []
        for keyword, names in indexed.items():
            present = set(disk.get(keyword, []))
            for name in names:
                if name not in present:
                    missing_files.append(f"{keyword}/{name}")
        empty_dirs = sorted(k for k, files in disk.items() if not files)
        return {
            "disk_only_keywords": disk_only_keywords,
            "index_only_keywords": index_only_keywords,
            "missing_files": missing_files[:200],
            "missing_files_total": len(missing_files),
            "empty_dirs": empty_dirs,
        }

    def _dedupe_by_hardlink(self, dry_run: bool = True) -> dict:
        """把内容相同的图片归并成硬链接（同一份数据只占一次空间）。

        只链不删：每个关键词目录里的文件名都保留，索引零改动，可逆。
        """
        groups: dict = {}
        for keyword, names in self.index.items():
            folder = self.data_dir / keyword
            for name in names:
                path = folder / name
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if stat.st_nlink > 1:
                    continue
                key = (stat.st_size, self._md5(path))
                groups.setdefault(key, []).append(path)

        plans = []
        reclaimed = 0
        for (size, _digest), paths in groups.items():
            if len(paths) < 2:
                continue
            paths.sort(key=lambda p: p.stat().st_mtime)
            keep, duplicates = paths[0], paths[1:]
            plans.append(
                {
                    "keep": str(keep.relative_to(self.data_dir)),
                    "linked": [str(p.relative_to(self.data_dir)) for p in duplicates],
                    "bytes": size * len(duplicates),
                }
            )
            reclaimed += size * len(duplicates)

        result = {
            "dry_run": dry_run,
            "groups": len(plans),
            "files": sum(len(item["linked"]) for item in plans),
            "reclaimed_mb": round(reclaimed / 1048576, 1),
            "plans": plans[:200],
        }
        if dry_run:
            return result

        done, failed = 0, 0
        for item in plans:
            keep = self.data_dir / item["keep"]
            for rel in item["linked"]:
                target = self.data_dir / rel
                try:
                    tmp = target.with_suffix(target.suffix + ".lnk")
                    if tmp.exists():
                        tmp.unlink()
                    os.link(keep, tmp)
                    os.replace(tmp, target)
                    done += 1
                except OSError as exc:
                    failed += 1
                    logger.warning("硬链接归并失败（%s）: %s", rel, exc)
                    try:
                        if tmp.exists():
                            tmp.unlink()
                    except OSError:
                        pass
        result["done"] = done
        result["failed"] = failed
        if done:
            logger.info("硬链接去重完成：%d 个文件，回收约 %.1fMB", done, result["reclaimed_mb"])
        return result

    # ---------------- 别名组指令 ----------------
    async def _handle_bind(self, event: AstrMessageEvent, message_str: str):
        """绑定{别名}到{主词}：单层多对一，别名不得当组名。"""
        if not self._is_admin(event):
            yield event.plain_result("此指令仅限管理员")
            return
        payload = message_str[len("绑定") :].strip()
        if "到" not in payload:
            yield event.plain_result("用法：绑定{别名}到{主词}")
            return
        alias, primary = (part.strip() for part in payload.split("到", 1))
        if not alias or not primary:
            yield event.plain_result("用法：绑定{别名}到{主词}")
            return
        if alias == primary:
            yield event.plain_result("别名与主词相同，无需绑定")
            return
        if not self._is_valid_keyword(alias) or not self._is_valid_keyword(primary):
            yield event.plain_result(
                "关键词不合法（不能是保留指令名、Windows 保留名，结尾也不能带点或空格）"
            )
            return
        if primary in self.alias_map:
            yield event.plain_result(
                f"{primary} 本身是 {self.alias_map[primary]} 的别名，别名不能再作为主词"
            )
            return
        if primary not in self.index:
            yield event.plain_result(f"主词 {primary} 下不存在图片，请先添加")
            return
        if alias in self.index:
            yield event.plain_result(f"{alias} 已经是真实关键词，别名不能与它同名")
            return
        if alias in self.alias_map:
            current = self.alias_map[alias]
            if current == primary:
                yield event.plain_result(f"{alias} 已经绑定到 {primary}")
                return
            yield event.plain_result(
                f"{alias} 已绑定到 {current}，请先执行 解绑{alias}"
            )
            return
        self.alias_groups.setdefault(primary, [])
        if alias not in self.alias_groups[primary]:
            self.alias_groups[primary].append(alias)
        self._refresh_alias_map()
        try:
            self._save_groups()
        except OSError as exc:
            logger.error("保存别名表失败: %s", exc)
            yield event.plain_result(f"保存别名失败：{exc}")
            return
        yield event.plain_result(f"已绑定：{alias} → {primary}（来只{alias} 等同 来只{primary}）")

    async def _handle_unbind(self, event: AstrMessageEvent, message_str: str):
        if not self._is_admin(event):
            yield event.plain_result("此指令仅限管理员")
            return
        alias = message_str[len("解绑") :].strip()
        if not alias:
            yield event.plain_result("用法：解绑{别名}")
            return
        primary = self.alias_map.get(alias)
        if not primary:
            yield event.plain_result(f"{alias} 不是任何关键词的别名")
            return
        aliases = self.alias_groups.get(primary) or []
        if alias in aliases:
            aliases.remove(alias)
        if not aliases:
            self.alias_groups.pop(primary, None)
        self._refresh_alias_map()
        try:
            self._save_groups()
        except OSError as exc:
            logger.error("保存别名表失败: %s", exc)
            yield event.plain_result(f"保存别名失败：{exc}")
            return
        yield event.plain_result(f"已解绑：{alias}")

    async def _handle_alias_list(self, event: AstrMessageEvent, message_str: str):
        keyword = message_str[len("别名") :].strip()
        if not keyword:
            if not self.alias_groups:
                yield event.plain_result("还没有任何别名绑定")
                return
            lines = ["别名总览："]
            for primary, aliases in sorted(self.alias_groups.items()):
                lines.append(f"- {primary} ← {('、'.join(aliases))}")
            yield event.plain_result("\n".join(lines))
            return
        keyword, how = self._resolve_keyword(keyword)
        if not keyword:
            yield event.plain_result(f"关键词 {message_str[len('别名'):].strip()} 不存在")
            return
        aliases = self.alias_groups.get(keyword) or []
        if not aliases:
            yield event.plain_result(f"{keyword} 没有别名")
            return
        note = "" if how == "exact" else "（按别名解析）"
        yield event.plain_result(
            f"{keyword}{note} 的别名：{'、'.join(aliases)}"
        )

    async def _handle_block(self, event: AstrMessageEvent, message_str: str):
        if not self._is_admin(event):
            yield event.plain_result("此指令仅限管理员")
            return

        keyword = message_str[2:].strip()
        raw_keyword = keyword
        keyword, _how = self._resolve_keyword(raw_keyword)
        if not keyword:
            yield event.plain_result('关键词 ' + raw_keyword + ' 不存在')
            return
        if not keyword:
            yield event.plain_result("用法：屏蔽{关键词}")
            return
        if not self._is_valid_keyword(keyword):
            yield event.plain_result("关键词不合法（不能是 Windows 保留名，结尾也不能带点或空格）")
            return
        if keyword in self.blocked_keywords:
            yield event.plain_result(f"关键词 {keyword} 已在屏蔽列表中")
            return

        self.blocked_keywords.add(keyword)
        self._save_blocked()
        yield event.plain_result(f"已屏蔽关键词：{keyword}")

    async def _handle_unblock(self, event: AstrMessageEvent, message_str: str):
        if not self._is_admin(event):
            yield event.plain_result("此指令仅限管理员")
            return

        keyword = message_str[4:].strip()
        if not keyword:
            yield event.plain_result("用法：解除屏蔽{关键词}")
            return
        if keyword not in self.blocked_keywords:
            yield event.plain_result(f"关键词 {keyword} 不在屏蔽列表中")
            return

        self.blocked_keywords.discard(keyword)
        self._save_blocked()
        yield event.plain_result(f"已解除屏蔽：{keyword}")

    async def _handle_block_list(self, event: AstrMessageEvent, message_str: str):
        if not self._is_admin(event):
            yield event.plain_result("此指令仅限管理员")
            return

        if not self.blocked_keywords:
            yield event.plain_result("暂无屏蔽关键词")
            return
        yield event.plain_result(
            "屏蔽关键词列表：\n" + "\n".join(sorted(self.blocked_keywords))
        )

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_reply_images(event: AstrMessageEvent) -> Optional[List[Image]]:
        """从消息链的 Reply 组件中提取被引用消息的全部 Image。

        返回 None 表示消息没有引用任何消息；返回 [] 表示引用了消息但其中没有图片。
        """
        message_obj = getattr(event, "message_obj", None)
        if message_obj is None:
            return None
        chain = getattr(message_obj, "message", None) or []
        for comp in chain:
            if isinstance(comp, Reply):
                reply_chain = getattr(comp, "chain", None) or []
                images = [c for c in reply_chain if isinstance(c, Image)]
                return images
        return None

    def _forward_key(self, event: AstrMessageEvent) -> str:
        group = event.get_group_id()
        sender = event.get_sender_id() or "unknown"
        if group:
            base = f"group:{group}:{sender}"
        else:
            base = f"user:{sender}"
        return platform_compat.scoped_key(event.get_platform_id(), base)

    @staticmethod
    def _is_forward_event(event: AstrMessageEvent) -> bool:
        """是否为合并转发消息（官方 102 / OneBot Forward|Node|Nodes）。"""
        return platform_compat.is_forward_event(event)

    async def _process_forward_add(self, event: AstrMessageEvent):
        """处于“等待合并转发”状态时，解析本次转发并批量添加。"""
        key = self._forward_key(event)
        state = self.pending_forward_add.get(key)
        if not state:
            return
        self.pending_forward_add.pop(key, None)
        if time.time() - state["time"] > FORWARD_ADD_TTL:
            yield event.plain_result(
                "等待超时，请重新发送 批量添加合并转发{关键词} 后再发合并转发"
            )
            return
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        payload = await platform_compat.extract_forward(event)
        images = list(payload.images) if payload is not None else []
        saw_forward_body = bool(payload.saw_body) if payload is not None else False

        if not images:
            logger.warning(
                "StickerPlugin 合并转发解析失败，原始结构: %s",
                platform_compat.describe_raw_structure(raw),
            )
            if saw_forward_body:
                yield event.plain_result("该合并转发中没有解析到图片")
            else:
                yield event.plain_result("未从合并转发中解析到内容，请重试")
            return

        logger.info("StickerPlugin 合并转发解析到 %d 张图片", len(images))
        keyword = state["keyword"]
        if keyword in self.blocked_keywords:
            yield event.plain_result(f"关键词 {keyword} 已被屏蔽，无法添加")
            return
        added, skipped, failed, blocked = await self._add_images(keyword, images)
        if blocked:
            yield event.plain_result(f"添加失败：{blocked}")
            return
        if failed:
            yield event.plain_result(
                f"已从合并转发添加 {added} 张图片"
                f"（跳过 {skipped} 张重复，{failed} 张下载失败）"
            )
        else:
            yield event.plain_result(
                f"已从合并转发添加 {added} 张图片（跳过 {skipped} 张重复）"
            )

    def _extract_quoted_forward_images(
        self, event: AstrMessageEvent
    ) -> Optional[List[Image]]:
        """尝试直接从引用消息的原始 msg_elements 中提取合并转发图片。"""
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if raw is None:
            return None
        elements = getattr(raw, "msg_elements", None)
        if not isinstance(elements, list) or not elements:
            return None
        urls = self._collect_forward_urls([elements[0]])
        if not urls:
            return None
        return [Image.fromURL(url) for url in urls]

    @classmethod
    def _collect_forward_images(cls, elements: list) -> List[Image]:
        """递归提取合并转发元素中的全部图片，返回去重后的 Image 列表。"""
        return [Image.fromURL(url) for url in cls._collect_forward_urls(elements)]

    @classmethod
    def _collect_forward_urls(cls, elements) -> List[str]:
        """兼容层委托：递归提取合并转发元素中的图片 URL。"""
        return platform_compat.collect_forward_urls(elements)

    @classmethod
    def _extract_forward_summary_urls(cls, content: str) -> List[str]:
        """兼容层委托：从 QQ 官方 102 摘要文本中提取图片 URL。"""
        return platform_compat.extract_forward_summary_urls(content)

    @staticmethod
    def _describe_raw_structure(raw) -> str:
        """兼容层委托：简要描述原始消息结构。"""
        return platform_compat.describe_raw_structure(raw)

    #: Windows 会把这些名字当设备名，not 目录名；创建目录会失败（BUG-022）
    _WINDOWS_RESERVED = frozenset(
        {"CON", "PRN", "AUX", "NUL"}
        | {f"COM{i}" for i in range(1, 10)}
        | {f"LPT{i}" for i in range(1, 10)}
    )

    @staticmethod
    def _is_valid_keyword(keyword: str) -> bool:
        if not keyword:
            return False
        if keyword in RESERVED_KEYWORDS:
            return False
        if re.search(r"\s", keyword):
            return False
        # Windows 保留名（含扩展名形式，如 con.txt）与结尾点/空格：目录建不出来
        if keyword.split(".")[0].upper() in StickerPlugin._WINDOWS_RESERVED:
            return False
        if keyword != keyword.rstrip(" ."):
            return False
        # 防御非法目录名 / 路径穿越
        if keyword in (".", "..") or "/" in keyword or "\\" in keyword:
            return False
        if any(ch in keyword for ch in '<>:"|?*'):
            return False
        return True

    @staticmethod
    def _parse_list_command(rest: str) -> Tuple[str, int]:
        """解析列表参数：关键词 + 可选页码（末尾数字）。"""
        match = re.search(r"(\d+)\s*$", rest)
        if match:
            return rest[: match.start()].strip(), int(match.group(1))
        return rest, 1

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        admins = [
            str(a).strip()
            for a in (self.config.get("admin_users", []) or [])
            if str(a).strip()
        ]
        sender_id = str(event.get_sender_id() or "")
        platform_id = str(event.get_platform_id() or "")
        is_admin = any(
            platform_compat.admin_entry_matches(entry, platform_id, sender_id)
            for entry in admins
        )
        if not is_admin:
            logger.warning(
                "StickerPlugin 管理员校验未通过: platform=%s sender_id=%s admin_users=%s",
                platform_id,
                sender_id,
                admins,
            )
        return is_admin

    def _prune_missing(self, keyword: str) -> List[str]:
        """自修复：移除索引中物理文件已丢失的条目，返回仍存在的文件名列表。"""
        folder = self.data_dir / keyword
        existing: List[str] = []
        changed = False
        for name in list(self.index.get(keyword, [])):
            if (folder / name).is_file():
                existing.append(name)
            else:
                changed = True
                logger.warning("自修复：移除不存在的图片索引 %s/%s", keyword, name)
        if changed:
            if existing:
                self.index[keyword] = existing
            else:
                self.index.pop(keyword, None)
            self._save_index()
        return existing

    @staticmethod
    def _md5(path: Path, chunk_size: int = 1 << 20) -> str:
        """算文件 MD5。默认 1MB 分块：8KB 分块实测只有 254MB/s，1MB 是 630MB/s。"""
        digest = hashlib.md5()
        with open(path, "rb") as fp:
            for chunk in iter(lambda: fp.read(chunk_size), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _folder_md5s(self, keyword: str) -> dict:
        """返回该关键词目录的 {文件名: md5}，带缓存。

        缓存按 (size, mtime_ns) 校验，不只是文件名——同名文件被删后重建、内容变了，
        光看名字会拿到旧 MD5（用例 test_folder_md5s_recomputes_changed_content 覆盖）。
        缓存不读文件内容，只多一次 stat，因此删图/手工改/恢复备份都不需要显式失效。
        """
        folder = self.data_dir / keyword
        cache = self.__dict__.setdefault("_md5_cache", {})
        cached = cache.setdefault(keyword, {})
        store = self.__dict__.get("_store")
        current = {}
        try:
            for entry in folder.iterdir():
                try:
                    stat = entry.stat()
                except OSError:
                    continue
                if entry.is_file():
                    current[entry.name] = (stat.st_size, stat.st_mtime_ns)
        except OSError:
            current = {}

        if not cached and store is not None:
            # 冷启动用库里的 MD5 做种子：否则首次添加仍要把整个目录重算一遍。
            # 种子必须带上真实 (size, mtime_ns)，否则会被下面的校验判为失效又重算。
            try:
                for name, digest in store.md5s_of(keyword).items():
                    if name in current:
                        size, mtime_ns = current[name]
                        cached[name] = (digest, size, mtime_ns)
            except Exception as exc:
                logger.warning("读取 SQLite MD5 失败（将重算）：%s", exc)

        result = {}
        for name, (size, mtime_ns) in current.items():
            hit = cached.get(name)
            if hit and hit[1] == size and hit[2] == mtime_ns:
                result[name] = hit[0]
                continue
            try:
                digest = self._md5(folder / name)
            except OSError as exc:
                logger.warning("计算 MD5 失败（%s/%s）: %s", keyword, name, exc)
                continue
            result[name] = digest
            cached[name] = (digest, size, mtime_ns)

        for stale in [name for name in cached if name not in current]:
            cached.pop(stale, None)
        if store is not None:
            try:
                store.set_md5s(keyword, result)
            except Exception as exc:
                logger.warning("回写 SQLite MD5 失败：%s", exc)
        return result

    def _remember_md5(self, keyword: str, filename: str, digest: str) -> None:
        """新增文件后把 MD5 写进缓存（避免同一批次里对刚写的文件再算一次）。"""
        cache = self.__dict__.setdefault("_md5_cache", {})
        cached = cache.setdefault(keyword, {})
        try:
            stat = (self.data_dir / keyword / filename).stat()
        except OSError:
            cached.pop(filename, None)
            return
        cached[filename] = (digest, stat.st_size, stat.st_mtime_ns)

    @property
    def db_path(self) -> Path:
        """惰性路径：测试里直接改 data_dir 时不会写到真实目录。"""
        return self.data_dir / "index.db"

    def _md5_for_sync(self, keyword: str, filename: str):
        """给 Store.sync_index 用的增量 MD5 提供器：缓存里没有就返回 None（列留空后续补）。"""
        cache = self.__dict__.get("_md5_cache") or {}
        hit = (cache.get(keyword) or {}).get(filename)
        if not hit:
            return None, None, None
        digest = hit[0] if isinstance(hit, tuple) else hit
        try:
            stat_info = (self.data_dir / keyword / filename).stat()
            return digest, stat_info.st_size, Path(filename).suffix.lower()
        except OSError:
            return digest, None, Path(filename).suffix.lower()

    def _init_storage(self) -> None:
        """建立 SQLite 存储（失败或 STICKER_STORAGE=json 时保持纯 JSON）。"""
        if _store_mod.storage_mode() == "json":
            logger.info("存储模式：json（STICKER_STORAGE=json，不启用 SQLite）")
            return
        try:
            store = _store_mod.Store(self.db_path)
        except Exception as exc:
            logger.error("打开 SQLite 失败，回落纯 JSON：%s", exc)
            return
        self._store = store
        try:
            counts = store.counts()
            if counts["images"] == 0:
                # 库空：把 JSON 索引迁移进来（幂等，迁移前先留一份 .bak）
                index_file = self.data_dir / "index.json"
                if index_file.exists():
                    try:
                        backup = index_file.with_suffix(".json.bak")
                        if not backup.exists():
                            backup.write_bytes(index_file.read_bytes())
                    except OSError:
                        pass
                expected = sum(len(v) for v in self.index.values())
                migrated = store.migrate(self.index, self._md5_for_sync)
                if migrated["images"] != expected:
                    raise RuntimeError(
                        f"迁移校验失败：JSON {expected} 张 vs DB {migrated['images']} 张"
                    )
                logger.info(
                    "迁移完成并校验通过：%d 关键词 / %d 张",
                    migrated["keywords"],
                    migrated["images"],
                )
            else:
                loaded = store.load_index()
                if loaded:
                    self.index = loaded
                    logger.info(
                        "索引已从 SQLite 载入：%d 关键词 / %d 张",
                        len(loaded),
                        sum(len(v) for v in loaded.values()),
                    )
        except Exception as exc:
            logger.error("SQLite 初始化失败，回落纯 JSON：%s", exc)
            try:
                store.close()
            except Exception:
                pass
            self._store = None
            # DB 载入可能已覆盖过内存索引：回落时以 JSON 为准重新读一次
            self._load_index()

    # ---------------- 别名组 ----------------
    def _load_groups(self) -> None:
        """读取 keyword_groups.json；损坏时按"无别名"处理，不阻塞启动。"""
        self.alias_groups = {}
        try:
            if self.alias_path.exists():
                raw = json.loads(self.alias_path.read_text(encoding="utf-8"))
                groups = (raw or {}).get("groups") if isinstance(raw, dict) else None
                if isinstance(groups, dict):
                    for primary, payload in groups.items():
                        aliases = []
                        if isinstance(payload, dict):
                            aliases = [
                                str(a).strip()
                                for a in (payload.get("aliases") or [])
                                if str(a).strip() and str(a).strip() != primary
                            ]
                        if aliases:
                            self.alias_groups[str(primary)] = aliases
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("读取别名表失败（按无别名处理）: %s", exc)
        self._refresh_alias_map()

    def _refresh_alias_map(self) -> None:
        mapping = {}
        for primary, aliases in self.alias_groups.items():
            for alias in aliases:
                mapping[alias] = primary
        self.alias_map = mapping

    def _save_groups(self) -> None:
        payload = {
            "version": 1,
            "groups": {
                primary: {"primary": primary, "aliases": list(aliases)}
                for primary, aliases in self.alias_groups.items()
            },
        }
        _atomic_write_json(self.alias_path, payload)

    def _resolve_keyword(self, raw: str):
        """唯一解析口：真关键词优先，其次别名。返回 (关键词或 None, 方式)。"""
        text = (raw or "").strip()
        if not text:
            return None, "empty"
        index = getattr(self, "index", None) or {}
        alias_map = getattr(self, "alias_map", None) or {}
        if text in index:
            return text, "exact"
        target = alias_map.get(text)
        if target and target in index:
            return target, "alias"
        return None, "unknown"

    def _load_index(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if self.index_path.exists():
            try:
                raw = json.loads(self.index_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self.index = {
                        str(k): [str(x) for x in v if isinstance(x, str)]
                        for k, v in raw.items()
                        if isinstance(v, list)
                    }
            except (json.JSONDecodeError, OSError) as exc:
                logger.error("读取索引文件失败，将使用空索引: %s", exc)
                self.index = {}
        else:
            self.index = {}
            self._save_index()

    def _load_blocked(self):
        self.blocked_keywords = set()
        if self.blocked_path.exists():
            try:
                raw = json.loads(self.blocked_path.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    self.blocked_keywords = {str(x) for x in raw if isinstance(x, str)}
            except (json.JSONDecodeError, OSError) as exc:
                logger.error("读取屏蔽列表失败: %s", exc)

    def _save_blocked(self):
        try:
            _atomic_write_json(self.blocked_path, sorted(self.blocked_keywords))
        except OSError as exc:
            logger.error("保存屏蔽列表失败: %s", exc)

    def _save_index(self):
        # JSON 镜像始终写：旧版本可读、备份格式不变、DB 故障时仍能降级
        try:
            _atomic_write_json(self.index_path, self.index)
        except OSError as exc:
            logger.error("保存索引失败: %s", exc)
        store = self.__dict__.get("_store")
        if store is not None:
            try:
                store.sync_index(self.index, self._md5_for_sync)
            except Exception as exc:
                logger.error("写入 SQLite 失败（JSON 镜像仍有效）: %s", exc)

    def _check_storage(self):
        """启动时检查一次并告警（真正的拦截在 _enforce_storage，每次新增前调用）。"""
        usage = self._storage_usage()
        if usage["used_mb"] > usage["limit_mb"]:
            logger.warning(
                "表情包存储已超过 %dMB（当前 %.1fMB，共 %d 张；备份另占 %.1fMB / %d 份），请清理",
                usage["limit_mb"],
                usage["used_mb"],
                usage["files"],
                usage["backup_mb"],
                usage["backup_count"],
            )
