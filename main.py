"""AstrBot 禁漫漫画插件。

指令：
    /jm <ID>        查看漫画封面、作者、简介
    /jm 下载 <ID>   下载整本并合并成 PDF 发送
    /jm 推荐        从最新分类里随机推荐一部
    /jm 帮助        查看帮助
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import AsyncGenerator

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.message_components import File, Image, Plain
from astrbot.api.star import Context, Star, StarTools

from .jm_service import (
    AlbumView,
    DownloadResultInfo,
    JMBlockedError,
    JMDownloadError,
    JMNetworkError,
    JMNotFoundError,
    JMService,
    JMServiceError,
    JMTimeoutError,
)
from .utils import extract_jm_id, format_album_text, human_size, truncate

PLUGIN_NAME = "astrbot_plugin_jmcomic"

HELP_TEXT = """📖 JM 漫画助手

/jm <ID>        查看漫画详情（封面 / 作者 / 简介）
/jm 下载 <ID>   下载整本并合并成一个 PDF 发送
/jm 推荐        随机推荐一部漫画
/jm 帮助        显示本帮助

示例：
/jm 123456
/jm 下载 123456
/jm 推荐"""


class JMComicPlugin(Star):
    """JM 漫画查询、下载与随机推荐。

    下载会把所有章节按顺序合并成一个 PDF，通过 OneBot v11 的文件消息发送。
    """

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config: AstrBotConfig | dict = config or {}
        self.data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.service = JMService(self.data_dir, self.config)
        self._cooldown: dict[str, float] = {}

    async def initialize(self) -> None:
        """插件加载完成后创建目录、清理过期缓存。"""
        self.service.ensure_dirs()
        try:
            await asyncio.to_thread(self.service.cleanup_old_files)
        except Exception:  # noqa: BLE001 - 清理失败不应影响插件加载
            logger.exception("清理 JM 插件缓存失败")
        logger.info("JM 漫画插件已加载，数据目录：%s", self.data_dir)

    async def terminate(self) -> None:
        """插件卸载/停用时调用。"""
        self.service.close()
        logger.info("JM 漫画插件已卸载")

    # ------------------------------------------------------------------
    # 指令入口
    # ------------------------------------------------------------------
    @filter.command("jm", alias={"jmcomic", "JM", "禁漫"})
    async def jm(self, event: AstrMessageEvent):
        """JM 漫画：/jm <ID> 查详情，/jm 下载 <ID> 发 PDF，/jm 推荐 随机推荐。"""
        denied = self._access_denied(event)
        if denied:
            yield event.plain_result(denied)
            return

        args = self._extract_args(event.message_str)
        action, payload = self._parse_command(args)

        if action == "help":
            yield event.plain_result(HELP_TEXT)
            return

        if action == "recommend":
            try:
                yield await self._recommend_result(event)
            except Exception as exc:  # noqa: BLE001 - 统一转成用户可读错误
                yield event.plain_result(self._error_text(exc))
            return

        album_id = extract_jm_id(payload)
        if not album_id:
            yield event.plain_result(
                "❌ 没有识别到 JM 号。\n用法：/jm <ID> 或 /jm 下载 <ID>\n" + HELP_TEXT
            )
            return

        if action == "download":
            async for result in self._download_results(event, album_id):
                yield result
            return

        try:
            yield await self._detail_result_for(event, album_id)
        except Exception as exc:  # noqa: BLE001 - 统一转成用户可读错误
            yield event.plain_result(self._error_text(exc))

    # ------------------------------------------------------------------
    # 各子指令
    # ------------------------------------------------------------------
    async def _detail_result_for(
        self,
        event: AstrMessageEvent,
        album_id: str,
    ) -> MessageEventResult:
        view = await self.service.get_album_view(album_id)
        return self._album_result(event, view)

    def _album_result(
        self, event: AstrMessageEvent, view: AlbumView
    ) -> MessageEventResult:
        text = format_album_text(
            view.album,
            max_title=self._config_int("title_max_len", 120, 20, 500),
            max_desc=self._config_int("description_max_len", 300, 0, 5000),
            show_warning=self._config_bool("nsfw_warning", True),
        )

        if not view.cover:
            return event.plain_result(text)

        return event.chain_result(
            [
                Plain(text),
                Image.fromFileSystem(str(view.cover)),
            ]
        )

    async def _recommend_result(self, event: AstrMessageEvent) -> MessageEventResult:
        view = await self.service.recommend()
        return self._album_result(event, view)

    async def _download_results(
        self,
        event: AstrMessageEvent,
        album_id: str,
    ) -> AsyncGenerator[MessageEventResult, None]:
        if self._config_bool("download_admin_only", False) and not self._is_admin(
            event
        ):
            yield event.plain_result("❌ 当前配置仅允许管理员下载漫画。")
            return

        remaining = self._check_cooldown(event)
        if remaining is not None:
            yield event.plain_result(f"⏳ 下载太频繁了，请 {remaining} 秒后再试。")
            return

        lock = self.service.get_album_lock(album_id)
        if lock.locked():
            yield event.plain_result(f"⏳ JM{album_id} 正在下载中，已加入等待队列…")

        yield event.plain_result(
            f"⏳ 开始处理 JM{album_id}：下载图片 → 合并 PDF，请稍候…"
        )

        result: DownloadResultInfo | None = None
        try:
            async with lock:
                result = await self.service.download_and_pdf(album_id)
        except Exception as exc:  # noqa: BLE001 - 下载失败需要反馈给用户
            yield event.plain_result(self._error_text(exc))
            return
        finally:
            self.service.release_album_lock(album_id)

        if result is None:
            yield event.plain_result("❌ 下载失败：没有拿到结果，请查看 AstrBot 日志。")
            return

        caption = self._download_caption(result)
        total = len(result.pdf_files)
        for index, pdf in enumerate(result.pdf_files, start=1):
            part_caption = caption
            if total > 1:
                part_caption += f"\n（第 {index}/{total} 部分）"
            yield event.chain_result(
                [
                    Plain(part_caption),
                    File(name=pdf.name, file=str(pdf)),
                ]
            )

    def _download_caption(self, result: DownloadResultInfo) -> str:
        album = result.album
        title = truncate(str(album.name or "无标题"), 80)
        prefix = "⚡ 命中缓存" if result.cached else "✅ 下载完成"
        lines = [
            f"{prefix} JM{album.album_id}",
            f"📖 {title}",
            f"🖼️ {result.image_count} 页 · 📦 {human_size(result.total_size)}",
        ]
        if result.failed_images:
            lines.append(f"⚠️ 有 {result.failed_images} 张图片下载失败，已跳过。")
        if len(result.pdf_files) > 1:
            lines.append(f"📚 已拆分 {len(result.pdf_files)} 个分卷，逐条发送。")
        if self._config_bool("nsfw_warning", True):
            lines.append("⚠️ 成人向内容，请勿在未成年人可访问的场景传播。")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 解析与权限
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_args(message_str: str | None) -> str:
        value = (message_str or "").strip()
        value = re.sub(r"^[\s/!#！。]+", "", value)
        match = re.match(
            r"^(?:jmcomic|jm|禁漫)(?:\s+|$)(.*)$",
            value,
            re.IGNORECASE | re.DOTALL,
        )
        if match:
            return match.group(1).strip()
        return value

    @staticmethod
    def _parse_command(args: str) -> tuple[str, str]:
        value = (args or "").strip()
        if not value:
            return "help", ""

        if re.fullmatch(r"(帮助|help|\?|h|说明)", value, re.IGNORECASE):
            return "help", ""

        if re.fullmatch(r"(推荐|随机|random|rand|recommend|r)", value, re.IGNORECASE):
            return "recommend", ""

        match = re.match(
            r"^(下载|download|dl)\s*(.*)$", value, re.IGNORECASE | re.DOTALL
        )
        if match:
            return "download", match.group(2).strip()

        return "detail", value

    def _access_denied(self, event: AstrMessageEvent) -> str | None:
        group_id = str(event.get_group_id() or "").strip()
        if not group_id:
            return None

        if not self._config_bool("group_enabled", True):
            return "❌ 本插件已在群聊中禁用。"

        blocked = {str(item) for item in self._config_list("blocked_groups")}
        if group_id in blocked:
            return "❌ 本群已被禁用。"

        allowed = {str(item) for item in self._config_list("allowed_groups")}
        if allowed and group_id not in allowed:
            return "❌ 本群未授权使用 JM 漫画功能。"

        return None

    @staticmethod
    def _is_admin(event: AstrMessageEvent) -> bool:
        try:
            return bool(event.is_admin())
        except Exception:  # noqa: BLE001 - 某些适配器没有 role 信息
            return False

    def _check_cooldown(self, event: AstrMessageEvent) -> int | None:
        cooldown = self._config_int("download_cooldown", 30, 0, 86400)
        if cooldown <= 0 or self._is_admin(event):
            return None

        user_id = str(event.get_sender_id() or event.get_session_id() or "")
        if not user_id:
            return None

        now = time.monotonic()
        last = self._cooldown.get(user_id, 0.0)
        if now - last < cooldown:
            return max(1, int(cooldown - (now - last)) + 1)

        self._cooldown[user_id] = now
        if len(self._cooldown) > 2000:
            cutoff = now - 86400
            self._cooldown = {
                key: value for key, value in self._cooldown.items() if value >= cutoff
            }
        return None

    # ------------------------------------------------------------------
    # 配置与错误
    # ------------------------------------------------------------------
    def _config_int(self, key: str, default: int, minimum: int, maximum: int) -> int:
        value = (
            self.config.get(key, default) if hasattr(self.config, "get") else default
        )
        try:
            number = int(value)
        except (TypeError, ValueError):
            number = default
        return max(minimum, min(maximum, number))

    def _config_bool(self, key: str, default: bool) -> bool:
        value = (
            self.config.get(key, default) if hasattr(self.config, "get") else default
        )
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on", "是"}
        return bool(value)

    def _config_list(self, key: str) -> list[str]:
        value = self.config.get(key, []) if hasattr(self.config, "get") else []
        if isinstance(value, str):
            return [item.strip() for item in re.split(r"[,，]", value) if item.strip()]
        if isinstance(value, (list, tuple, set)):
            return [str(item).strip() for item in value if str(item).strip()]
        return []

    @staticmethod
    def _error_text(exc: Exception) -> str:
        if isinstance(exc, JMBlockedError):
            return f"🚫 该漫画命中屏蔽标签「{exc.tag}」，已拒绝处理。"
        if isinstance(exc, JMNotFoundError):
            return f"❌ {exc}"
        if isinstance(exc, JMTimeoutError):
            return f"⏰ {exc}"
        if isinstance(exc, JMNetworkError):
            return f"🌐 {exc}"
        if isinstance(exc, JMDownloadError):
            return f"❌ {exc}"
        if isinstance(exc, JMServiceError):
            return f"❌ {exc}"
        logger.exception("JM 指令处理失败", exc_info=exc)
        return "❌ 处理失败，请稍后重试或查看 AstrBot 日志。"
