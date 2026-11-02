#!/usr/bin/env python3
"""抓取网易号作者主页，输出文章列表（JSON）。

只负责“作者主页 -> 文章链接列表”，正文解析见 crawl_article.py。

用法示例：
    python crawl_author.py "https://www.163.com/dy/media/T1715569280443.html"
    python crawl_author.py "https://www.163.com/dy/media/xxx.html" -o list.json   # 最新 5 篇
    python crawl_author.py "https://..." -n 0 -o - | python crawl_article.py -i - # 全量

输出的 JSON 结构：
    {
      "author_url": "...",
      "author_name": "...",
      "author_id": "...",
      "fetched_at": "2025-01-01T00:00:00+08:00",
      "count": 20,
      "articles": [{"title": "...", "url": "...", "publish_time": "2025-01-01 08:00"}]
    }
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup

from netease_common import (
    DEFAULT_TIMEOUT,
    dump_json,
    extract_article_list,
    get_html,
    load_json,
    log,
    parse_author_id,
)

ARTICLE_PATH_MARK = "/dy/article/"
VIDEO_PATH_MARK = "/v/video/"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="抓取网易号作者主页的文章列表",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("author_url", nargs="?", help="作者主页 URL，如 https://www.163.com/dy/media/xxx.html")
    p.add_argument("--author-url", dest="author_url_opt", metavar="URL",
                   help="同上，用选项方式传入（与位置参数二选一）")
    p.add_argument("-o", "--output", default="netease_article_list.json",
                   help="输出 JSON 文件路径，'-' 表示输出到 stdout（默认：%(default)s）")
    p.add_argument("-n", "--max-articles", type=int, default=5, metavar="N",
                   help="最多保留多少篇，按主页顺序即最新在前；0 表示全部（默认：%(default)s）")
    p.add_argument("--include-video", action="store_true",
                   help="同时收集视频链接（/v/video/），默认只收图文文章")
    p.add_argument("--merge", action="store_true",
                   help="与输出文件中已有的文章列表合并去重，而不是覆盖")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, metavar="SEC",
                   help="请求超时秒数（默认：%(default)s）")
    p.add_argument("--debug-dir", metavar="DIR",
                   help="抓取或解析失败时，把页面 HTML 保存到该目录便于排查")
    return p


def extract_author_name(soup: BeautifulSoup) -> str:
    """尽量从页面里取作者昵称（网易号主页的 <title> 就是昵称）。"""
    if soup.title:
        name = soup.title.get_text(strip=True)
        name = name.rsplit("-", 1)[0] if "-" in name else name
        name = re.sub(r"\s*(网易号|网易)\s*$", "", name).strip()
        if name:
            return name
    return ""


def extract_publish_time(a_tag) -> str:
    """从标题链接所在的文章条目里找发布时间（作者页常见 span.time）。

    只在「仅包含本篇文章标题」的父容器里找，避免爬到整个列表容器后
    误拿第一篇的时间。找不到返回空串。
    """
    for parent in a_tag.parents:
        if parent.name == "body":
            break
        if len(parent.find_all("a", class_="title")) > 1:
            break
        time_tag = parent.find("span", class_="time")
        if time_tag:
            return time_tag.get_text(strip=True)
    return ""


def extract_articles(html: str, *, include_video: bool = False) -> list[dict]:
    """解析主页 HTML，提取文章 {title, url, publish_time} 列表。"""
    soup = BeautifulSoup(html, "html.parser")
    articles: list[dict] = []
    seen: set[str] = set()
    video_count = 0

    for a_tag in soup.find_all("a", class_="title", href=True):
        href = a_tag["href"]
        title = a_tag.get_text(strip=True)
        if not title:
            continue

        is_article = ARTICLE_PATH_MARK in href
        is_video = VIDEO_PATH_MARK in href
        if is_video and not include_video:
            video_count += 1
            continue
        if not (is_article or (include_video and is_video)):
            continue

        full_url = href.split("?")[0]
        if full_url in seen:
            continue
        seen.add(full_url)
        articles.append({"title": title, "url": full_url,
                         "publish_time": extract_publish_time(a_tag)})

    if video_count:
        log(f"[信息] 已跳过 {video_count} 个视频链接（可用 --include-video 收集）")
    return articles


def save_debug(debug_dir: str | None, name: str, content: str) -> None:
    if not debug_dir:
        return
    d = Path(debug_dir)
    d.mkdir(parents=True, exist_ok=True)
    path = d / name
    path.write_text(content, encoding="utf-8")
    log(f"[调试] 页面已保存：{path}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    author_url = (args.author_url or args.author_url_opt or "").strip()
    if not author_url:
        log("错误：请提供作者主页 URL（位置参数或 --author-url）。")
        return 2

    log(f"作者主页：{author_url}")
    log("正在提取文章列表...")

    try:
        html = get_html(author_url, timeout=args.timeout)
    except Exception as e:  # noqa: BLE001
        log(f"[错误] 请求主页失败：{e}")
        return 1

    if ARTICLE_PATH_MARK not in html and not args.include_video:
        log(f"[警告] HTML 中未找到 '{ARTICLE_PATH_MARK}'，页面结构可能已变化")
        save_debug(args.debug_dir or ".", "debug_author_page.html", html)

    soup = BeautifulSoup(html, "html.parser")
    author_name = extract_author_name(soup)
    articles = extract_articles(html, include_video=args.include_video)
    log(f"[信息] 提取到 {len(articles)} 篇文章")

    if not articles:
        save_debug(args.debug_dir or ".", "debug_author_page.html", html)
        log("未提取到任何文章链接，请检查 URL 是否正确或页面结构是否变化。")
        return 1

    if args.merge and str(args.output) != "-":
        old = load_json(args.output, default=None)
        if old is not None:
            # 本次抓到的是最新一批，放前面，保证“最新 N 篇”仍然成立
            merged: dict[str, dict] = {}
            for item in articles + extract_article_list(old):
                merged.setdefault(item["url"], item)
            articles = list(merged.values())
            log(f"[信息] 合并已有列表后共 {len(articles)} 篇")

    if args.max_articles and args.max_articles > 0:
        articles = articles[: args.max_articles]

    payload = {
        "author_url": author_url,
        "author_name": author_name,
        "author_id": parse_author_id(author_url),
        "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "count": len(articles),
        "articles": articles,
    }
    dump_json(payload, args.output)

    if str(args.output) != "-":
        log(f"完成。{len(articles)} 篇文章已保存到 {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
