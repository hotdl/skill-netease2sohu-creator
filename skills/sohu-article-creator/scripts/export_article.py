#!/usr/bin/env python3
"""把单篇文章的 rewrite.txt 装配成含图的 rewrite.md，并导出 docx。

- 标题唯一来源是 state.db 里的原始标题：用于 docx 文件名；docx 正文不包含标题。
- rewrite.md 开头保留标题便于预览，md -> docx 时移除首个 H1。
- 默认输出 workspaces/output/<导出当天 yyyyMMdd>/<清理后的标题>.docx；
  同名自动加 _1、_2；当天重做同一篇复用同一文件名。
- 正文里误加的 H1、图片行会被剥掉并记录 warning。
- 默认先把原图底部裁掉（max(24px, 5% 宽度)），移除元数据，
  统一输出 JPEG quality=90 到 images_cropped/；原图 images/ 不动。
- 裁剪后的图按文件名顺序均匀插入正文；零图片时导出纯文字 docx。
- docx 生成后解压校验正文首段不是标题，通过后才把 state 置 1。
- stdout 只输出 JSON，日志走 stderr。

用法：
    python3 export_article.py LAMQTNFC05560WGN
    python3 export_article.py LAMQTNFC05560WGN --root workspaces --force
    python3 export_article.py LAMQTNFC05560WGN --out /tmp/成稿.docx --footer
    python3 export_article.py LAMQTNFC05560WGN --no-crop
    python3 export_article.py LAMQTNFC05560WGN --crop-bottom-px 40

退出码：0=导出成功或按规则跳过；1=正文/图片/导出/标题校验失败。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime
from pathlib import Path

import image_prep
import img2md
import make_docx
import state_store
from netease_common import (
    DEFAULT_ROOT,
    IMAGE_EXTS,
    log,
    sanitize_component,
)

H1_RE = re.compile(r"^\s{0,3}#(?!#)\s+")
IMAGE_LINE_RE = re.compile(r"^\s*!\[[^\]]*\]\([^)]*\)\s*$")
W_NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
DATE_FMT = "%Y%m%d"
MANIFEST_NAME = ".docx_manifest.json"


def output_json(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def fail(article_id: str, title: str, message: str, *, code: int = 1) -> int:
    log(f"[错误] {message}")
    output_json({
        "status": "error",
        "article_id": article_id,
        "title": title,
        "error": message,
    })
    return code


def read_body(path: Path, title: str) -> tuple[list[str], list[str]]:
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    warnings: list[str] = []
    kept: list[str] = []
    dropped_h1 = 0
    dropped_images = 0
    dropped_title = 0
    seen_content = False

    for line in lines:
        stripped = line.strip()
        if H1_RE.match(line):
            dropped_h1 += 1
            continue
        if IMAGE_LINE_RE.match(line):
            dropped_images += 1
            continue
        if not seen_content and stripped:
            seen_content = True
            if stripped == title.strip():
                dropped_title += 1
                continue
        kept.append(line)

    if dropped_h1:
        warnings.append(f"已从正文剥离 {dropped_h1} 个一级标题")
    if dropped_images:
        warnings.append(f"已从正文剥离 {dropped_images} 行图片 Markdown")
    if dropped_title:
        warnings.append("正文首行与标题相同，已剥离，避免文档内重复标题")
    return kept, warnings


def collect_images(article_dir: Path) -> tuple[list[Path], list[str]]:
    images_dir = article_dir / "images"
    if not images_dir.is_dir():
        return [], []
    images = sorted(
        (p for p in images_dir.iterdir()
         if p.is_file() and p.suffix.lower() in IMAGE_EXTS),
        key=lambda p: p.name,
    )
    return images, []


def build_markdown(article_dir: Path, title: str, body_lines: list[str],
                   images: list[Path]) -> tuple[str, int]:
    body_text = "\n".join(body_lines).strip()
    blocks = img2md.parse_blocks(body_text, per_line_paragraphs=True)
    rel_images = [p.relative_to(article_dir).as_posix() for p in images]
    mapping = img2md.assign_images(blocks, rel_images, min_gap=1)

    parts: list[str] = [f"# {title}", ""]
    image_number = 0
    for index, block in enumerate(blocks):
        parts.append("\n".join(block.lines))
        parts.append("")
        for rel in mapping.get(index, []):
            image_number += 1
            parts.append(f"![配图{image_number:02d}]({rel})")
            parts.append("")
    return "\n".join(parts).rstrip() + "\n", image_number


def load_manifest(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("manifest 不是 JSON 对象")
        if not isinstance(data.get("articles"), dict):
            data["articles"] = {}
        data.setdefault("schema_version", 1)
        return data
    except (OSError, ValueError, json.JSONDecodeError):
        return {"schema_version": 1, "articles": {}}


def write_manifest(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    tmp.replace(path)


def resolve_output(root: Path, title: str, article_id: str, *,
                   allocate: bool) -> tuple[Path, Path | None, dict | None]:
    """返回 (docx 路径, manifest 路径, manifest)。

    默认目录是导出当天 output/<yyyyMMdd>/；同一天同一篇复用 manifest 里的
    文件名；新文章用清理后的标题，重名时加 _1、_2。
    传 allocate=False 时只查已有分配，不占用新名字。
    """
    date_dir = root / "output" / datetime.now().strftime(DATE_FMT)
    manifest_path = date_dir / MANIFEST_NAME
    manifest = load_manifest(manifest_path)
    entries = manifest.setdefault("articles", {})

    existing = entries.get(article_id)
    if isinstance(existing, dict) and existing.get("file"):
        name = Path(str(existing["file"])).name
        if name:
            return date_dir / name, manifest_path, manifest

    base = sanitize_component(title, limit=60, fallback="untitled")
    if not allocate:
        return date_dir / f"{base}.docx", manifest_path, manifest

    claimed = {
        Path(str(value.get("file"))).name
        for key, value in entries.items()
        if key != article_id and isinstance(value, dict) and value.get("file")
    }
    candidate = date_dir / f"{base}.docx"
    seq = 1
    while candidate.exists() or candidate.name in claimed:
        candidate = date_dir / f"{base}_{seq}.docx"
        seq += 1
    return candidate, manifest_path, manifest


def record_output(manifest_path: Path, manifest: dict, article_id: str,
                  file_name: str, title: str) -> None:
    manifest.setdefault("articles", {})[article_id] = {
        "file": file_name,
        "title": title,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    write_manifest(manifest_path, manifest)


def verify_docx(path: Path, title: str) -> tuple[bool, int, str]:
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            xml_bytes = archive.read("word/document.xml")
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        return False, 0, f"docx 无法读取：{exc}"

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        return False, 0, f"docx XML 解析失败：{exc}"

    paragraphs = root.findall(".//w:p", W_NS)
    if not paragraphs:
        return False, 0, "docx 中没有段落"
    first = "".join(t.text or "" for t in paragraphs[0].findall(".//w:t", W_NS))
    if first == title:
        return False, 0, "docx 正文仍以标题开头；正文不应包含标题"

    embedded = sum(1 for name in names if name.startswith("word/media/"))
    return True, embedded, ""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="rewrite.txt -> 图文 rewrite.md -> docx，并校验标题",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("article_id", help="文章 ID，如 LAMQTNFC05560WGN")
    p.add_argument("--root", default=str(DEFAULT_ROOT), metavar="DIR",
                   help="数据根目录（默认：%(default)s）")
    p.add_argument("--body", metavar="PATH",
                   help="重写正文路径（默认：<article_dir>/rewrite.txt）")
    p.add_argument("--out", metavar="PATH",
                   help="docx 输出路径（默认：<root>/output/<标题>_<文章ID>.docx）")
    p.add_argument("--no-crop", action="store_true", help="不裁剪图片，直接使用原图")
    p.add_argument("--crop-bottom-px", type=int, default=0, metavar="N",
                   help="底部裁剪像素；0=自动（max(24, 5%% 宽)）")
    p.add_argument("--crop-right-px", type=int, default=0, metavar="N",
                   help="右侧裁剪像素（默认 0）")
    p.add_argument("--font", default="Microsoft YaHei",
                   help="中西文字体（默认：%(default)s）")
    p.add_argument("--footer", action="store_true", help="生成页码页脚（默认不生成）")
    p.add_argument("--footer-text", default="搜狐号成稿",
                   help="页脚前缀文案（默认：%(default)s）")
    p.add_argument("--force", action="store_true", help="已采用文章也重新导出")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.root).expanduser()
    conn = state_store.connect(root)
    row = state_store.get_article(conn, args.article_id)
    if row is None:
        return fail(args.article_id, "", f"状态库中没有文章 {args.article_id}")

    title = row["title"] or ""
    if not title:
        return fail(args.article_id, "", "状态库里缺少文章标题")
    if not state_store.is_crawled(conn, args.article_id, root=root):
        return fail(args.article_id, title,
                    "正文未抓取，或 content.txt 缺失/为空；请先抓正文")

    manifest_path: Path | None = None
    manifest: dict | None = None
    if args.out:
        out_path = Path(args.out).expanduser()
    else:
        out_path, manifest_path, manifest = resolve_output(
            root, title, args.article_id, allocate=False)

    if int(row["state"]) == 1 and not args.force:
        log(f"[跳过] {title} 已采用；如需重做请加 --force")
        output_json({
            "status": "skipped_adopted",
            "article_id": args.article_id,
            "title": title,
            "state": 1,
            "docx": str(out_path) if out_path.exists() else "",
        })
        return 0

    if not args.out:
        out_path, manifest_path, manifest = resolve_output(
            root, title, args.article_id, allocate=True)

    article_dir = root / row["dir_path"]
    body_path = (Path(args.body).expanduser() if args.body
                 else article_dir / "rewrite.txt")
    if not body_path.is_file():
        return fail(args.article_id, title, f"找不到重写正文：{body_path}")

    try:
        body_lines, warnings = read_body(body_path, title)
    except OSError as exc:
        return fail(args.article_id, title, f"读取重写正文失败：{exc}")

    if not any(line.strip() for line in body_lines):
        return fail(args.article_id, title, "重写正文为空")

    raw_images, image_warnings = collect_images(article_dir)
    warnings.extend(image_warnings)
    if args.no_crop:
        webp = [p.name for p in raw_images if p.suffix.lower() == ".webp"]
        if webp:
            warnings.append("裁剪已关闭，WebP 图片在部分 Word 版本中可能显示异常："
                            + ", ".join(webp))
    images, prep_warnings, crop_stats = image_prep.prepare_images(
        article_dir, raw_images,
        enabled=not args.no_crop,
        bottom_px=args.crop_bottom_px,
        right_px=args.crop_right_px,
    )
    warnings.extend(prep_warnings)
    md_text, inserted = build_markdown(article_dir, title, body_lines, images)
    md_path = article_dir / "rewrite.md"
    try:
        md_path.write_text(md_text, encoding="utf-8")
    except OSError as exc:
        return fail(args.article_id, title, f"写 rewrite.md 失败：{exc}")

    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return fail(args.article_id, title, f"创建输出目录失败：{exc}")
    tmp_path = out_path.with_name(out_path.name + ".partial")
    try:
        with contextlib.redirect_stdout(sys.stderr):
            rc = make_docx.build_docx(
                md_path, tmp_path, True, False, args.font, args.footer,
                title_override="", footer_text=args.footer_text,
                render_title=False,
            )
    except Exception as exc:  # noqa: BLE001 - 统一转成 JSON 错误
        tmp_path.unlink(missing_ok=True)
        return fail(args.article_id, title, f"生成 docx 失败：{exc}")

    if rc != 0 or not tmp_path.is_file() or tmp_path.stat().st_size == 0:
        tmp_path.unlink(missing_ok=True)
        return fail(args.article_id, title, "生成 docx 失败（make_docx 返回非 0）")

    title_ok, embedded, verify_error = verify_docx(tmp_path, title)
    if not title_ok:
        tmp_path.unlink(missing_ok=True)
        return fail(args.article_id, title, verify_error)

    try:
        tmp_path.replace(out_path)
    except OSError as exc:
        tmp_path.unlink(missing_ok=True)
        return fail(args.article_id, title, f"保存 docx 失败：{exc}")

    if manifest_path is not None and manifest is not None:
        try:
            record_output(manifest_path, manifest, args.article_id,
                          out_path.name, title)
        except OSError as exc:
            warnings.append(f"写 docx 清单失败：{exc}")

    if images and embedded != len(images):
        warnings.append(f"嵌入图片 {embedded} 张，实际图片 {len(images)} 张，请检查缺图")
    if inserted != len(images):
        warnings.append(f"排版插入 {inserted} 张，实际图片 {len(images)} 张")

    marked = state_store.set_state(conn, args.article_id, 1)
    if not marked:
        warnings.append("状态库标记已采用失败（记录可能已被删除）")

    log(f"[完成] {title} -> {out_path}")
    output_json({
        "status": "exported",
        "article_id": args.article_id,
        "title": title,
        "title_verified": True,
        "body": str(body_path),
        "md": str(md_path),
        "docx": str(out_path),
        "images": len(raw_images),
        "cropped_images": crop_stats.get("cropped", 0),
        "skipped_crops": crop_stats.get("skipped", 0),
        "failed_crops": crop_stats.get("failed", 0),
        "crop_enabled": crop_stats.get("enabled", False),
        "crop_bottom_px": args.crop_bottom_px,
        "crop_right_px": args.crop_right_px,
        "jpeg_quality": (crop_stats.get("jpeg_quality")
                         if crop_stats.get("enabled") else None),
        "metadata_removed": bool(crop_stats.get("enabled")
                                 and crop_stats.get("cropped")),
        "inserted_images": inserted,
        "embedded_images": embedded,
        "state": 1 if marked else int(row["state"]),
        "warnings": warnings,
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
