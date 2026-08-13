"""AstrBot QQ 群表情包管理插件（StickerPlugin）。

指令（精确前缀触发，不匹配的消息完全忽略）：
- 添加{关键词}        回复一条含图片的消息后触发，支持多图批量入库
- 来只{关键词}        随机发送一张该关键词下的图片
- 列表{关键词}{页码}   仅管理员，分页查看（每页 10 张）
- 删图{关键词}{序号}   仅管理员，删除指定序号图片
- 删除{关键词}        仅管理员，二次确认（60 秒）后删除整个关键词目录
- 统计              仅管理员，查看关键词数、图片总数、Top 3
- 菜单              所有人，查看指令与权限说明

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
from pathlib import Path
from typing import List, Optional, Tuple

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Reply
from astrbot.api.star import Context, Star

# 与指令名重名的关键词一律不合法
RESERVED_KEYWORDS = {"添加", "来只", "列表", "删图", "删除", "统计", "菜单"}
# 二次删除确认的有效期（秒）
CONFIRM_TTL = 60
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


class StickerPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.data_dir = _resolve_data_dir()
        self.index_path = self.data_dir / "index.json"
        # 索引结构：{"关键词": ["文件名1.png", ...]}
        self.index: dict = {}
        # 批量删除二次确认：{user_id: {"keyword": str, "time": float}}
        self.pending_delete: dict = {}
        self._load_index()
        self._check_storage()
        logger.info(
            "StickerPlugin 管理员列表: %s",
            [str(a) for a in (self.config.get("admin_users", []) or [])],
        )

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
        if not message_str:
            return

        try:
            if message_str.startswith("添加"):
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
            elif message_str.startswith("菜单"):
                async for result in self._handle_menu(event, message_str):
                    yield result
            # 其余消息：完全忽略
        except Exception as exc:
            logger.error("StickerPlugin 处理消息异常: %s", exc, exc_info=True)
            yield event.plain_result("表情包插件处理出错，请查看日志")

    # ------------------------------------------------------------------
    # A. 添加（回复消息中包含图片才触发，否则完全无视）
    # ------------------------------------------------------------------
    async def _handle_add(self, event: AstrMessageEvent, message_str: str):
        images = self._extract_reply_images(event)
        logger.info(
            "StickerPlugin 添加: 提取到引用图片 %s",
            None if images is None else len(images),
        )
        keyword = message_str[2:].strip()
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
            if keyword:
                yield event.plain_result(
                    "添加失败：未获取到图片。请把图片和“添加{关键词}”放在同一条消息发送"
                    "（私聊引用消息平台可能不下发被引用内容）"
                )
            return
        if not keyword:
            yield event.plain_result("用法：添加{关键词}（请回复包含图片的消息）")
            return
        if not self._is_valid_keyword(keyword):
            yield event.plain_result("关键词不合法")
            return

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
        if failed:
            yield event.plain_result(
                f"成功添加 {added} 张图片（跳过 {skipped} 张重复，{failed} 张下载失败）"
            )
        else:
            yield event.plain_result(f"成功添加 {added} 张图片（跳过 {skipped} 张重复）")

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

        rest = message_str[2:].strip()
        match = re.search(r"(\d+)\s*$", rest)
        if not match:
            yield event.plain_result("用法：删图{关键词}{序号}")
            return
        keyword = rest[: match.start()].strip()
        index = int(match.group(1))
        if not keyword:
            yield event.plain_result("用法：删图{关键词}{序号}")
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
            "添加{关键词} - 回复图片，或与图片同一条消息发送，即可入库",
            "来只{关键词} - 随机发送一张该关键词的图片",
            "菜单 - 显示本菜单",
            "【仅管理员】",
            "列表{关键词}{页码} - 分页查看该关键词的图片（每页10张）",
            "删图{关键词}{序号} - 按序号删除单张图片",
            "删除{关键词} - 删除该关键词全部图片（60秒内二次确认）",
            "统计 - 查看关键词数、图片总数、图片数Top 3",
        ]
        yield event.plain_result("\n".join(lines))

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
