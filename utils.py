"""插件内共用的小工具：JM 号解析、文件名清洗、消息格式化。

这个模块刻意不依赖 astrbot / jmcomic，方便单独做单元测试。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence

# JM 号：支持 "123456"、"JM123456"、"jm-123456" 以及常见详情页 URL
_ID_FULL = re.compile(r"^(?:jm)?[\s_\-]?(\d{1,9})$", re.IGNORECASE)
_ID_URL = re.compile(
    r"(?:/album/|/photo/|/photos/|[?&]id=)(\d{1,9})",
    re.IGNORECASE,
)
_ID_LOOSE = re.compile(r"(?:jm)?[\s_\-]?(\d{2,9})", re.IGNORECASE)

_INVALID_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')
_WHITESPACE = re.compile(r"\s+")
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def extract_jm_id(text: str | None) -> str | None:
    """从用户输入里提取 JM 号。

    >>> extract_jm_id("123456")
    '123456'
    >>> extract_jm_id("JM-123456")
    '123456'
    >>> extract_jm_id("https://18comic.vip/album/123456/")
    '123456'
    >>> extract_jm_id("随便聊聊") is None
    True
    """
    if not text:
        return None

    value = str(text).strip()
    if not value:
        return None

    match = _ID_URL.search(value)
    if match:
        return match.group(1)

    match = _ID_FULL.fullmatch(value)
    if match:
        return match.group(1)

    # 允许 "/jm 123456 谢谢" 这类尾巴：只取开头的数字
    match = _ID_LOOSE.match(value)
    if match:
        return match.group(1)

    return None


def sanitize_filename(name: str, max_len: int = 64) -> str:
    """清洗成跨平台可用的文件名（不含扩展名）。"""
    value = unicodedata.normalize("NFKC", str(name or "")).strip()
    value = _INVALID_FILENAME_CHARS.sub("_", value)
    value = _WHITESPACE.sub(" ", value).strip(" ._")
    if not value:
        value = "album"

    # Linux 单个文件名上限通常是 255 字节，中文按 3 字节估算，留出余地
    if len(value) > max_len:
        value = value[:max_len].rstrip(" ._") or "album"
    return value


def truncate(text: str, limit: int, suffix: str = "…") -> str:
    value = str(text or "")
    if limit <= 0 or len(value) <= limit:
        return value
    return value[:limit].rstrip() + suffix


def human_size(num_bytes: float) -> str:
    size = float(num_bytes or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _clean_inline(text: object) -> str:
    """去掉控制字符并把换行压成空格，适合标题/作者/标签。"""
    value = _CONTROL_CHARS.sub("", str(text or ""))
    return _WHITESPACE.sub(" ", value).strip()


def _as_str_list(value: Iterable[object] | None) -> list[str]:
    if not value:
        return []
    return [cleaned for item in value if (cleaned := _clean_inline(item))]


def format_album_text(
    album: object,
    *,
    max_title: int = 120,
    max_desc: int = 300,
    show_warning: bool = True,
) -> str:
    """把 JmAlbumDetail 格式化成聊天消息。

    使用 getattr 而不是类型标注，方便用假对象做测试。
    """
    album_id = getattr(album, "album_id", "")
    title = truncate(_clean_inline(getattr(album, "name", "") or "无标题"), max_title)

    authors = _as_str_list(getattr(album, "authors", None))
    author = (
        "、".join(authors)
        if authors
        else _clean_inline(getattr(album, "author", "")) or "未知"
    )

    tags = _as_str_list(getattr(album, "tags", None))
    tag_text = "、".join(tags[:12]) if tags else "无"
    if len(tags) > 12:
        tag_text += f" 等 {len(tags)} 个"

    page_count = getattr(album, "page_count", 0) or 0
    try:
        chapter_count = len(album)  # type: ignore[arg-type]
    except TypeError:
        chapter_count = 1

    description = _CONTROL_CHARS.sub("", str(getattr(album, "description", "") or ""))
    description = _WHITESPACE.sub(" ", description).strip()
    description = truncate(description, max_desc) if description else "（暂无简介）"

    lines = [
        f"📚 JM{album_id} · {title}",
        f"👤 作者：{author}",
        f"🏷️ 标签：{tag_text}",
        f"📄 页数：{page_count} 页 / {chapter_count} 章",
        "📝 简介：",
        description,
    ]
    if show_warning:
        lines.append("")
        lines.append("⚠️ 内容可能包含成人向内容，请确认已满 18 岁，并遵守当地法律法规。")

    return "\n".join(lines)


def find_blocked_tag(
    album: object,
    blocked_tags: Sequence[str] | None,
) -> str | None:
    """返回命中的屏蔽标签；未命中返回 None。"""
    if not blocked_tags:
        return None

    tags = _as_str_list(getattr(album, "tags", None))
    haystack = " ".join([str(getattr(album, "name", "") or ""), *tags]).casefold()

    for raw in blocked_tags:
        tag = str(raw).strip()
        if tag and tag.casefold() in haystack:
            return tag
    return None
