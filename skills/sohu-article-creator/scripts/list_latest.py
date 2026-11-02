#!/usr/bin/env python3
"""按作者列表抓取每个作者最新 N 篇文章，列出编号与状态，并写列表快照。

- 只抓作者主页，不抓正文；状态来自 state.db：
  [已采用] state=1；[已抓取] 有正文且 content.txt 存在；[抓取失败] 有错误；否则 [新]。
- 编号在一次输出内全局连续，按作者分组。
- 快照默认写到 <root>/.state/latest_list.json，供 select_articles.py 使用。
- 绝不向 articles 表插入列表占位行。

用法：
    python3 list_latest.py
    python3 list_latest.py -n 5 --root workspaces
    python3 list_latest.py --json

退出码：0=至少列出一篇；1=所有作者失败或无文章；2=参数/文件错误；3=没有作者配置。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup

import state_store
from author_store import load_authors
from crawl_author import extract_articles, extract_author_name
from netease_common import (
    DEFAULT_DELAY,
    DEFAULT_ROOT,
    DEFAULT_TIMEOUT,
    get_html,
    log,
    parse_article_id,
)

STATUS_LABELS = {
    "new": "[新]",
    "crawled": "[已抓取]",
    "adopted": "[已采用]",
    "failed": "[抓取失败]",
}


def article_tags(item: dict) -> str:
    tags: list[str] = []
    if item["adopted"]:
        tags.append("[已采用]")
    if item["crawled"]:
        tags.append("[已抓取]")
    if item["status"] == "failed":
        tags.append("[抓取失败]")
    if not tags:
        tags.append("[新]")
    return "".join(tags)


def fetch_author(url: str, *, timeout: float, retries: int) -> tuple[str, str, list[dict]]:
    """返回 (author_name, html, articles)；失败抛异常。"""
    last_error: Exception | None = None
    for attempt in range(max(0, retries) + 1):
        try:
            html = get_html(url, timeout=timeout)
            soup = BeautifulSoup(html, "html.parser")
            name = extract_author_name(soup)
            articles = extract_articles(html)
            return name, html, articles
        except Exception as exc:  # noqa: BLE001 - 单个作者失败不应中断整批
            last_error = exc
            if attempt < retries:
                log(f"  [重试 {attempt + 1}/{retries}] {url} -> {exc}")
                time.sleep(1.5)
    raise RuntimeError(str(last_error)) from last_error


def build_snapshot(args, authors_path, authors, warnings, conn, root: Path) -> dict:
    author_entries: list[dict] = []
    all_articles: list[dict] = []
    index = 0
    failures = 0

    for position, author in enumerate(authors):
        if position and args.delay > 0:
            time.sleep(args.delay)
        url = author["url"]
        author_id = author["author_id"]
        log(f"\n[{position + 1}/{len(authors)}] 作者：{author_id}  {url}")

        try:
            author_name, _, articles = fetch_author(
                url, timeout=args.timeout, retries=args.retries)
        except Exception as exc:  # noqa: BLE001
            failures += 1
            log(f"  [作者抓取失败] {exc}")
            author_entries.append({
                "author_url": url,
                "author_name": "",
                "author_id": author_id,
                "status": "error",
                "article_count": 0,
                "error": str(exc),
            })
            continue

        if args.limit and args.limit > 0:
            articles = articles[: args.limit]
        author_name = author_name or author_id

        rows: list[dict] = []
        for article in articles:
            index += 1
            article_id = parse_article_id(article["url"])
            row = state_store.get_article(conn, article_id)
            cached = state_store.is_crawled(conn, article_id, root=root) if row else False
            adopted = bool(row and row["state"] == 1)
            error = (row["error"] if row and row["error"] else "")
            if adopted:
                status = "adopted"
            elif cached:
                status = "crawled"
            elif error:
                status = "failed"
            else:
                status = "new"

            title = (row["title"] if row and row["title"] else "") or article["title"]
            publish_time = ((row["publish_time"] if row else "")
                            or article.get("publish_time") or "")
            item = {
                "index": index,
                "article_id": article_id,
                "title": title,
                "url": article["url"],
                "author_name": author_name,
                "author_id": author_id,
                "publish_time": publish_time,
                "status": status,
                "crawled": cached,
                "adopted": adopted,
                "dir_path": row["dir_path"] if row else "",
                "content_len": int(row["content_len"] or 0) if row else 0,
                "image_count": int(row["image_count"] or 0) if row else 0,
                "error": error,
            }
            rows.append(item)
            all_articles.append(item)

        author_entries.append({
            "author_url": url,
            "author_name": author_name,
            "author_id": author_id,
            "status": "ok" if rows else "empty",
            "article_count": len(rows),
            "error": "",
        })
        log(f"  -> {len(rows)} 篇")

    snapshot = {
        "schema_version": 1,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "authors_file": str(authors_path) if authors_path else None,
        "max_per_author": args.limit,
        "count": len(all_articles),
        "authors": author_entries,
        "articles": all_articles,
        "warnings": warnings,
        "failed_authors": failures,
    }
    return snapshot


def print_human(snapshot: dict, *, snapshot_path: Path) -> None:
    print(f"作者文件：{snapshot['authors_file'] or '(未配置)'}")
    print(f"共 {len(snapshot['authors'])} 位作者，列出 {snapshot['count']} 篇"
          f"（每位最新 {snapshot['max_per_author']} 篇）")
    for warning in snapshot.get("warnings") or []:
        print(f"[提示] {warning}", file=sys.stderr)

    for author in snapshot["authors"]:
        print()
        name = author["author_name"] or author["author_id"]
        print(f"作者：{name}（{author['author_id']}）")
        if author["status"] == "error":
            print(f"  [作者抓取失败] {author['error']}")
            continue
        items = [item for item in snapshot["articles"]
                 if item["author_id"] == author["author_id"]]
        if not items:
            print("  （没有文章）")
            continue
        for item in items:
            print(f"  {item['index']}. {article_tags(item)} {item['title']}"
                  f"  ({item['publish_time'] or '时间未知'})")

    print("\n标记说明：[新] 未抓取；[已抓取] 正文已缓存；"
          "[已采用] 已导出 docx（同时显示 [已抓取]）；"
          "[抓取失败] 可重新选择重试。")
    print(f"列表快照：{snapshot_path}")


def write_snapshot(snapshot: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    tmp.replace(path)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="按作者列表抓取每个作者最新 N 篇，并标记抓取/采用状态",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("-n", "--limit", type=int, default=5, metavar="N",
                   help="每位作者最多保留多少篇（默认：%(default)s；0=全部）")
    p.add_argument("--root", default=str(DEFAULT_ROOT), metavar="DIR",
                   help="数据根目录（默认：%(default)s）")
    p.add_argument("--authors-file", metavar="PATH",
                   help="作者文件（默认：当前目录 authors.txt / authros.txt）")
    p.add_argument("--snapshot", metavar="PATH",
                   help="列表快照路径（默认：<root>/.state/latest_list.json）")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, metavar="SEC",
                   help="请求超时秒数（默认：%(default)s）")
    p.add_argument("--delay", type=float, default=DEFAULT_DELAY, metavar="SEC",
                   help="作者之间的间隔秒数（默认：%(default)s）")
    p.add_argument("--retries", type=int, default=1, metavar="N",
                   help="单个作者请求失败后的重试次数（默认：%(default)s）")
    p.add_argument("--json", action="store_true", help="输出快照 JSON 到 stdout")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.root).expanduser()

    authors_path, authors, invalid, warnings = load_authors(args.authors_file)
    for item in invalid:
        log(f"[警告] 第 {item['line']} 行不是有效作者 URL：{item['raw']}")

    if not authors:
        if authors_path and authors_path.is_file():
            log(f"作者文件 {authors_path} 中没有有效作者。")
            return 2 if invalid else 3
        log("没有作者列表。请先提供网易号作者主页 URL。")
        return 3

    conn = state_store.connect(root)
    snapshot = build_snapshot(args, authors_path, authors, warnings, conn, root)

    snapshot_path = (Path(args.snapshot).expanduser() if args.snapshot
                     else root / ".state" / "latest_list.json")
    try:
        write_snapshot(snapshot, snapshot_path)
    except OSError as exc:
        log(f"[错误] 写列表快照失败：{exc}")
        return 2

    if args.json:
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
    else:
        print_human(snapshot, snapshot_path=snapshot_path)

    if not snapshot["articles"]:
        log("没有抓到任何文章。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
