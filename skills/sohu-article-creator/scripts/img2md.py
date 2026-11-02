#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
img2md —— 把一组图片按段落"智能"插入 Markdown 正文，生成图文混排稿。

核心思路：不依赖图文语义匹配，而是按正文的"体量"均匀布点。
把每张图放到「累计字数等分点」最近的段落边界上，
于是长段落之间图距自然拉大，短段落之间图距自然收紧，
读起来节奏均匀，不会出现三张图挤在开头、后半段干巴巴的情况。

用法示例：
    python3 img2md.py input/001.md -i a.jpg b.jpg c.jpg -o output/001_out.md --copy-images
    python3 img2md.py input/001.md -i img/*.jpg -o output/001_out.md --html output/preview.html
    python3 img2md.py input/001.md -i img/*.jpg -o out.md --mode even-span --min-gap 2
    python3 img2md.py content.txt -i images/*.jpg -o out.md --title "文章标题"
"""

from __future__ import annotations

import argparse
import glob
import html
import os
import re
import shutil
import sys
from dataclasses import dataclass, field

# ---------------------------------------------------------------- 块解析

CONTENT_KINDS = {"paragraph", "quote", "list", "table"}


@dataclass
class Block:
    kind: str                 # heading | paragraph | quote | list | table | code | hr | image
    lines: list[str] = field(default_factory=list)
    raw_lines: list[str] = field(default_factory=list)  # 原始行（保留缩进/前后空行信息）

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    @property
    def chars(self) -> int:
        """有效字数：去掉空白与 markdown 符号后的长度，用于计算布点位置。"""
        t = re.sub(r"[#>*_`\-\[\]()|]", "", self.text)
        return len(re.sub(r"\s+", "", t))


HR_RE = re.compile(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$")
HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s")
UL_RE = re.compile(r"^\s{0,3}[-*+]\s+")
OL_RE = re.compile(r"^\s{0,3}\d+[.)]\s+")
IMG_ONLY_RE = re.compile(r"^\s*!\[[^\]]*\]\([^)]*\)\s*$")


def parse_blocks(md: str, *, per_line_paragraphs: bool = False) -> list[Block]:
    """把正文拆成块。

    per_line_paragraphs=True 用于纯文本：crawler 落盘的 content.txt 一行就是一段，
    若不按行拆段，所有非空行会被合并成唯一段落，图片就全部堆到文章末尾。
    Markdown 则保持标准语义：连续非空行属于同一段。
    """
    lines = md.replace("\r\n", "\n").split("\n")
    blocks: list[Block] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            blocks.append(Block("paragraph", list(buf)))
            buf.clear()

    i, n = 0, len(lines)
    while i < n:
        raw = lines[i]
        s = raw.strip()

        if not s:
            flush()
            i += 1
            continue

        # 代码块（含围栏）
        if s.startswith("```") or s.startswith("~~~"):
            flush()
            fence = s[:3]
            code = [raw]
            i += 1
            while i < n and not lines[i].strip().startswith(fence):
                code.append(lines[i])
                i += 1
            if i < n:
                code.append(lines[i])
                i += 1
            blocks.append(Block("code", code))
            continue

        if HEADING_RE.match(raw):
            flush()
            blocks.append(Block("heading", [raw]))
            i += 1
            continue

        if HR_RE.match(raw):
            flush()
            blocks.append(Block("hr", [raw]))
            i += 1
            continue

        # 独立成行的图片
        if IMG_ONLY_RE.match(raw):
            flush()
            blocks.append(Block("image", [raw]))
            i += 1
            continue

        # 引用块
        if s.startswith(">"):
            flush()
            q = []
            while i < n and lines[i].strip().startswith(">"):
                q.append(lines[i])
                i += 1
            blocks.append(Block("quote", q))
            continue

        # 列表块（连续的列表行 + 其缩进续行）
        if UL_RE.match(raw) or OL_RE.match(raw):
            flush()
            lst = []
            while i < n:
                cur = lines[i]
                if not cur.strip():
                    break
                if UL_RE.match(cur) or OL_RE.match(cur):
                    lst.append(cur)
                    i += 1
                elif cur.startswith(("  ", "\t")) and lst:
                    lst.append(cur)
                    i += 1
                else:
                    break
            blocks.append(Block("list", lst))
            continue

        # 表格块
        if s.startswith("|"):
            flush()
            tb = []
            while i < n and lines[i].strip().startswith("|"):
                tb.append(lines[i])
                i += 1
            blocks.append(Block("table", tb))
            continue

        # 普通正文段落：纯文本一行一段；Markdown 连续非空行合并为一段
        if per_line_paragraphs:
            flush()
            blocks.append(Block("paragraph", [raw]))
            i += 1
            continue

        buf.append(raw)
        i += 1

    flush()
    return blocks


# ---------------------------------------------------------------- 布点算法


def pick_boundaries(blocks: list[Block], count: int, min_gap: int = 1) -> list[int]:
    """返回每个图片要插入的 block 下标（插在该块之后）。

    策略：累计字数等分点 —— 第 k 张图的目标位 = 总字数 * k / (count + 1)，
    在候选边界里挑「已放图最少、且累计字数最接近目标」的那一个。
    先分散再对位，所以既不会全堆文末，也不会几张挤在同一处。
    """
    if count <= 0:
        return []

    content_idx = [i for i, b in enumerate(blocks) if b.kind in CONTENT_KINDS]
    if not content_idx:
        return [len(blocks) - 1] * count

    # 累计字数：cum[i] = 到第 i 个块结束时正文累计字数
    cum: dict[int, int] = {}
    total = 0
    for i, b in enumerate(blocks):
        if b.kind in CONTENT_KINDS:
            total += b.chars
        cum[i] = total

    # 候选：每个内容块的结束处（图插在它后面）。
    # 代码块、分隔线、已有图片不算内容块，所以图不会压在代码上或图挨图。
    cands = list(content_idx)
    if not cands:
        return [content_idx[-1]] * count

    pos_of = {i: content_idx.index(i) for i in cands}
    n = len(cands)

    # 情况一：图不超过段落数 —— 按累计字数等分点逐张落位，保持顺序单调
    if count <= n:
        chosen: list[int] = []
        prev_c = -1
        for k in range(1, count + 1):
            target = total * k / (count + 1)
            pool = [i for i in cands if pos_of[i] >= prev_c] or cands
            if prev_c >= 0 and min_gap > 0:
                gapped = [i for i in pool if pos_of[i] - prev_c >= min_gap]
                pool = gapped or pool
            best = min(pool, key=lambda i: (abs(cum[i] - target), cum[i]))
            chosen.append(best)
            prev_c = pos_of[best]
        return chosen

    # 情况二：图比段落多 —— 每格先平均分一轮，余数按等间隔摊开，避免全堆在文末
    base, rem = divmod(count, n)
    counts = [base] * n
    for t in range(rem):
        idx = min(n - 1, int(round(t * n / rem)))
        while counts[idx] > base:
            idx = min(n - 1, idx + 1)
        counts[idx] += 1
    out: list[int] = []
    for j, i in enumerate(cands):
        out.extend([i] * counts[j])
    return out


def assign_images(blocks: list[Block], images: list[str], min_gap: int) -> dict[int, list[str]]:
    slots = pick_boundaries(blocks, len(images), min_gap=min_gap)
    mapping: dict[int, list[str]] = {}
    for img, slot in zip(images, slots):
        mapping.setdefault(slot, []).append(img)
    return mapping


# ---------------------------------------------------------------- 渲染


def rel_or_abs(path: str, out_dir: str) -> str:
    """输出文档里引用的图片路径：尽量用相对路径，方便整包搬走。"""
    if re.match(r"^https?://", path):
        return path
    try:
        rel = os.path.relpath(os.path.abspath(path), os.path.abspath(out_dir))
    except ValueError:
        return path
    return rel.replace(os.sep, "/")


def build_markdown(blocks: list[Block], mapping: dict[int, list[str]], out_dir: str,
                   alt_prefix: str = "配图", captions: dict[str, str] | None = None,
                   title: str = "") -> str:
    """块与块之间空一行；图片作为独立元素参与排版，前后留空行。

    提供 title 时在文首插入一级标题；若正文首块已是同名标题则不重复插入。
    """
    elements: list[list[str]] = []
    poster_count = 0

    title = re.sub(r"\s+", " ", title).strip()
    if title:
        first_heading = ""
        if blocks and blocks[0].kind == "heading":
            first_heading = re.sub(r"^\s{0,3}#{1,6}\s+", "", blocks[0].lines[0]).strip()
        if first_heading != title:
            elements.append([f"# {title}"])

    for i, b in enumerate(blocks):
        elements.append(list(b.lines))
        for img in mapping.get(i, []):
            poster_count += 1
            alt = (captions or {}).get(img) or f"{alt_prefix}{poster_count:02d}"
            elements.append([f"![{alt}]({rel_or_abs(img, out_dir)})"])
    return "\n\n".join("\n".join(e) for e in elements if e).rstrip() + "\n"


# ---------------------------------------------------------------- HTML 预览

_INLINE = [
    (re.compile(r"`([^`]+)`"), r"<code>\1</code>"),
    (re.compile(r"\*\*([^*]+)\*\*"), r"<strong>\1</strong>"),
    (re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)"), r"<em>\1</em>"),
    (re.compile(r"\[([^\]]+)\]\(([^)]+)\)"), r'<a href="\2">\1</a>'),
]


def md_inline(t: str) -> str:
    t = html.escape(t)
    for pat, rep in _INLINE:
        t = pat.sub(rep, t)
    return t


def build_html(blocks: list[Block], mapping: dict[int, list[str]], out_dir: str, title: str = "") -> str:
    body: list[str] = []
    for i, b in enumerate(blocks):
        k = b.kind
        if k == "heading":
            m = re.match(r"^\s{0,3}(#{1,6})\s+(.*)$", b.lines[0])
            lvl = len(m.group(1)) if m else 2
            body.append(f"<h{lvl}>{md_inline(m.group(2).strip() if m else '')}</h{lvl}>")
        elif k == "paragraph":
            body.append("<p>" + md_inline(" ".join(x.strip() for x in b.lines)) + "</p>")
        elif k == "quote":
            txt = " ".join(re.sub(r"^\s*>\s?", "", x) for x in b.lines)
            body.append(f"<blockquote>{md_inline(txt)}</blockquote>")
        elif k == "list":
            items = []
            for x in b.lines:
                items.append(md_inline(re.sub(r"^\s{0,3}([-*+]|\d+[.)])\s+", "", x)))
            body.append("<ul>" + "".join(f"<li>{it}</li>" for it in items) + "</ul>")
        elif k == "table":
            rows = [r.strip().strip("|").split("|") for r in b.lines]
            head = rows[0] if rows else []
            trs = []
            for r in rows[2:] if len(rows) > 1 and re.match(r"^[\s:|-]+$", "".join(rows[1])) else rows[1:]:
                trs.append("<tr>" + "".join(f"<td>{md_inline(c.strip())}</td>" for c in r) + "</tr>")
            body.append("<table><thead><tr>" + "".join(
                f"<th>{md_inline(c.strip())}</th>" for c in head) + "</tr></thead><tbody>" + "".join(trs) + "</tbody></table>")
        elif k == "code":
            body.append("<pre><code>" + html.escape("\n".join(b.lines[1:-1] if len(b.lines) > 1 else b.lines)) + "</code></pre>")
        elif k == "hr":
            body.append("<hr/>")
        elif k == "image":
            body.append('<p class="figure">' + b.lines[0] + "</p>")

        for img in mapping.get(i, []):
            src = rel_or_abs(img, out_dir)
            body.append(f'<figure><img src="{html.escape(src)}" alt=""/></figure>')

    t = html.escape(title or "图文预览")
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{t}</title>
<style>
  :root {{ color-scheme: light; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; background:#f6f5f3; color:#22201e;
         font: 17px/1.95 "PingFang SC","Hiragino Sans GB","Noto Sans SC",system-ui,sans-serif; }}
  .page {{ max-width: 720px; margin: 0 auto; padding: 56px 28px 96px; background:#fff;
           box-shadow: 0 0 0 1px #ebe8e4; }}
  h1 {{ font-size: 27px; line-height:1.5; margin: 0 0 34px; letter-spacing:.4px; }}
  h2 {{ font-size: 21px; margin: 38px 0 16px; }}
  h3 {{ font-size: 18px; margin: 30px 0 12px; }}
  p {{ margin: 0 0 24px; text-indent: 0; }}
  figure {{ margin: 34px 0; }}
  figure img {{ display:block; margin:0 auto; max-width:100%; max-height: min(72vh, 720px);
                width:auto; border-radius: 6px; }}
  blockquote {{ margin: 26px 0; padding: 2px 0 2px 18px; border-left: 3px solid #ccc7c0; color:#55504b; }}
  pre {{ background:#f3f1ee; padding:16px; border-radius:8px; overflow:auto; font-size:14px; }}
  code {{ background:#f3f1ee; padding:1px 5px; border-radius:4px; font-size:.92em; }}
  hr {{ border:0; border-top:1px solid #e8e5e1; margin:40px 0; }}
  table {{ width:100%; border-collapse: collapse; margin: 24px 0; font-size:15px; }}
  th,td {{ border:1px solid #e5e2de; padding:8px 10px; text-align:left; }}
  a {{ color:#8a6b47; }}
  @media (max-width: 640px) {{ body {{ font-size:16px; }} .page {{ padding: 28px 18px 60px; box-shadow:none; }} }}
</style></head>
<body><div class="page">
{chr(10).join(body)}
</div></body></html>
"""


# ---------------------------------------------------------------- CLI


def expand_images(items: list[str]) -> list[str]:
    out: list[str] = []
    for it in items:
        if re.match(r"^https?://", it):
            out.append(it)
        elif any(ch in it for ch in "*?["):
            out.extend(sorted(glob.glob(it)))
        else:
            out.append(it)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="img2md",
        description="把图片按段落均匀插入 Markdown 正文，生成图文混排稿（不做语义匹配，纯按文本体量布点）。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
示例：
  python3 img2md.py input/001.md -i input/images/*.jpg -o output/001_out.md --copy-images
  python3 img2md.py input/001.md -i a.jpg b.jpg -o out.md --min-gap 2 --html out.html
  python3 img2md.py input/001.md -i img/*.jpg -o out.md --alt 厦门街拍
  python3 img2md.py content.txt -i images/*.jpg -o out.md --title "文章标题"
""",
    )
    ap.add_argument("text", help="正文文件（.md / .txt；.txt 中一行即一段）")
    ap.add_argument("-i", "--images", nargs="+", required=True, help="图片路径或 glob，按给定顺序插入")
    ap.add_argument("-o", "--out", required=True, help="输出 markdown 路径")
    ap.add_argument("--title", default="", help="文章标题；提供后在 Markdown 顶部插入一级标题，并作为 HTML 预览标题")
    ap.add_argument("--html", help="同时输出一个 HTML 预览")
    ap.add_argument("--copy-images", action="store_true",
                    help="把图片复制到输出目录下的 images/，引用改相对路径")
    ap.add_argument("--min-gap", type=int, default=1, help="相邻两张图之间至少隔几个段落，默认 1")
    ap.add_argument("--alt", default="配图", help="图片 alt 文本前缀，默认「配图」")
    ap.add_argument("--stdout", action="store_true", help="同时打印到终端")
    args = ap.parse_args(argv)

    if not os.path.isfile(args.text):
        print(f"[x] 找不到正文文件: {args.text}", file=sys.stderr)
        return 2

    images = expand_images(args.images)
    missing = [p for p in images if not re.match(r"^https?://", p) and not os.path.isfile(p)]
    if missing:
        print("[x] 以下图片不存在:\n  " + "\n  ".join(missing), file=sys.stderr)
        return 2
    if not images:
        print("[x] 没有可用图片", file=sys.stderr)
        return 2

    with open(args.text, encoding="utf-8") as f:
        md = f.read()

    out_dir = os.path.dirname(os.path.abspath(args.out))
    os.makedirs(out_dir, exist_ok=True)

    if args.copy_images:
        img_dir = os.path.join(out_dir, "images")
        os.makedirs(img_dir, exist_ok=True)
        copied = []
        for p in images:
            if re.match(r"^https?://", p):
                copied.append(p)
                continue
            dst = os.path.join(img_dir, os.path.basename(p))
            if os.path.abspath(p) != os.path.abspath(dst):
                shutil.copy2(p, dst)
            copied.append(dst)
        images = copied

    # .txt（以及无后缀的纯文本）一行即一段；Markdown 才按连续行合并段落
    ext = os.path.splitext(args.text)[1].lower()
    blocks = parse_blocks(md, per_line_paragraphs=ext not in (".md", ".markdown"))
    mapping = assign_images(blocks, images, args.min_gap)
    derived_title = next((re.sub(r"^#+\s*", "", b.lines[0]) for b in blocks if b.kind == "heading"), "")
    doc_title = re.sub(r"\s+", " ", args.title).strip()
    title = doc_title or derived_title

    result = build_markdown(blocks, mapping, out_dir, alt_prefix=args.alt, title=doc_title)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(result)

    if args.html:
        with open(args.html, "w", encoding="utf-8") as f:
            f.write(build_html(blocks, mapping, os.path.dirname(os.path.abspath(args.html)), title))

    if args.stdout:
        print(result)

    # 摘要
    content_blocks = sum(1 for b in blocks if b.kind in CONTENT_KINDS)
    print(f"✓ 输出 {args.out}")
    print(f"  正文块 {content_blocks} 个 / 图片 {len(images)} 张 / 实心插入点 {len(mapping)} 处")
    for i in sorted(mapping):
        head = re.sub(r"\s+", " ", blocks[i].text)[:24]
        print(f"  · 第 {i+1} 块（{blocks[i].kind}）后插入 {len(mapping[i])} 张  ← “{head}…”")
    if args.html:
        print(f"  预览 {args.html}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
