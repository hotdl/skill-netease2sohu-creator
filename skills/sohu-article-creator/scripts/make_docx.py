#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 Markdown 原样导出为 docx（纯标准库，不依赖 python-docx）。
- 标题、段落、列表、引用、代码块、表格、分割线按原顺序导出，不做内容筛选。
- `![alt](./images/x.jpg)` 会内嵌成图片（默认不带图注）；
  `**加粗**`、`*斜体*`、`` `等宽` `` 保留样式。
- 第一个一级标题作为文档标题，其余标题按层级导出；也可用 `--title` 指定精确标题
  （逐字保留，不经过 Markdown 行内解析）。`--no-title` 时不输出标题，只移除开头的 H1。
- 默认不生成页脚，需要页码时加 `--footer`；需要图注时加 `--captions`。
- 页脚文案默认「搜狐号成稿」，可用 `--footer-text` 覆盖。
- 缺图不报错，降级为一行灰色提示。

用法
----
python3 scripts/make_docx.py <article.md>                 # 输出同名 .docx（无图注、无页脚）
python3 scripts/make_docx.py <article.md> -o out.docx --no-images
python3 scripts/make_docx.py <article.md> --captions --footer
python3 scripts/make_docx.py <article.md> --title "逐字保留的标题" --footer-text "搜狐号成稿"
python3 scripts/make_docx.py <article.md> --no-title      # 正文不输出标题

退出码：0 成功；1 参数/文件错误或没有可导出的内容。
"""

from __future__ import annotations

import argparse
import os
import re
import struct
import sys
import zipfile
from pathlib import Path

EMU_PER_CM = 360000
MAX_W_CM = 12.6
PORTRAIT_W_CM = 9.6
MAX_H_CM = 13.5

INK = "222222"
GRAY = "777777"
RED = "8B2E2E"
SHADE_META = "F4F6F9"
SHADE_END = "FDF3F2"

BODY_SZ = 21        # 半磅：10.5pt
CAPTION_SZ = 17     # 8.5pt
TITLE_SZ = 42       # 21pt
HEADING_SZ = {1: 32, 2: 27, 3: 25, 4: 23, 5: 22, 6: 21}   # 半磅
LINE = "348"        # 1.45 倍行距（240 = 单倍）
TABLE_W = 9298      # 版心宽度（twips）：11906 - 左右边距 1304 * 2

HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*)$")
HR_RE = re.compile(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$")
LIST_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
IMG_RE = re.compile(r"^!\[([^\]]*)\]\(([^)]+)\)\s*$")
CAPTION_RE = re.compile(r"^\*[^*].*\*$")
INLINE_RE = re.compile(
    r"(!\[[^\]]*\]\([^)]+\)|\*\*.+?\*\*|`[^`]+`|\[[^\]]+\]\([^)]+\)|\*[^*\n]+?\*|_[^_\n]+?_)"
)


# ------------------------------------------------------------------ md 解析
def split_table_row(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c.strip() for c in line.split("|")]


def is_table_sep(line: str) -> bool:
    return all(re.fullmatch(r":?-{3,}:?", c) for c in split_table_row(line))


def parse_table(raw_rows: list[str]) -> tuple[list[list[str]], bool]:
    """拆 Markdown 表格；返回 (行数据, 是否有表头)。"""
    rows = [split_table_row(r) for r in raw_rows]
    has_header = len(rows) >= 2 and is_table_sep(raw_rows[1])
    if has_header:
        del rows[1]
    return rows, has_header


def parse_blocks(md_text: str) -> list[tuple[str, object]]:
    """按 Markdown 块顺序解析全文；不丢弃任何内容。"""
    lines = md_text.replace("\r\n", "\n").splitlines()
    blocks: list[tuple[str, object]] = []
    i, n = 0, len(lines)

    while i < n:
        raw = lines[i]
        s = raw.strip()
        if not s:
            i += 1
            continue

        # 围栏代码块
        if s.startswith("```") or s.startswith("~~~"):
            fence = s[:3]
            code: list[str] = []
            i += 1
            while i < n and not lines[i].strip().startswith(fence):
                code.append(lines[i])
                i += 1
            if i < n:
                i += 1
            blocks.append(("code", code))
            continue

        m = HEADING_RE.match(raw)
        if m:
            text = re.sub(r"\s+#+\s*$", "", m.group(2)).strip()
            blocks.append(("heading", (len(m.group(1)), text)))
            i += 1
            continue

        if HR_RE.match(raw):
            blocks.append(("hr", None))
            i += 1
            continue

        m = IMG_RE.match(s)
        if m:
            alt, src = m.group(1), m.group(2)
            caption = None
            j = i + 1
            while j < n and not lines[j].strip():
                j += 1
            if j < n and CAPTION_RE.match(lines[j].strip()):
                caption = lines[j].strip().strip("*")
                i = j
            blocks.append(("image", (alt, src, caption)))
            i += 1
            continue

        if s.startswith(">"):
            quote: list[str] = []
            while i < n and lines[i].strip().startswith(">"):
                quote.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            blocks.append(("quote", "\n".join(quote)))
            continue

        if LIST_RE.match(raw):
            items: list[tuple[int, str, str]] = []
            while i < n:
                cur = lines[i]
                if not cur.strip():
                    break
                lm = LIST_RE.match(cur)
                if lm:
                    indent = len(lm.group(1).expandtabs(4)) // 2
                    items.append((indent, lm.group(2), lm.group(3).strip()))
                    i += 1
                elif cur.startswith(("  ", "\t")) and items:
                    lvl, marker, text = items[-1]
                    items[-1] = (lvl, marker, text + " " + cur.strip())
                    i += 1
                else:
                    break
            blocks.append(("list", items))
            continue

        # 表格：带外框线，或第二行是分隔行（无外框线写法）
        nxt_s = lines[i + 1].strip() if i + 1 < n else ""
        if s.startswith("|") or ("|" in s and is_table_sep(nxt_s)):
            rows_raw: list[str] = [s]
            i += 1
            while i < n and "|" in lines[i] and lines[i].strip():
                rows_raw.append(lines[i].strip())
                i += 1
            blocks.append(("table", parse_table(rows_raw)))
            continue

        # 普通段落：连续非空行合并为一段
        para = [s]
        i += 1
        while i < n:
            nxt = lines[i]
            ns = nxt.strip()
            if not ns:
                break
            if (HEADING_RE.match(nxt) or HR_RE.match(nxt) or IMG_RE.match(ns)
                    or ns.startswith((">", "|", "```", "~~~")) or LIST_RE.match(nxt)
                    or ("|" in ns and i + 1 < n
                        and is_table_sep(lines[i + 1].strip()))):
                break
            para.append(ns)
            i += 1
        blocks.append(("paragraph", " ".join(para)))

    return blocks


def parse_inline(text: str) -> list[tuple[str, bool, bool, bool]]:
    """返回 [(片段, 是否加粗, 是否等宽, 是否斜体)]"""
    out: list[tuple[str, bool, bool, bool]] = []
    for piece in INLINE_RE.split(text):
        if not piece:
            continue
        if piece.startswith("**") and piece.endswith("**") and len(piece) > 4:
            out.append((piece[2:-2], True, False, False))
        elif piece.startswith("`") and piece.endswith("`") and len(piece) > 2:
            out.append((piece[1:-1], False, True, False))
        elif piece.startswith("!["):
            m = re.match(r"!\[([^\]]*)\]\(([^)]+)\)", piece)
            label = (m.group(1) or m.group(2)) if m else piece
            out.append((label, False, False, False))
        elif piece.startswith("["):
            m = re.match(r"\[([^\]]+)\]\(([^)]+)\)", piece)
            out.append((m.group(1) if m else piece, False, False, False))
        elif ((piece.startswith("*") and piece.endswith("*") and len(piece) > 2)
              or (piece.startswith("_") and piece.endswith("_") and len(piece) > 2)):
            out.append((piece[1:-1], False, False, True))
        else:
            out.append((piece, False, False, False))
    return out


# ------------------------------------------------------------------ 图片信息
def image_size(path: Path) -> tuple[int, int] | None:
    try:
        data = path.read_bytes()[:4096]
    except OSError:
        return None
    if data[:3] == b"\xff\xd8\xff":
        i = 2
        while i < len(data) - 9:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            seg = struct.unpack(">H", data[i + 2:i + 4])[0]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                          0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return w, h
            i += 2 + seg
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", data[16:24])
        return w, h
    if data[:6] in (b"GIF87a", b"GIF89a"):
        w, h = struct.unpack("<HH", data[6:10])
        return w, h
    return None


def display_size(path: Path) -> tuple[int, int]:
    """返回展示尺寸（EMU）。竖图窄一点，高度封顶。"""
    dims = image_size(path)
    if not dims:
        return int(MAX_W_CM * EMU_PER_CM), int(8 * EMU_PER_CM)
    w, h = dims
    ratio = h / w if w else 0.66
    width_cm = PORTRAIT_W_CM if ratio > 1.15 else MAX_W_CM
    if width_cm * ratio > MAX_H_CM:
        width_cm = MAX_H_CM / ratio
    return int(width_cm * EMU_PER_CM), int(width_cm * ratio * EMU_PER_CM)


# ------------------------------------------------------------------ XML 片段
def esc(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace('"', "&quot;"))


def run_xml(text: str, size: int = BODY_SZ, bold: bool = False, mono: bool = False,
            color: str = INK, italic: bool = False) -> str:
    font = "Consolas" if mono else "Microsoft YaHei"
    rpr = (
        f'<w:rFonts w:ascii="{font}" w:hAnsi="{font}" w:eastAsia="{font}" w:cs="{font}"/>'
        + ("<w:b/>" if bold else "")
        + ("<w:i/>" if italic else "")
        + f'<w:color w:val="{color}"/>'
        + f'<w:sz w:val="{size}"/><w:szCs w:val="{size}"/>'
    )
    return f'<w:r><w:rPr>{rpr}</w:rPr><w:t xml:space="preserve">{esc(text)}</w:t></w:r>'


def para_xml(runs: str, *, align: str = "both", size: int = BODY_SZ, after: int = 160,
             before: int = 0, shade: str | None = None, outline: int | None = None,
             indent: int = 0) -> str:
    ppr = ""
    if shade:
        ppr += f'<w:shd w:val="clear" w:color="auto" w:fill="{shade}"/>'
    ppr += f'<w:spacing w:before="{before}" w:after="{after}" w:line="{LINE}" w:lineRule="auto"/>'
    if indent:                       # ind 必须排在 jc 之前
        ppr += f'<w:ind w:left="{indent}"/>'
    ppr += f'<w:jc w:val="{align}"/>'
    if outline is not None:          # outlineLvl 必须排在 jc 之后
        ppr += f'<w:outlineLvl w:val="{outline}"/>'
    ppr += f'<w:rPr><w:sz w:val="{size}"/><w:szCs w:val="{size}"/></w:rPr>'
    return f"<w:p><w:pPr>{ppr}</w:pPr>{runs}</w:p>"


def drawing_xml(rel_id: str, cx: int, cy: int, pic_id: int, name: str) -> str:
    return (
        "<w:r><w:drawing>"
        '<wp:inline distT="0" distB="0" distL="0" distR="0">'
        f'<wp:extent cx="{cx}" cy="{cy}"/>'
        '<wp:effectExtent l="0" t="0" r="0" b="0"/>'
        f'<wp:docPr id="{pic_id}" name="Picture {pic_id}"/>'
        '<wp:cNvGraphicFramePr><a:graphicFrameLocks xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" noChangeAspect="1"/></wp:cNvGraphicFramePr>'
        '<a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        '<a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        '<pic:pic xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        f'<pic:nvPicPr><pic:cNvPr id="{pic_id}" name="{esc(name)}"/><pic:cNvPicPr/></pic:nvPicPr>'
        f'<pic:blipFill><a:blip r:embed="{rel_id}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
        '<pic:spPr><a:xfrm><a:off x="0" y="0"/>'
        f'<a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr>'
        "</pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r>"
    )


def table_xml(rows: list[list[str]], has_header: bool) -> str:
    ncols = max((len(r) for r in rows), default=1) or 1
    widths = [TABLE_W // ncols] * ncols
    widths[-1] += TABLE_W - sum(widths)

    borders = "".join(
        f'<w:{side} w:val="single" w:sz="4" w:space="0" w:color="BFBFBF"/>'
        for side in ("top", "left", "bottom", "right", "insideH", "insideV")
    )
    grid = "".join(f'<w:gridCol w:w="{w}"/>' for w in widths)

    trs: list[str] = []
    for r, row in enumerate(rows):
        cells: list[str] = []
        for c in range(ncols):
            text = row[c] if c < len(row) else ""
            bold = has_header and r == 0
            runs = "".join(
                run_xml(t, BODY_SZ, bold=bold or b, mono=m, italic=it)
                for t, b, m, it in parse_inline(text)
            )
            cells.append(
                f'<w:tc><w:tcPr><w:tcW w:w="{widths[c]}" w:type="dxa"/></w:tcPr>'
                '<w:p><w:pPr><w:spacing w:before="40" w:after="40" '
                'w:line="240" w:lineRule="auto"/><w:jc w:val="left"/></w:pPr>'
                f"{runs}</w:p></w:tc>"
            )
        trs.append(f'<w:tr>{"".join(cells)}</w:tr>')

    return (
        "<w:tbl><w:tblPr>"
        '<w:tblW w:w="5000" w:type="pct"/>'
        f"<w:tblBorders>{borders}</w:tblBorders>"
        '<w:tblLayout w:type="fixed"/>'
        "</w:tblPr>"
        f"<w:tblGrid>{grid}</w:tblGrid>"
        + "".join(trs)
        + "</w:tbl>"
    )


def hr_xml() -> str:
    return (
        "<w:p><w:pPr>"
        '<w:pBdr><w:bottom w:val="single" w:sz="6" w:space="1" w:color="CCCCCC"/></w:pBdr>'
        '<w:spacing w:before="120" w:after="120" w:line="240" w:lineRule="auto"/>'
        "</w:pPr></w:p>"
    )


# ------------------------------------------------------------------ 组装
def build_docx(md_path: Path, out_path: Path, with_images: bool, with_captions: bool,
               font: str, footer: bool, title_override: str = "",
               footer_text: str = "搜狐号成稿", render_title: bool = True) -> int:
    md_text = md_path.read_text(encoding="utf-8")
    blocks = parse_blocks(md_text)

    # 标题来源：优先使用调用方给的精确标题；否则取首个一级标题。
    # render_title=False 时不输出标题，但仍移除开头的 H1（通常是标题行）。
    title = ""
    if render_title:
        if title_override:
            title = title_override
            if (blocks and blocks[0][0] == "heading" and blocks[0][1][0] == 1
                    and blocks[0][1][1].strip() == title.strip()):
                blocks = blocks[1:]
        elif blocks and blocks[0][0] == "heading" and blocks[0][1][0] == 1:
            title = blocks[0][1][1]
            blocks = blocks[1:]
    elif blocks and blocks[0][0] == "heading" and blocks[0][1][0] == 1:
        blocks = blocks[1:]

    if not blocks and not title:
        print("错误：没有可导出的内容", file=sys.stderr)
        return 1

    base = md_path.parent
    parts: list[str] = []
    media: list[tuple[str, bytes]] = []          # (zip 内路径, 数据)
    rels: list[tuple[str, str, str]] = []        # (rId, type, target)
    pic_id = 0
    img_seq = 0

    # 标题：单 run 输出，避免标题里的 * _ [ ] 被当成 Markdown 行内语法
    if title:
        parts.append(para_xml(run_xml(title, TITLE_SZ, bold=True, color=INK),
                              align="center", size=TITLE_SZ, after=240))

    for kind, value in blocks:
        if kind == "heading":
            level, text = value
            size = HEADING_SZ.get(level, BODY_SZ)
            runs = "".join(run_xml(t, size, bold=True, mono=m, italic=it)
                           for t, b, m, it in parse_inline(text))
            parts.append(para_xml(runs, align="left", size=size, after=120,
                                  before=240 if level <= 2 else 160,
                                  outline=level - 1))
        elif kind == "paragraph":
            line = str(value)
            if line.startswith("【"):
                parts.append(para_xml(run_xml(line, BODY_SZ, bold=True, color=RED),
                                      align="both", after=200, before=200,
                                      shade=SHADE_END))
                continue
            runs = "".join(run_xml(t, BODY_SZ, bold=b, mono=m, italic=it)
                           for t, b, m, it in parse_inline(line))
            parts.append(para_xml(runs))
        elif kind == "quote":
            for ln in str(value).splitlines():
                runs = "".join(run_xml(t, BODY_SZ, bold=b, mono=m, italic=it, color=GRAY)
                               for t, b, m, it in parse_inline(ln))
                parts.append(para_xml(runs, align="left", shade=SHADE_META,
                                      indent=360, after=80))
        elif kind == "list":
            for level, marker, text in value:
                prefix = "• " if marker in ("-", "*", "+") else f"{marker} "
                runs = "".join(run_xml(t, BODY_SZ, bold=b, mono=m, italic=it)
                               for t, b, m, it in parse_inline(prefix + text))
                parts.append(para_xml(runs, align="left", indent=360 * (level + 1),
                                      after=60))
        elif kind == "code":
            for ln in value:
                parts.append(para_xml(run_xml(ln or " ", BODY_SZ, mono=True),
                                      align="left", shade=SHADE_META, indent=240,
                                      after=0, before=0))
            parts.append('<w:p><w:pPr><w:spacing w:after="140"/></w:pPr></w:p>')
        elif kind == "table":
            rows, has_header = value
            parts.append(table_xml(rows, has_header))
            parts.append('<w:p><w:pPr><w:spacing w:after="120"/></w:pPr></w:p>')
        elif kind == "hr":
            parts.append(hr_xml())
        elif kind == "image":
            alt, src, caption = value
            if not with_images:
                if caption:          # 不嵌图也保留图注文字
                    parts.append(para_xml(run_xml(caption, BODY_SZ, italic=True)))
                continue
            ipath = (base / src).resolve() if not os.path.isabs(src) else Path(src)
            if not ipath.exists():
                parts.append(para_xml(run_xml(f"[缺图] {src}", CAPTION_SZ, color=GRAY),
                                      align="center", size=CAPTION_SZ, after=120, before=120))
                continue
            ext = ipath.suffix.lower().lstrip(".") or "jpg"
            if ext == "jpeg":
                ext = "jpg"
            img_seq += 1
            pic_id += 1
            rel_id = f"rIdImg{img_seq}"
            zip_name = f"word/media/image{img_seq}.{ext}"
            media.append((zip_name, ipath.read_bytes()))
            rels.append((rel_id,
                         "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image",
                         f"media/image{img_seq}.{ext}"))
            cx, cy = display_size(ipath)
            parts.append(para_xml(drawing_xml(rel_id, cx, cy, pic_id, ipath.name),
                                  align="center", after=40, before=120))
            if with_captions:
                label = caption or alt
                if label:
                    parts.append(para_xml(run_xml(label, CAPTION_SZ, color=GRAY, italic=True),
                                          align="center", size=CAPTION_SZ, after=200))
            elif caption:
                # 不生成图注时，图注仍是正文中的斜体段落，照常导出
                parts.append(para_xml(run_xml(caption, BODY_SZ, italic=True)))

    if not parts:
        print("错误：没有可导出的内容，未生成 docx", file=sys.stderr)
        return 1

    sect_ppr = (
        '<w:sectPr>'
        + ('<w:footerReference w:type="default" r:id="rIdFooter"/>' if footer else "")
        + '<w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1247" w:right="1304" w:bottom="1134" w:left="1304" '
        'w:header="851" w:footer="992" w:gutter="0"/>'
        '<w:cols w:space="425"/><w:docGrid w:linePitch="312"/>'
        "</w:sectPr>"
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        f"<w:body>{''.join(parts)}{sect_ppr}</w:body></w:document>"
    )

    footer_text = (footer_text or "搜狐号成稿").strip() or "搜狐号成稿"
    footer_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:ftr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:p><w:pPr><w:jc w:val="center"/>'
        f'<w:rPr><w:sz w:val="{CAPTION_SZ}"/></w:rPr></w:pPr>'
        f'<w:r><w:rPr><w:sz w:val="{CAPTION_SZ}"/><w:color w:val="{GRAY}"/></w:rPr>'
        f'<w:t xml:space="preserve">{esc(footer_text)}    第 </w:t></w:r>'
        '<w:fldSimple w:instr=" PAGE "><w:r><w:rPr>'
        f'<w:sz w:val="{CAPTION_SZ}"/><w:color w:val="{GRAY}"/></w:rPr><w:t>1</w:t></w:r></w:fldSimple>'
        f'<w:r><w:rPr><w:sz w:val="{CAPTION_SZ}"/><w:color w:val="{GRAY}"/></w:rPr>'
        '<w:t xml:space="preserve"> 页</w:t></w:r></w:p></w:ftr>'
    )

    styles_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:docDefaults><w:rPrDefault><w:rPr>"
        f'<w:rFonts w:ascii="{font}" w:eastAsia="{font}" w:hAnsi="{font}" w:cs="{font}"/>'
        f'<w:color w:val="{INK}"/><w:sz w:val="{BODY_SZ}"/><w:szCs w:val="{BODY_SZ}"/>'
        "</w:rPr></w:rPrDefault><w:pPrDefault><w:pPr>"
        f'<w:spacing w:after="160" w:line="{LINE}" w:lineRule="auto"/>'
        "</w:pPr></w:pPrDefault></w:docDefaults>"
        '<w:style w:type="paragraph" w:default="1" w:styleId="Normal">'
        '<w:name w:val="Normal"/><w:qFormat/></w:style>'
        "</w:styles>"
    )

    exts = {m[0].rsplit(".", 1)[-1].lower() for m in media}
    ct_defaults = "".join(
        f'<Default Extension="{e}" ContentType="image/{"jpeg" if e == "jpg" else e}"/>'
        for e in sorted(exts)
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        f"{ct_defaults}"
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
        + ('<Override PartName="/word/footer1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"/>' if footer else "")
        + "</Types>"
    )

    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        "</Relationships>"
    )

    doc_rels = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rIdStyles" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>']
    for rid, rtype, target in rels:
        doc_rels.append(f'<Relationship Id="{rid}" Type="{rtype}" Target="{target}"/>')
    if footer:
        doc_rels.append('<Relationship Id="rIdFooter" '
                        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer" '
                        'Target="footer1.xml"/>')
    doc_rels.append("</Relationships>")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", root_rels)
        z.writestr("word/document.xml", document)
        z.writestr("word/styles.xml", styles_xml)
        z.writestr("word/_rels/document.xml.rels", "".join(doc_rels))
        if footer:
            z.writestr("word/footer1.xml", footer_xml)
        for name, data in media:
            z.writestr(name, data)
    tmp.replace(out_path)

    n_paras = len([p for p in parts if p.startswith("<w:p>")])
    print(f"saved: {out_path}  ({out_path.stat().st_size / 1024:.1f} KB, "
          f"段落 {n_paras}, 图片 {len(media)})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Markdown -> docx（不做内容筛选，纯标准库）",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument("md", help="Markdown 路径")
    p.add_argument("-o", "--output", help="输出 docx（默认与 md 同名同目录）")
    p.add_argument("--no-images", action="store_true", help="不嵌入配图（纯文字）")
    p.add_argument("--captions", action="store_true", help="图片下方显示图注（默认不显示）")
    p.add_argument("--font", default="Microsoft YaHei", help="中西文字体（默认 Microsoft YaHei）")
    p.add_argument("--title", default="", help="覆盖文档标题（逐字保留，默认取首个一级标题）")
    p.add_argument("--no-title", action="store_true",
                   help="正文不输出标题（移除开头的 H1；--title 不再生效）")
    p.add_argument("--footer", action="store_true", help="生成页码页脚（默认不生成）")
    p.add_argument("--footer-text", default="搜狐号成稿",
                   help="页脚前缀文案（默认：%(default)s）")
    return p


def main() -> int:
    args = build_parser().parse_args()
    md_path = Path(args.md).expanduser().resolve()
    if not md_path.exists():
        print(f"错误：找不到 {md_path}", file=sys.stderr)
        return 1
    out = Path(args.output).expanduser() if args.output else md_path.with_suffix(".docx")
    try:
        return build_docx(md_path, out, not args.no_images, args.captions,
                          args.font, args.footer, args.title, args.footer_text,
                          render_title=not args.no_title)
    except OSError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
