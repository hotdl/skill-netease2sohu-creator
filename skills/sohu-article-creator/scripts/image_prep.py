#!/usr/bin/env python3
"""文章图片预处理：固定裁掉底部，移除元数据，统一输出 JPEG quality=90。


- 原图 `images/` 不动，裁剪结果写到 `<article_dir>/images_cropped/*.jpg`。
- 裁剪后转成全新的 RGB 图像再保存，不传 exif/icc_profile/xmp/comment，
  从而移除 EXIF/GPS/ICC 等元数据；带 alpha 的图合成到白底。
- 极小图或裁剪失败时回退原图，由调用方决定是否继续导出。
"""

from __future__ import annotations

from pathlib import Path

try:
    from PIL import Image, ImageOps
except ImportError:  # pragma: no cover - 环境缺少 Pillow 时回退原图
    Image = None
    ImageOps = None

DEFAULT_BOTTOM_RATIO = 0.05
MIN_BOTTOM_PX = 24
MAX_CROP_RATIO = 0.25
MIN_REMAINING_PX = 16
JPEG_QUALITY = 90
CROPPED_DIRNAME = "images_cropped"


def resolve_bottom_margin(width: int, bottom_px: int = 0) -> int:
    """0 表示自动：max(24, 5% 宽度)；显式值则按像素。"""
    if bottom_px > 0:
        return int(bottom_px)
    return max(MIN_BOTTOM_PX, round(width * DEFAULT_BOTTOM_RATIO))


def _has_alpha(image: "Image.Image") -> bool:
    return (image.mode in ("RGBA", "LA")
            or (image.mode == "P" and "transparency" in image.info))


def crop_image(src: Path, dst: Path, *, bottom_px: int = 0,
               right_px: int = 0) -> tuple[bool, str]:
    """裁剪 src 并输出干净 JPEG。返回 (是否成功, 失败原因)。"""
    if Image is None or ImageOps is None:
        return False, "Pillow 未安装"

    try:
        with Image.open(src) as opened:
            # 先按原始尺寸判断，避免对极小图做无意义/易失败的解码与转换
            if min(opened.size) < MIN_REMAINING_PX:
                return False, "图片太小，跳过裁剪"
            image = ImageOps.exif_transpose(opened)
            width, height = image.size
            bottom = resolve_bottom_margin(width, bottom_px)
            right = max(0, int(right_px))
            bottom = min(bottom, round(height * MAX_CROP_RATIO))
            right = min(right, round(width * MAX_CROP_RATIO))

            new_width, new_height = width - right, height - bottom
            if new_width < MIN_REMAINING_PX or new_height < MIN_REMAINING_PX:
                return False, "图片太小，跳过裁剪"

            cropped = image.crop((0, 0, new_width, new_height))
            if _has_alpha(cropped):
                rgba = cropped.convert("RGBA")
                clean = Image.new("RGB", rgba.size, (255, 255, 255))
                clean.paste(rgba, mask=rgba.split()[-1])
            else:
                rgb = cropped.convert("RGB")
                clean = Image.new("RGB", rgb.size)
                clean.paste(rgb)

            dst.parent.mkdir(parents=True, exist_ok=True)
            clean.save(dst, "JPEG", quality=JPEG_QUALITY, optimize=True)
        return True, ""
    except Exception as exc:  # noqa: BLE001 - 单张失败不应中断整批
        return False, str(exc)


def prepare_images(article_dir: Path, images: list[Path], *, enabled: bool = True,
                   bottom_px: int = 0, right_px: int = 0) -> tuple[list[Path], list[str], dict]:
    """批量裁剪。成功返回 images_cropped 路径，失败/太小回退原图。

    返回 (用于排版的图片路径, warnings, stats)。
    """
    stats = {
        "enabled": bool(enabled),
        "bottom_px": int(bottom_px),
        "right_px": int(right_px),
        "cropped": 0,
        "skipped": 0,
        "failed": 0,
        "jpeg_quality": JPEG_QUALITY,
        "metadata_removed": True,
    }
    warnings: list[str] = []
    if not enabled or not images:
        return list(images), warnings, stats
    if Image is None or ImageOps is None:
        stats["enabled"] = False
        warnings.append("Pillow 未安装，跳过图片裁剪与元数据清理")
        return list(images), warnings, stats

    cropped_dir = article_dir / CROPPED_DIRNAME
    try:
        cropped_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        stats["enabled"] = False
        warnings.append(f"创建图片裁剪目录失败：{exc}；改用原图")
        return list(images), warnings, stats

    processed: list[Path] = []
    expected: set[str] = set()

    for src in images:
        dst = cropped_dir / f"{src.stem}.jpg"
        ok, message = crop_image(src, dst, bottom_px=bottom_px, right_px=right_px)
        if ok:
            processed.append(dst)
            expected.add(dst.name)
            stats["cropped"] += 1
            continue

        processed.append(src)
        if "太小" in message:
            stats["skipped"] += 1
            warnings.append(f"[跳过裁剪] {src.name}：{message}")
        else:
            stats["failed"] += 1
            warnings.append(f"[图片裁剪失败] {src.name}：{message}；改用原图")

    # 清掉上一轮遗留、本轮不再使用的裁剪图
    try:
        for stale in cropped_dir.iterdir():
            if stale.is_file() and stale.name not in expected:
                stale.unlink(missing_ok=True)
    except OSError as exc:
        warnings.append(f"清理旧裁剪图失败：{exc}")

    return processed, warnings, stats
