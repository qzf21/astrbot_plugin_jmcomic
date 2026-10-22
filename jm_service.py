"""JM 业务层：调用 jmcomic 库，供 AstrBot 指令使用。

这里只做四件事：
1. 构造 JmOption（代理、域名、并发、日志）；
2. 获取本子详情 + 封面；
3. 从“最新”分类里随机挑一本；
4. 下载本子并把图片合并成 PDF。
"""

from __future__ import annotations

import asyncio
import random
import shutil
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from astrbot.api import logger
from jmcomic.api import download_album_async
from jmcomic.jm_config import JmMagicConstants, JmModuleConfig
from jmcomic.jm_entity import JmAlbumDetail
from jmcomic.jm_exception import (
    JmcomicException,
    MissingAlbumPhotoException,
    RequestRetryAllFailException,
)
from jmcomic.jm_option import JmOption
from jmcomic.jm_toolkit import JmcomicText

from .pdf_builder import PdfBuildError, build_pdfs, cleanup_empty_dir
from .utils import find_blocked_tag, human_size, sanitize_filename


class JMServiceError(Exception):
    """业务异常基类。"""


class JMNotFoundError(JMServiceError):
    """本子不存在。"""


class JMTimeoutError(JMServiceError):
    """请求或下载超时。"""


class JMNetworkError(JMServiceError):
    """JM 站点/镜像请求失败。"""


class JMBlockedError(JMServiceError):
    """命中屏蔽标签。"""

    def __init__(self, tag: str, album: JmAlbumDetail | None = None):
        self.tag = tag
        self.album = album
        super().__init__(f"命中屏蔽标签：{tag}")


class JMDownloadError(JMServiceError):
    """下载或 PDF 生成失败。"""


@dataclass(slots=True)
class AlbumView:
    album: JmAlbumDetail
    cover: Path | None = None


@dataclass(slots=True)
class DownloadResultInfo:
    album: JmAlbumDetail
    pdf_files: list[Path]
    image_count: int = 0
    failed_images: int = 0
    duration: float = 0.0
    cached: bool = False

    @property
    def total_size(self) -> int:
        total = 0
        for path in self.pdf_files:
            try:
                total += path.stat().st_size
            except OSError:
                continue
        return total


@dataclass(slots=True)
class _PageCacheItem:
    created_at: float
    page: Any


class JMService:
    """一个插件实例对应一个 JMService。"""

    def __init__(self, data_dir: str | Path, config: dict[str, Any] | None = None):
        self.data_dir = Path(data_dir)
        self.config: dict[str, Any] = dict(config or {})

        self.downloads_dir = self.data_dir / "downloads"
        self.pdf_dir = self.data_dir / "pdf"
        self.covers_dir = self.data_dir / "covers"
        self.temp_dir = self.data_dir / "tmp"

        self.max_concurrent = self._int("max_concurrent_downloads", 2, 1, 10)
        self.download_timeout = self._int("download_timeout", 600, 30, 3600)
        self.pdf_timeout = self._int("pdf_timeout", 300, 30, 3600)
        self.detail_timeout = self._int("detail_timeout", 45, 10, 180)
        self.image_concurrency = self._int("image_concurrency", 8, 1, 30)
        self.http_timeout = self._int("http_timeout", 30, 5, 120)
        self.retry_times = self._int("retry_times", 3, 0, 10)
        self.max_album_pages = self._int("max_album_pages", 1000, 0, 100000)
        self.pdf_pages_per_file = self._int("pdf_pages_per_file", 0, 0, 10000)
        self.pdf_split_size_mb = self._int("pdf_split_size_mb", 0, 0, 2048)
        self.pdf_jpeg_quality = self._int("pdf_jpeg_quality", 90, 60, 100)
        self.cache_days = self._int("cache_days", 7, 0, 3650)
        self.max_random_page = self._int("max_random_page", 20, 1, 500)
        self.recommend_attempts = self._int("recommend_attempts", 8, 1, 30)

        self.cleanup_images = self._bool("cleanup_images", True)
        self.cache_pdf = self._bool("cache_pdf", True)
        self.cover_enabled = self._bool("cover_enabled", True)
        self.enforce_blocked_tags = self._bool("enforce_blocked_tags", True)
        self.debug_log = self._bool("debug_log", False)

        self.blocked_tags = self._list("blocked_tags")
        self.domain_list = self._list("domain_list")
        self.proxy = self._str("proxy")

        self._download_sem = asyncio.Semaphore(self.max_concurrent)
        self._album_locks: dict[str, asyncio.Lock] = {}
        self._page_cache: dict[int, _PageCacheItem] = {}
        self._closed = False

    # ------------------------------------------------------------------
    # 配置读取
    # ------------------------------------------------------------------
    def _raw(self, key: str, default: Any) -> Any:
        value = self.config.get(key, default)
        return default if value is None else value

    def _int(self, key: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(self._raw(key, default))
        except (TypeError, ValueError):
            value = default
        return max(minimum, min(maximum, value))

    def _bool(self, key: str, default: bool) -> bool:
        value = self._raw(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on", "是"}
        return bool(value)

    def _str(self, key: str, default: str = "") -> str:
        value = self._raw(key, default)
        return str(value).strip() if value is not None else default

    def _list(self, key: str) -> list[str]:
        value = self._raw(key, [])
        if isinstance(value, str):
            return [
                part.strip()
                for part in value.replace("，", ",").split(",")
                if part.strip()
            ]
        if isinstance(value, (list, tuple, set)):
            return [str(item).strip() for item in value if str(item).strip()]
        return []

    # ------------------------------------------------------------------
    # 目录与生命周期
    # ------------------------------------------------------------------
    def ensure_dirs(self) -> None:
        for folder in (
            self.data_dir,
            self.downloads_dir,
            self.pdf_dir,
            self.covers_dir,
            self.temp_dir,
        ):
            folder.mkdir(parents=True, exist_ok=True)

    def cleanup_old_files(self) -> None:
        """清理超过 cache_days 的 PDF、封面和残留下载文件。"""
        if self.cache_days <= 0:
            return

        cutoff = time.time() - self.cache_days * 86400
        for folder in (
            self.pdf_dir,
            self.covers_dir,
            self.downloads_dir,
            self.temp_dir,
        ):
            if not folder.exists():
                continue
            for path in folder.rglob("*"):
                try:
                    if path.is_file() and path.stat().st_mtime < cutoff:
                        path.unlink(missing_ok=True)
                except OSError:
                    continue

            # 自底向上删除空目录
            dirs = [item for item in folder.rglob("*") if item.is_dir()]
            for item in sorted(dirs, key=lambda p: len(p.parts), reverse=True):
                try:
                    item.rmdir()
                except OSError:
                    continue

    def close(self) -> None:
        self._closed = True

    def get_album_lock(self, album_id: str) -> asyncio.Lock:
        lock = self._album_locks.get(album_id)
        if lock is None:
            lock = asyncio.Lock()
            self._album_locks[album_id] = lock
        return lock

    def release_album_lock(self, album_id: str) -> None:
        lock = self._album_locks.get(album_id)
        if lock is not None and not lock.locked():
            self._album_locks.pop(album_id, None)

    # ------------------------------------------------------------------
    # JmOption
    # ------------------------------------------------------------------
    def _build_option(self) -> JmOption:
        proxies = None
        if self.proxy:
            proxies = {"http": self.proxy, "https": self.proxy}

        if self.debug_log:
            JmModuleConfig.FLAG_ENABLE_JM_LOG = True

        option = JmOption.construct(
            {
                "log": self.debug_log,
                "dir_rule": {
                    "rule": "Bd_Aid_Pname",
                    "base_dir": str(self.downloads_dir),
                },
                "download": {
                    "cache": True,
                    "image": {"decode": True, "suffix": None},
                    "threading": {"image": self.image_concurrency},
                },
                "client": {
                    "impl": "api",
                    "retry_times": self.retry_times,
                    "timeout": self.http_timeout,
                    "postman": {"meta_data": {"proxies": proxies}},
                    "domain": self.domain_list,
                },
            }
        )
        return option

    # ------------------------------------------------------------------
    # 详情 / 封面
    # ------------------------------------------------------------------
    async def get_album_view(self, album_id: str) -> AlbumView:
        album, cover = await self._fetch_album(album_id, with_cover=self.cover_enabled)
        self._ensure_not_blocked(album)
        return AlbumView(album=album, cover=cover)

    async def get_album_meta(self, album_id: str) -> JmAlbumDetail:
        album, _ = await self._fetch_album(album_id, with_cover=False)
        self._ensure_not_blocked(album)
        return album

    async def _fetch_album(
        self,
        album_id: str,
        *,
        with_cover: bool,
    ) -> tuple[JmAlbumDetail, Path | None]:
        option = self._build_option()
        try:
            async with option.new_jm_async_client() as client:
                album = await asyncio.wait_for(
                    client.get_album_detail(album_id),
                    timeout=self.detail_timeout,
                )
                cover = (
                    await self._download_cover(client, album.album_id)
                    if with_cover
                    else None
                )
                return album, cover
        except (asyncio.TimeoutError, TimeoutError) as exc:  # noqa: UP041 - 兼容 Python 3.10
            raise JMTimeoutError("获取漫画信息超时，请稍后再试") from exc
        except MissingAlbumPhotoException as exc:
            raise JMNotFoundError(f"JM{album_id} 不存在或已下架") from exc
        except RequestRetryAllFailException as exc:
            raise JMNetworkError("JM 站点请求失败，请检查网络、代理或域名配置") from exc
        except JmcomicException as exc:
            raise JMNetworkError(f"JM 接口返回错误：{exc}") from exc
        except Exception as exc:
            logger.exception("获取 JM%s 详情失败", album_id)
            raise JMNetworkError(f"获取漫画信息失败：{exc}") from exc

    def _ensure_not_blocked(self, album: JmAlbumDetail) -> None:
        if not self.enforce_blocked_tags:
            return
        tag = find_blocked_tag(album, self.blocked_tags)
        if tag:
            logger.info("JM%s 命中屏蔽标签 %s，已拒绝", album.album_id, tag)
            raise JMBlockedError(tag, album)

    async def _download_cover(self, client: Any, album_id: str) -> Path | None:
        if not self.cover_enabled:
            return None

        cache = self.covers_dir / f"{album_id}.jpg"
        if (
            cache.is_file()
            and cache.stat().st_size > 0
            and (
                self.cache_days <= 0
                or time.time() - cache.stat().st_mtime < self.cache_days * 86400
            )
        ):
            try:
                with cache.open("rb") as file:
                    header = file.read(12)
            except OSError:
                header = b""
            if _looks_like_image(header):
                return cache
            cache.unlink(missing_ok=True)

        domains = list(getattr(JmModuleConfig, "DOMAIN_IMAGE_LIST", None) or [])
        random.shuffle(domains)
        # 图片 CDN 域名很多，试 3 个通常足够，避免全部失败时拖太久
        for domain in domains[:3] or [None]:
            try:
                url = JmcomicText.get_album_cover_url(album_id, image_domain=domain)
                response = await asyncio.wait_for(
                    client.get_jm_image(url),
                    timeout=min(20, self.http_timeout),
                )
                content = bytes(getattr(response, "content", b"") or b"")
                if not content or not _looks_like_image(content):
                    continue

                self.covers_dir.mkdir(parents=True, exist_ok=True)
                temp = cache.with_suffix(".part")
                temp.write_bytes(content)
                temp.replace(cache)
                return cache
            except Exception as exc:  # noqa: BLE001 - 单个 CDN 失败不应中断封面流程
                logger.debug("封面下载失败（%s）：%s", domain, exc)
                continue

        logger.warning("JM%s 封面下载失败，将只返回文字信息", album_id)
        return None

    # ------------------------------------------------------------------
    # 随机推荐
    # ------------------------------------------------------------------
    async def recommend(self) -> AlbumView:
        option = self._build_option()
        tried: set[str] = set()

        try:
            async with option.new_jm_async_client() as client:
                for _ in range(self.recommend_attempts):
                    ids = await self._random_album_ids(client)
                    candidates = [item for item in ids if item not in tried]
                    if not candidates:
                        continue
                    album_id = random.choice(candidates)
                    tried.add(album_id)

                    try:
                        album = await asyncio.wait_for(
                            client.get_album_detail(album_id),
                            timeout=self.detail_timeout,
                        )
                    except Exception as exc:  # noqa: BLE001 - 单个候选失败继续抽下一个
                        logger.debug("推荐候选 JM%s 获取失败：%s", album_id, exc)
                        continue

                    if self.enforce_blocked_tags and find_blocked_tag(
                        album, self.blocked_tags
                    ):
                        logger.info("推荐候选 JM%s 命中屏蔽标签，跳过", album_id)
                        continue

                    cover = (
                        await self._download_cover(client, album.album_id)
                        if self.cover_enabled
                        else None
                    )
                    return AlbumView(album=album, cover=cover)
        except JMServiceError:
            raise
        except (asyncio.TimeoutError, TimeoutError) as exc:  # noqa: UP041 - 兼容 Python 3.10
            raise JMTimeoutError("获取推荐超时，请稍后再试") from exc
        except Exception as exc:
            logger.exception("获取 JM 推荐失败")
            raise JMNetworkError(f"获取推荐失败：{exc}") from exc

        raise JMServiceError("暂时没有找到合适的推荐，请稍后再试")

    async def _random_album_ids(self, client: Any) -> list[str]:
        first = await self._category_page(client, 1)
        page_count = max(1, int(getattr(first, "page_count", 1) or 1))
        max_page = max(1, min(page_count, self.max_random_page))
        page = random.randint(1, max_page)

        data = first if page == 1 else await self._category_page(client, page)
        ids = [str(item) for item in data.iter_id()]
        random.shuffle(ids)
        return ids

    async def _category_page(self, client: Any, page: int) -> Any:
        now = time.monotonic()
        cached = self._page_cache.get(page)
        if cached is not None and now - cached.created_at < 600:
            return cached.page

        data = await asyncio.wait_for(
            client.categories_filter(
                page=page,
                time=JmMagicConstants.TIME_ALL,
                category=JmMagicConstants.CATEGORY_ALL,
                order_by=JmMagicConstants.ORDER_BY_LATEST,
            ),
            timeout=self.detail_timeout,
        )
        self._page_cache[page] = _PageCacheItem(created_at=now, page=data)
        return data

    # ------------------------------------------------------------------
    # 下载 + PDF
    # ------------------------------------------------------------------
    async def download_and_pdf(self, album_id: str) -> DownloadResultInfo:
        started = time.monotonic()

        # 先查详情：一是让不存在的本子快速失败，二是做页数/标签预检
        album = await self.get_album_meta(album_id)

        if self.max_album_pages > 0 and album.page_count > self.max_album_pages:
            raise JMDownloadError(
                f"本子共 {album.page_count} 页，超过配置上限 {self.max_album_pages} 页，已取消下载"
            )

        cached = self._find_cached_pdfs(album_id)
        if cached:
            return DownloadResultInfo(
                album=album,
                pdf_files=cached,
                image_count=album.page_count,
                duration=time.monotonic() - started,
                cached=True,
            )

        async with self._download_sem:
            # 排队期间可能已经有别的请求生成好了 PDF
            cached = self._find_cached_pdfs(album_id)
            if cached:
                return DownloadResultInfo(
                    album=album,
                    pdf_files=cached,
                    image_count=album.page_count,
                    duration=time.monotonic() - started,
                    cached=True,
                )

            option = self._build_option()
            try:
                result = await asyncio.wait_for(
                    download_album_async(
                        album_id,
                        option,
                        check_exception=False,
                    ),
                    timeout=self.download_timeout,
                )
            except (asyncio.TimeoutError, TimeoutError) as exc:  # noqa: UP041 - 兼容 Python 3.10
                raise JMTimeoutError(
                    f"下载超过 {self.download_timeout} 秒，已取消；请检查网络或稍后重试"
                ) from exc
            except MissingAlbumPhotoException as exc:
                raise JMNotFoundError(f"JM{album_id} 不存在或已下架") from exc
            except JmcomicException as exc:
                raise JMDownloadError(f"下载失败：{exc}") from exc
            except Exception as exc:
                logger.exception("JM%s 下载失败", album_id)
                raise JMDownloadError(f"下载失败：{exc}") from exc

            album = result.detail
            paths = [
                Path(item)
                for item in result.manifest.image_filepath_list
                if Path(item).is_file() and Path(item).stat().st_size > 0
            ]
            failed_images = len(result.downloader.download_failed_image)
            if not paths:
                raise JMDownloadError("没有下载到任何图片，可能是网络或域名问题")

            pdf_path = self._pdf_path(album)
            try:
                pdf_files = await asyncio.wait_for(
                    asyncio.to_thread(
                        build_pdfs,
                        paths,
                        pdf_path,
                        pages_per_file=self.pdf_pages_per_file,
                        split_size_mb=self.pdf_split_size_mb,
                        jpeg_quality=self.pdf_jpeg_quality,
                        title=str(album.name or ""),
                        author="、".join(str(item) for item in (album.authors or [])),
                        convert_to_jpeg=True,
                    ),
                    timeout=self.pdf_timeout,
                )
            except (asyncio.TimeoutError, TimeoutError) as exc:  # noqa: UP041 - 兼容 Python 3.10
                raise JMDownloadError(
                    f"PDF 生成超过 {self.pdf_timeout} 秒，图片已保留，可稍后重试"
                ) from exc
            except PdfBuildError as exc:
                raise JMDownloadError(f"PDF 生成失败：{exc}") from exc
            except Exception as exc:
                logger.exception("JM%s PDF 生成失败", album_id)
                raise JMDownloadError(f"PDF 生成失败：{exc}") from exc

            if self.cleanup_images:
                self._cleanup_images(paths)

            duration = time.monotonic() - started
            logger.info(
                "JM%s 下载完成：%s 张图片，%s 个 PDF，共 %s，耗时 %.1fs",
                album.album_id,
                len(paths),
                len(pdf_files),
                human_size(sum(item.stat().st_size for item in pdf_files)),
                duration,
            )
            return DownloadResultInfo(
                album=album,
                pdf_files=pdf_files,
                image_count=len(paths),
                failed_images=failed_images,
                duration=duration,
                cached=False,
            )

    def _pdf_path(self, album: JmAlbumDetail) -> Path:
        name = sanitize_filename(f"JM{album.album_id}_{album.name}", 64)
        return self.pdf_dir / f"{name}.pdf"

    def _find_cached_pdfs(self, album_id: str) -> list[Path]:
        if not self.cache_pdf:
            return []
        if not self.pdf_dir.exists():
            return []

        candidates = set(self.pdf_dir.glob(f"JM{album_id}_*.pdf"))
        exact = self.pdf_dir / f"JM{album_id}.pdf"
        if exact.exists():
            candidates.add(exact)

        files: list[Path] = []
        for item in candidates:
            if not item.is_file() or item.stat().st_size == 0:
                continue
            if _is_valid_pdf(item):
                files.append(item)
            else:
                logger.warning("删除损坏的 PDF 缓存：%s", item.name)
                item.unlink(missing_ok=True)

        return sorted(files)

    def _cleanup_images(self, paths: Sequence[Path]) -> None:
        root = self.downloads_dir.resolve()
        for parent in {path.parent for path in paths}:
            try:
                resolved = parent.resolve()
            except OSError:
                continue
            if resolved == root or root not in resolved.parents:
                continue
            shutil.rmtree(resolved, ignore_errors=True)
            cleanup_empty_dir(resolved.parent, root)


def _looks_like_image(content: bytes) -> bool:
    if len(content) < 12:
        return False
    if content.startswith(b"\xff\xd8\xff"):  # JPEG
        return True
    if content.startswith(b"\x89PNG\r\n\x1a\n"):  # PNG
        return True
    if content.startswith((b"GIF87a", b"GIF89a")):
        return True
    return content[:4] == b"RIFF" and content[8:12] == b"WEBP"  # WebP


def _is_valid_pdf(path: Path) -> bool:
    try:
        size = path.stat().st_size
        if size < 100:
            return False
        with path.open("rb") as file:
            head = file.read(5)
            file.seek(max(0, size - 2048))
            tail = file.read()
        return head == b"%PDF-" and b"%%EOF" in tail
    except OSError:
        return False
