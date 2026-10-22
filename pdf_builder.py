"""把下载好的漫画图片合并成 PDF。

优先使用 img2pdf（无损嵌入 JPEG，速度快、体积小）；当 img2pdf 不可用时
退回 Pillow。图片会先统一转成 JPEG，避免 WebP/PNG 在 PDF 里膨胀。
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Sequence
from pathlib import Path

from astrbot.api import logger
from PIL import Image, ImageOps, UnidentifiedImageError


class PdfBuildError(Exception):
    """PDF 生成失败。"""


def build_pdfs(
    image_paths: Sequence[str | Path],
    output_path: str | Path,
    *,
    pages_per_file: int = 0,
    split_size_mb: int = 0,
    jpeg_quality: int = 90,
    title: str = "",
    author: str = "",
    convert_to_jpeg: bool = True,
) -> list[Path]:
    """把图片按顺序合并成 PDF。

    Args:
        image_paths: 图片路径，顺序即 PDF 页顺序。
        output_path: 单文件时的输出路径；分卷时会追加 ``_part01`` 等后缀。
        pages_per_file: 每个 PDF 最多多少页，0 表示不按页数分卷。
        split_size_mb: 每个 PDF 的预估大小上限（MB），0 表示不按大小分卷。
        jpeg_quality: 转 JPEG 时的质量，60-100。
        title: PDF 元数据标题。
        author: PDF 元数据作者。
        convert_to_jpeg: 是否统一转成 JPEG（推荐，WebP 体积可缩小 4-5 倍）。

    Returns:
        实际生成的 PDF 路径列表。

    Raises:
        PdfBuildError: 没有任何可用图片，或 PDF 写入失败。
    """
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    paths: list[Path] = []
    seen: set[str] = set()
    for item in image_paths:
        path = Path(item)
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if path.is_file() and path.stat().st_size > 0:
            paths.append(path)

    if not paths:
        raise PdfBuildError("没有可用的图片，无法生成 PDF")

    quality = max(60, min(100, int(jpeg_quality or 90)))
    temp_dir = Path(tempfile.mkdtemp(prefix=".pdf_", dir=str(output.parent)))
    try:
        prepared, skipped = _prepare_images(
            paths,
            temp_dir,
            quality=quality,
            convert_to_jpeg=convert_to_jpeg,
        )
        if not prepared:
            raise PdfBuildError("所有图片都无法读取，PDF 生成失败")
        if skipped:
            logger.warning("PDF 生成时跳过了 %s 张损坏/不支持的图片", skipped)

        chunks = _split_chunks(prepared, pages_per_file, split_size_mb)
        results: list[Path] = []
        for index, chunk in enumerate(chunks):
            if len(chunks) == 1:
                part_path = output
            else:
                part_path = output.with_name(
                    f"{output.stem}_part{index + 1:02d}{output.suffix}"
                )
            _write_pdf(chunk, part_path, title=title, author=author)
            results.append(part_path)
            logger.info(
                "PDF 分卷 %s/%s 已生成: %s（%s 页）",
                index + 1,
                len(chunks),
                part_path.name,
                len(chunk),
            )
        return results
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _prepare_images(
    paths: Sequence[Path],
    temp_dir: Path,
    *,
    quality: int,
    convert_to_jpeg: bool,
) -> tuple[list[Path], int]:
    prepared: list[Path] = []
    skipped = 0

    for index, path in enumerate(paths):
        try:
            if not convert_to_jpeg and path.suffix.lower() in {".jpg", ".jpeg", ".png"}:
                with Image.open(path) as image:
                    image.verify()
                prepared.append(path)
                continue

            with Image.open(path) as image:
                image.seek(0)
                image.load()
                frame = ImageOps.exif_transpose(image)

                # 透明背景统一铺白，避免 PDF 里出现黑底
                if frame.mode in {"RGBA", "LA", "P"} or "transparency" in image.info:
                    rgba = frame.convert("RGBA")
                    background = Image.new("RGB", rgba.size, (255, 255, 255))
                    background.paste(rgba, mask=rgba.getchannel("A"))
                    frame = background
                elif frame.mode != "RGB":
                    frame = frame.convert("RGB")

                out = temp_dir / f"{index:05d}.jpg"
                frame.save(
                    out,
                    format="JPEG",
                    quality=quality,
                    optimize=True,
                )
                prepared.append(out)
        except (
            UnidentifiedImageError,
            OSError,
            ValueError,
            Image.DecompressionBombError,
        ) as exc:
            skipped += 1
            logger.warning("跳过无法读取的图片 %s: %s", path.name, exc)
        except Exception as exc:  # noqa: BLE001 - Pillow 在极端情况下会抛其他异常
            skipped += 1
            logger.warning("处理图片 %s 失败: %s", path.name, exc)

    return prepared, skipped


def _split_chunks(
    images: Sequence[Path],
    pages_per_file: int,
    split_size_mb: int,
) -> list[list[Path]]:
    if pages_per_file and pages_per_file > 0:
        step = int(pages_per_file)
        return [list(images[i : i + step]) for i in range(0, len(images), step)]

    if split_size_mb and split_size_mb > 0:
        limit = int(split_size_mb) * 1024 * 1024 * 0.95
        chunks: list[list[Path]] = []
        current: list[Path] = []
        current_size = 0.0
        for image in images:
            size = float(image.stat().st_size)
            if current and current_size + size > limit:
                chunks.append(current)
                current = []
                current_size = 0.0
            current.append(image)
            current_size += size
        if current:
            chunks.append(current)
        return chunks

    return [list(images)]


def _write_pdf(
    images: Sequence[Path],
    output: Path,
    *,
    title: str,
    author: str,
) -> None:
    # 先写临时文件再原子替换，避免进程中断留下能被当成缓存的半截 PDF
    temp_output = output.with_name(f".{output.stem}.part{output.suffix}")
    temp_output.unlink(missing_ok=True)

    try:
        import img2pdf
    except ImportError:  # pragma: no cover - requirements 里已经包含，兜底用
        logger.warning("未安装 img2pdf，改用 Pillow 生成 PDF（内存占用更高）")
        _write_pdf_with_pillow(images, temp_output)
    else:
        kwargs: dict[str, object] = {}
        rotation = getattr(getattr(img2pdf, "Rotation", None), "ifvalid", None)
        if rotation is not None:
            kwargs["rotation"] = rotation
        if title:
            kwargs["title"] = title
        if author:
            kwargs["author"] = author

        try:
            with open(temp_output, "wb") as file:
                img2pdf.convert(
                    [str(image) for image in images],
                    outputstream=file,
                    **kwargs,
                )
        except Exception as exc:
            temp_output.unlink(missing_ok=True)
            raise PdfBuildError(f"img2pdf 写入失败: {exc}") from exc

    if not temp_output.exists() or temp_output.stat().st_size == 0:
        temp_output.unlink(missing_ok=True)
        raise PdfBuildError("PDF 文件为空")

    try:
        temp_output.replace(output)
    except OSError as exc:
        temp_output.unlink(missing_ok=True)
        raise PdfBuildError(f"PDF 保存失败: {exc}") from exc


def _write_pdf_with_pillow(images: Sequence[Path], output: Path) -> None:
    opened: list[Image.Image] = []
    try:
        for image_path in images:
            image = Image.open(image_path)
            image.load()
            opened.append(image.convert("RGB"))

        if not opened:
            raise PdfBuildError("没有可用图片")

        opened[0].save(
            output,
            format="PDF",
            save_all=True,
            append_images=opened[1:],
            resolution=150.0,
        )
    except PdfBuildError:
        raise
    except Exception as exc:
        output.unlink(missing_ok=True)
        raise PdfBuildError(f"Pillow 写入 PDF 失败: {exc}") from exc
    finally:
        for image in opened:
            image.close()

    if not output.exists() or output.stat().st_size == 0:
        raise PdfBuildError("PDF 文件为空")


def cleanup_empty_dir(path: str | Path, root: str | Path) -> None:
    """删除 root 下变空的目录（只向上删到 root 为止）。"""
    root_path = Path(root).resolve()
    current = Path(path).resolve()
    while current != root_path and root_path in current.parents:
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent
