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

import hashlib
import json
import random
import re
import shutil
import time
import zipfile
from pathlib import Path
from typing import List, Optional, Tuple

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Reply
from astrbot.api.star import Context, Star
from astrbot.api.web import error_response, file_response, json_response, request

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
}
# 二次删除确认的有效期（秒）
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
# 允许保留的图片扩展名（convert_to_file_path 可能返回无后缀的临时文件）
ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


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
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(self, f, ensure_ascii=False, indent=2)
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
        # 索引结构：{"关键词": ["文件名1.png", ...]}
        self.index: dict = {}
        # 批量删除二次确认：{user_id: {"keyword": str, "time": float}}
        self.pending_delete: dict = {}
        # 屏蔽关键词集合
        self.blocked_keywords: set = set()
        # 等待合并转发的添加状态：{key: {"keyword": str, "time": float}}
        self.pending_forward_add: dict = {}
        self._load_index()
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
    @filter.platform_adapter_type(filter.PlatformAdapterType.QQOFFICIAL)
    @filter.event_message_type(
        filter.EventMessageType.GROUP_MESSAGE
        | filter.EventMessageType.PRIVATE_MESSAGE
    )
    async def on_message(self, event: AstrMessageEvent):
        message_str = (event.message_str or "").strip()
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
        if not keyword:
            yield event.plain_result("用法：添加{关键词}（请回复包含图片的消息）")
            return
        if not self._is_valid_keyword(keyword):
            yield event.plain_result("关键词不合法")
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

        added, skipped, failed = await self._add_images(keyword, images)
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
        keyword = message_str[len("批量添加合并转发") :].strip()
        if not keyword:
            yield event.plain_result("用法：批量添加合并转发{关键词}")
            return
        if not self._is_valid_keyword(keyword):
            yield event.plain_result("关键词不合法")
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
    ) -> Tuple[int, int, int]:
        """把图片批量写入关键词目录（MD5 去重），返回 (新增, 跳过重复, 下载失败)。"""
        folder = self.data_dir / keyword
        folder.mkdir(parents=True, exist_ok=True)
        existing_md5 = set()
        for f in folder.iterdir():
            if f.is_file():
                try:
                    existing_md5.add(self._md5(f))
                except OSError:
                    continue

        added = 0
        skipped = 0
        failed = 0
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
            self.index.setdefault(keyword, []).append(filename)
            added += 1

        self._save_index()
        return added, skipped, failed

    # ------------------------------------------------------------------
    # B. 发送（来只{关键词}）
    # ------------------------------------------------------------------
    async def _handle_send(self, event: AstrMessageEvent, message_str: str):
        keyword = message_str[2:].strip()
        if not keyword:
            yield event.plain_result("用法：来只{关键词}")
            return
        if not self._is_valid_keyword(keyword):
            yield event.plain_result("关键词不合法")
            return
        if keyword in self.blocked_keywords:
            yield event.plain_result(f"关键词 {keyword} 已被屏蔽，无法发送")
            return

        # 自修复：索引中存在但文件已丢失的条目自动清理
        files = self._prune_missing(keyword)
        if not files:
            yield event.plain_result(f"关键词 {keyword} 下还不存在图片")
            return

        filename = random.choice(files)
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
            yield event.plain_result("关键词不合法")
            return
        if keyword in self.blocked_keywords:
            yield event.plain_result(f"关键词 {keyword} 已被屏蔽")
            return

        files = self._prune_missing(keyword)
        if not files:
            yield event.plain_result("该关键词下没有图片")
            return

        total_pages = (len(files) + PAGE_SIZE - 1) // PAGE_SIZE
        if page < 1 or page > total_pages:
            yield event.plain_result("页码超出范围")
            return

        start = (page - 1) * PAGE_SIZE
        lines = [
            f"[{idx}] {name}"
            for idx, name in enumerate(files[start : start + PAGE_SIZE], start=start + 1)
        ]
        lines.append(f"第 {page}/{total_pages} 页，共 {len(files)} 张")
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
            yield event.plain_result("关键词不合法")
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
        if keyword and not self._is_valid_keyword(keyword):
            yield event.plain_result("关键词不合法")
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
        if not keyword:
            yield event.plain_result("用法：删除{关键词}")
            return
        if not self._is_valid_keyword(keyword):
            yield event.plain_result("关键词不合法")
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

        total_keywords = len(self.index)
        total_images = sum(len(v) for v in self.index.values())
        top3 = sorted(self.index.items(), key=lambda item: len(item[1]), reverse=True)[:3]
        lines = [
            f"关键词数：{total_keywords}",
            f"图片总数：{total_images}",
            "图片数 Top 3：",
        ]
        for keyword, files in top3:
            lines.append(f"{keyword}: {len(files)} 张")
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------------
    # 菜单（所有人）
    # ------------------------------------------------------------------
    async def _handle_menu(self, event: AstrMessageEvent, message_str: str):
        lines = [
            "表情包管理菜单",
            "【所有人可用】",
            "添加{关键词} - 回复图片批量入库",
            "批量添加合并转发{关键词} - 先发送命令，90秒内本人发送的合并转发自动批量入库",
            "来只{关键词} - 随机发送一张该关键词的图片",
            "表情包管理菜单 - 显示本菜单",
            "【仅管理员】",
            "屏蔽{关键词} - 屏蔽关键词（禁止添加、发送、查看）",
            "屏蔽列表 - 查看屏蔽关键词",
            "解除屏蔽{关键词} - 解除屏蔽",
            "列表{关键词}{页码} - 分页查看该关键词的图片（每页10张）",
            "删图 - 回复图片，按 MD5 自动定位并删除该图（可不带关键词）",
            "删图{关键词} - 回复图片，仅在该关键词下按 MD5 删除",
            "删图{关键词}{序号} - 按序号删除单张图片",
            "删除{关键词} - 删除该关键词全部图片（60秒内二次确认）",
            "统计 - 查看关键词数、图片总数、图片数Top 3",
            "备份/恢复/管理员设置 - 请在 AstrBot WebUI 插件页面的“表情包管理”页操作",
            "开源：https://github.com/td1336065617/laizhiqingchu-bot",
        ]
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------------
    # G. WebUI 备份 / 恢复（AstrBot 管理面板后台）
    # ------------------------------------------------------------------
    async def _web_config_get(self):
        return json_response(
            {
                "status": "success",
                "data": {
                    "admin_users": [
                        str(a) for a in (self.config.get("admin_users", []) or [])
                    ],
                    "max_storage_mb": self._max_storage_mb(),
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

    def _apply_config_payload(self, payload: dict) -> dict:
        """校验并保存后台配置，返回实际更新的字段。"""
        updated: dict = {}
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
        try:
            self.config.save_config()
        except Exception as exc:
            logger.error("保存插件配置失败: %s", exc)
            raise ValueError(f"保存配置失败：{exc}") from exc
        return updated

    async def _web_backup(self):
        """生成备份压缩包：data/stickers -> zip（含 index/blocked/manifest 校验清单）。"""
        try:
            info = self._backup_to_zip()
        except Exception as exc:
            logger.error("备份失败: %s", exc, exc_info=True)
            return error_response(f"备份失败：{exc}")
        return json_response(
            {
                "status": "success",
                "data": {
                    "filename": info["filename"],
                    "keywords": info["keywords"],
                    "images": info["images"],
                    "size": info["size"],
                    "message": (
                        f"备份完成：{info['keywords']} 个关键词，"
                        f"{info['images']} 张图片，{info['size'] / 1024:.1f} KB"
                    ),
                },
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

    def _backup_to_zip(self) -> dict:
        """把当前全部表情包数据打包为 zip（含 manifest 校验清单）。"""
        self.backup_root.mkdir(parents=True, exist_ok=True)
        filename = (
            f"sticker_backup_{time.strftime('%Y%m%d_%H%M%S')}_{random.randint(1000, 9999)}.zip"
        )
        target = self.backup_root / filename
        files_meta = []
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                "index.json",
                json.dumps(self.index, ensure_ascii=False, indent=2),
            )
            zf.writestr(
                "blocked_keywords.json",
                json.dumps(sorted(self.blocked_keywords), ensure_ascii=False, indent=2),
            )
            for keyword, names in self.index.items():
                folder = self.data_dir / keyword
                for fname in names:
                    fpath = folder / fname
                    if not fpath.is_file():
                        continue
                    arc = f"{keyword}/{fname}"
                    zf.write(fpath, arc)
                    files_meta.append({"path": arc, "md5": self._md5(fpath)})
            zf.writestr(
                "manifest.json",
                json.dumps(
                    {
                        "version": 1,
                        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "keywords": len(self.index),
                        "images": len(files_meta),
                        "files": files_meta,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        return {
            "filename": filename,
            "size": target.stat().st_size,
            "keywords": len(self.index),
            "images": len(files_meta),
        }

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
            if manifest.get("version") != 1:
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
            # 包内文件必须是索引引用的图片或固定元数据文件，且路径安全
            for member in members:
                if member in ("index.json", "manifest.json", "blocked_keywords.json"):
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

        return {
            "archive_keywords": len(archive_index),
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
    async def _handle_block(self, event: AstrMessageEvent, message_str: str):
        if not self._is_admin(event):
            yield event.plain_result("此指令仅限管理员")
            return

        keyword = message_str[2:].strip()
        if not keyword:
            yield event.plain_result("用法：屏蔽{关键词}")
            return
        if not self._is_valid_keyword(keyword):
            yield event.plain_result("关键词不合法")
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
            return f"group:{group}:{sender}"
        return f"user:{sender}"

    @staticmethod
    def _is_forward_event(event: AstrMessageEvent) -> bool:
        """判断是否为合并转发（聊天记录，message_type=102）消息。"""
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if raw is None:
            return False
        try:
            return (
                int(getattr(raw, "message_type", None) or 0)
                == QQ_FORWARD_MESSAGE_TYPE
            )
        except (TypeError, ValueError):
            return False

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
        urls: List[str] = []
        seen: set = set()
        saw_forward_body = False

        def add_url(url: str) -> None:
            url = (url or "").strip()
            if url and url not in seen:
                seen.add(url)
                urls.append(url)

        if raw is not None:
            # 1) 嵌套消息元素（文档结构，部分场景会带真实附件）
            elements = getattr(raw, "msg_elements", None)
            if isinstance(elements, list) and elements:
                saw_forward_body = True
                for url in self._collect_forward_urls(elements):
                    add_url(url)
            # 2) 顶层附件（部分场景 102 直接带附件）
            top_attachments = getattr(raw, "attachments", None) or []
            if top_attachments:
                saw_forward_body = True
                for url in self._collect_forward_urls(
                    [{"attachments": top_attachments}]
                ):
                    add_url(url)
            # 3) 摘要文本：QQ 官方实际推送 102 时 msg_elements 为空，
            #    图片 URL 内嵌在 content 的渲染摘要里（[附件1] 类型:图片 ... URL:...）
            summary_text = getattr(raw, "content", None) or ""
            if isinstance(summary_text, str) and summary_text.strip():
                saw_forward_body = True
                for url in self._extract_forward_summary_urls(summary_text):
                    add_url(url)
        else:
            summary_text = event.message_str or ""
            if summary_text.strip():
                saw_forward_body = True
                for url in self._extract_forward_summary_urls(summary_text):
                    add_url(url)

        if not urls:
            logger.warning(
                "StickerPlugin 102 解析失败，原始结构: %s",
                self._describe_raw_structure(raw),
            )
            if saw_forward_body:
                yield event.plain_result("该合并转发中没有解析到图片")
            else:
                yield event.plain_result("未从合并转发中解析到内容，请重试")
            return

        logger.info("StickerPlugin 102 解析到 %d 张图片", len(urls))
        images = [Image.fromURL(url) for url in urls]
        keyword = state["keyword"]
        if keyword in self.blocked_keywords:
            yield event.plain_result(f"关键词 {keyword} 已被屏蔽，无法添加")
            return
        added, skipped, failed = await self._add_images(keyword, images)
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
        """递归提取合并转发元素中的图片 URL，返回去重后的 URL 列表。

        兼容多种结构：attachments 附件、嵌套 msg_elements/elements/messages/nodes、
        JSON 字符串 content，以及 QQ 官方 102 的渲染摘要文本。
        """
        urls: List[str] = []
        seen: set = set()

        def get_attr(element, key):
            if isinstance(element, dict):
                return element.get(key)
            return getattr(element, key, None)

        def add_url(url: str) -> None:
            url = (url or "").strip()
            if url and url not in seen:
                seen.add(url)
                urls.append(url)

        def add_attachments(attachments) -> None:
            if not isinstance(attachments, list):
                return
            for att in attachments:
                url = str(get_attr(att, "url") or "").strip()
                if not url:
                    continue
                ctype = str(get_attr(att, "content_type") or "").lower()
                filename = str(
                    get_attr(att, "filename") or get_attr(att, "name") or ""
                )
                ext = Path(filename).suffix.lower()
                if (
                    ctype.startswith("image")
                    or ext in ALLOWED_EXTS
                    or "multimedia.nt.qq.com.cn" in url
                ):
                    add_url(url)

        def handle_element(element) -> None:
            if element is None:
                return
            if isinstance(element, str):
                for url in cls._extract_forward_summary_urls(element):
                    add_url(url)
                return
            add_attachments(get_attr(element, "attachments"))
            content = get_attr(element, "content")
            if isinstance(content, str):
                stripped = content.strip()
                if stripped.startswith(("{", "[")):
                    try:
                        parsed = json.loads(stripped)
                    except (json.JSONDecodeError, TypeError):
                        parsed = None
                    if isinstance(parsed, list):
                        for item in parsed:
                            handle_element(item)
                    elif isinstance(parsed, dict):
                        handle_element(parsed)
                    else:
                        # 形如 JSON 但不是 JSON（如 “[群聊的聊天记录]...” 摘要文本），
                        # 回落到按摘要行提取图片 URL
                        for url in cls._extract_forward_summary_urls(content):
                            add_url(url)
                else:
                    # 非 JSON 文本：按 102 渲染摘要提取图片 URL
                    for url in cls._extract_forward_summary_urls(content):
                        add_url(url)
            for key in FORWARD_NESTED_KEYS:
                nested = get_attr(element, key)
                if isinstance(nested, list):
                    for item in nested:
                        handle_element(item)

        if isinstance(elements, list):
            for element in elements:
                handle_element(element)
        else:
            handle_element(elements)
        return urls

    @classmethod
    def _extract_forward_summary_urls(cls, content: str) -> List[str]:
        """从 QQ 官方 102 摘要文本中提取图片 URL。

        真实推送中 msg_elements 为空，图片信息以
        “[附件1] 类型:图片 文件名:xxx 尺寸:... URL:https://...” 的文本行内嵌在 content 中。
        同时兼容 “[图片] 图片1:xxx URL:...” 的变体。
        """
        if not content:
            return []
        urls: List[str] = []
        seen: set = set()

        def add_url(url: str) -> None:
            url = (url or "").strip()
            if url and url not in seen:
                seen.add(url)
                urls.append(url)

        for line in content.splitlines():
            if "URL:" not in line or not any(
                marker in line for marker in FORWARD_SUMMARY_MARKERS
            ):
                continue
            type_match = re.search(r"类型:\s*([^\s]+)", line)
            if type_match and type_match.group(1) != "图片":
                # 附件行带类型且不是图片（如 类型:文件/视频/语音），跳过
                continue
            for m in FORWARD_URL_RE.finditer(line):
                add_url(m.group(1))
        if not urls:
            # 兜底：摘要中直接出现的 QQ 附件下载地址
            for m in re.finditer(
                r"https://multimedia\.nt\.qq\.com\.cn/download\?\S+", content
            ):
                add_url(m.group(0))
        return urls

    @staticmethod
    def _describe_raw_structure(raw) -> str:
        """简要描述 102 原始消息结构（只打印键名与短文本，避免刷爆日志）。"""
        if raw is None:
            return "raw=None"
        if isinstance(raw, dict):
            return "keys=" + ",".join(str(k) for k in raw.keys())
        attrs = [
            name
            for name in ("content", "message_type", "attachments", "msg_elements")
            if hasattr(raw, name)
        ]
        text = getattr(raw, "content", None)
        raw_data = getattr(raw, "raw_data", None)
        desc = "attrs=" + ",".join(attrs)
        if isinstance(raw_data, dict):
            desc += "; raw_data_keys=" + ",".join(str(k) for k in raw_data.keys())
        if isinstance(text, str) and text:
            desc += "; content_head=" + repr(text[:300])
        return desc

    @staticmethod
    def _is_valid_keyword(keyword: str) -> bool:
        if not keyword:
            return False
        if keyword in RESERVED_KEYWORDS:
            return False
        if re.search(r"\s", keyword):
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
        sender_id = event.get_sender_id()
        is_admin = sender_id in admins
        if not is_admin:
            logger.warning(
                "StickerPlugin 管理员校验未通过: sender_id=%s admin_users=%s",
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
    def _md5(path: Path) -> str:
        digest = hashlib.md5()
        with open(path, "rb") as fp:
            for chunk in iter(lambda: fp.read(8192), b""):
                digest.update(chunk)
        return digest.hexdigest()

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
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.blocked_path.write_text(
                json.dumps(sorted(self.blocked_keywords), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.error("保存屏蔽列表失败: %s", exc)

    def _save_index(self):
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.index_path.write_text(
                json.dumps(self.index, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.error("保存索引失败: %s", exc)

    def _check_storage(self):
        try:
            limit_mb = int(
                self.config.get("max_storage_mb", DEFAULT_MAX_STORAGE_MB)
                or DEFAULT_MAX_STORAGE_MB
            )
        except (TypeError, ValueError):
            limit_mb = DEFAULT_MAX_STORAGE_MB
        limit = limit_mb * 1024 * 1024
        total = 0
        if self.data_dir.exists():
            for f in self.data_dir.rglob("*"):
                if f.is_file():
                    try:
                        total += f.stat().st_size
                    except OSError:
                        continue
        if total > limit:
            logger.warning(
                "表情包存储已超过 %dMB（当前 %.1fMB），请清理",
                limit_mb,
                total / 1024 / 1024,
            )
