#!/usr/bin/env python3
"""抓取网易号文章正文与图片，按日期归档到本地文章库，并写入状态库。

输出目录结构（root 默认 <当前工作目录>/workspaces，即运行命令的项目目录）：
    workspaces/
    ├── .state/state.db                      # 状态库（sqlite3）
    └── articles/
        └── <作者名字>_<作者id>/
            └── <yyyyMMdd>_<标题>_<文章id>/
                ├── content.txt              # 正文纯文本
                └── images/001.jpg, 002.jpg ...   # 图片按顺序编号

输入可以是：
  - 命令行直接给的 URL；
  - crawl_author.py 生成的 JSON 文件（或直接管道 stdin）；
  - 一行一个 URL 的文本文件。

用法示例：
    # 单篇
    python crawl_article.py "https://www.163.com/dy/article/XXXX.html"

    # 从作者主页列表批量抓
    python crawl_article.py -i netease_article_list.json

    # 全流程管道：主页 -> 列表 -> 正文
    python crawl_author.py "https://www.163.com/dy/media/xxx.html" -o - \
        | python crawl_article.py -i -

    # 增量：跳过状态库里已成功抓取的文章（默认仍只处理最新 5 篇，-n 0 为全量）
    python crawl_article.py -i netease_article_list.json --skip-existing
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

from bs4 import BeautifulSoup

import state_store
from netease_common import (
    ARTICLES_DIRNAME,
    DEFAULT_DELAY,
    DEFAULT_ROOT,
    DEFAULT_TIMEOUT,
    download_image,
    extract_article_list,
    get_html,
    log,
    parse_article_id,
    parse_author_id,
    read_text,
    sanitize_component,
)

BODY_CLASSES = ["post_body", "content", "article-content", "main-content"]
AD_CLASS_PATTERN = re.compile(r"gg|ad|advertisement|recommend|related", re.I)
PUBLISH_TIME_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?")
AUTHOR_LINK_RE = re.compile(r"/dy/media/")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="抓取网易号文章正文，归档到 workspaces/articles/ 并写入 .state/state.db",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("urls", nargs="*", help="文章 URL，可传多个")
    p.add_argument("-i", "--input", metavar="FILE",
                   help="文章列表来源：JSON（crawl_author.py 的输出）、URL 列表文本，或 '-' 表示 stdin")
    p.add_argument("--root", default=str(DEFAULT_ROOT), metavar="DIR",
                   help="文章库根目录（默认：%(default)s）")
    p.add_argument("--no-images", action="store_true", help="只保存正文，不下载图片")
    p.add_argument("-n", "--max-articles", type=int, default=5, metavar="N",
                   help="本次最多处理多少篇，按输入顺序取前 N（默认最新 5 篇）；0 表示全部")
    p.add_argument("--delay", type=float, default=DEFAULT_DELAY, metavar="SEC",
                   help="每篇之间的间隔秒数（默认：%(default)s）")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, metavar="SEC",
                   help="请求超时秒数（默认：%(default)s）")
    p.add_argument("--skip-existing", action="store_true",
                   help="跳过状态库中已成功抓取的文章")
    p.add_argument("--author-name", metavar="NAME",
                   help="作者昵称（页面解析不到时使用，单独抓 URL 时可用）")
    p.add_argument("--author-id", metavar="ID",
                   help="作者 ID（页面解析不到时使用，单独抓 URL 时可用）")
    p.add_argument("--debug-dir", metavar="DIR",
                   help="解析失败时把页面 HTML 保存到该目录便于排查")
    return p


# ------------------------- 页面解析 -------------------------

def normalize_publish_time(raw: str) -> str:
    """把各种时间写法统一成 'YYYY-MM-DD HH:MM:SS'；无法识别返回空串。"""
    m = PUBLISH_TIME_RE.search(raw or "")
    if not m:
        return ""
    year, month, day, hour, minute, second = m.groups()
    return f"{year}-{month}-{day} {hour}:{minute}:{second or '00'}"


def extract_publish_time(soup: BeautifulSoup) -> str:
    # 1) <meta property="article:published_time" content="2026-10-31T20:27:15+08:00">
    meta = soup.find("meta", attrs={"property": "article:published_time"})
    if meta and meta.get("content"):
        value = normalize_publish_time(meta["content"])
        if value:
            return value

    # 2) <html data-publishtime="2026-10-31 20:27:15">
    html_tag = soup.find("html")
    if html_tag and html_tag.get("data-publishtime"):
        value = normalize_publish_time(html_tag["data-publishtime"])
        if value:
            return value

    # 3) 正文上方 post_info 里的时间文本
    info = soup.find("div", class_="post_info")
    if info:
        return normalize_publish_time(info.get_text(" ", strip=True))
    return ""


def extract_author(soup: BeautifulSoup) -> tuple[str, str]:
    """返回 (作者昵称, 作者主页 URL)，主要从 post_info 里的“来源”链接取。"""
    info = soup.find("div", class_="post_info")
    if info:
        for a_tag in info.find_all("a", href=True):
            if AUTHOR_LINK_RE.search(a_tag["href"]):
                name = a_tag.get_text(strip=True)
                url = a_tag["href"].split("?")[0]
                return name, url
    return "", ""


def extract_body(soup: BeautifulSoup):
    for cls in BODY_CLASSES:
        body = soup.find("div", class_=cls)
        if body:
            return body
    return None


def parse_article_html(html: str, article_url: str) -> dict:
    """从已下载的 HTML 解析文章：标题、时间、作者、正文、图片。"""
    soup = BeautifulSoup(html, "html.parser")

    # ---------- 标题 ----------
    title = ""
    h1 = soup.find("h1", class_="post_title") or soup.h1
    if h1:
        title = h1.get_text(strip=True)
    elif soup.title:
        title = soup.title.get_text(strip=True)
    title = re.sub(r"\s*[\|_\-–—]\s*网易.*$", "", title).strip()

    publish_time = extract_publish_time(soup)
    author_name, author_url = extract_author(soup)

    detail = {
        "url": article_url,
        "title": title,
        "publish_time": publish_time,
        "publish_date": publish_time[:10].replace("-", "") if publish_time else "",
        "author_name": author_name,
        "author_url": author_url,
        "author_id": parse_author_id(author_url),
        "content_html": "",
        "content_text": "",
        "images": [],
        "error": None,
    }

    # ---------- 正文 ----------
    body = extract_body(soup)
    if not body:
        detail["error"] = "未找到正文容器 div.post_body"
        return detail

    for unwanted in body.find_all(["script", "style", "iframe", "ins", "noscript"]):
        unwanted.decompose()
    for tag in body.find_all(class_=AD_CLASS_PATTERN):
        tag.decompose()

    detail["content_html"] = str(body)
    detail["content_text"] = body.get_text(separator="\n", strip=True)

    # ---------- 图片 ----------
    images: list[str] = []
    for img in body.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-original")
        if not src:
            continue
        src = src.strip()
        if src.startswith("//"):
            src = "https:" + src
        elif src.startswith("/"):
            src = "https://www.163.com" + src
        elif not src.startswith(("http://", "https://")):
            src = "https://www.163.com/" + src.lstrip("./")
        if any(k in src.lower() for k in ["icon", "logo", "pixel", "1x1", "blank.gif"]):
            continue
        if src not in images:
            images.append(src)
    detail["images"] = images
    return detail


def save_debug_page(debug_dir: str | None, article_url: str, html: str) -> None:
    if not debug_dir:
        return
    d = Path(debug_dir)
    d.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^0-9A-Za-z_-]", "_", article_url)[-80:] or "page"
    path = d / f"{name}.html"
    path.write_text(html, encoding="utf-8")
    log(f"  [调试] 页面已保存：{path}")


# ------------------------- 输入解析 -------------------------

def collect_articles(args) -> list[dict]:
    """汇总命令行 URL 与 --input 里的条目，去重（保持顺序），并带上作者信息。"""
    raw_items: list[dict] = [{"title": "", "url": u} for u in args.urls]
    input_author_name, input_author_id = "", ""

    if args.input:
        try:
            text = read_text(args.input).strip()
        except OSError as e:
            log(f"[错误] 读取 {args.input} 失败：{e}")
            return []
        if not text:
            log(f"[警告] {args.input} 内容为空")
        elif text[0] in "[{":
            try:
                data = json.loads(text)
            except json.JSONDecodeError as e:
                log(f"[错误] {args.input} 不是合法 JSON：{e}")
                return []
            if isinstance(data, dict):
                input_author_name = str(data.get("author_name") or "")
                input_author_id = (str(data.get("author_id") or "")
                                   or parse_author_id(str(data.get("author_url") or "")))
            raw_items += extract_article_list(data)
        else:
            lines = [ln.strip() for ln in text.splitlines()]
            raw_items += [{"title": "", "url": ln} for ln in lines
                          if ln and not ln.startswith("#")]

    author_name = args.author_name or input_author_name
    author_id = args.author_id or input_author_id

    merged: dict[str, dict] = {}
    for item in extract_article_list(raw_items):
        item.setdefault("author_name", "")
        item.setdefault("author_id", "")
        item["author_name"] = item["author_name"] or author_name
        item["author_id"] = item["author_id"] or author_id
        merged.setdefault(item["url"], item)
    return list(merged.values())


# ------------------------- 落盘 -------------------------

def article_dir_for(root: Path, *, author_name: str, author_id: str,
                    publish_date: str, title: str, article_id: str) -> Path:
    author_part = sanitize_component(f"{author_name or 'unknown'}_{author_id or 'unknown'}",
                                     limit=120, fallback="unknown")
    name = "_".join([
        publish_date or "unknown",
        sanitize_component(title, limit=60, fallback="untitled"),
        article_id,
    ])
    return root / ARTICLES_DIRNAME / author_part / sanitize_component(name, limit=180)


def write_content(article_dir: Path, text: str) -> Path:
    path = article_dir / "content.txt"
    path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    return path


def fetch_images(images: list[str], article_dir: Path, *, timeout: float) -> list[Path]:
    """按 001、002… 顺序下载图片，返回成功下载的本地文件路径。"""
    images_dir = article_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    locals_: list[Path] = []
    for index, img_url in enumerate(images, 1):
        path = download_image(img_url, images_dir / f"{index:03d}", timeout=timeout)
        if path:
            locals_.append(path)
    # 清掉上一轮遗留的多余编号文件（例如文章图片变少了）
    for stale in images_dir.glob("*"):
        m = re.match(r"^(\d{3,})\.", stale.name)
        if m and int(m.group(1)) > len(images):
            stale.unlink(missing_ok=True)
    return locals_


# ------------------------- 主流程 -------------------------

def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    articles = collect_articles(args)
    if not articles:
        log("错误：没有可抓取的文章 URL。请用位置参数或 -i/--input 提供。")
        return 2

    root = Path(args.root).expanduser()
    conn = state_store.connect(root)
    log(f"文章库：{root.resolve()}")
    log(f"状态库：{state_store.db_path(root).resolve()}")

    if args.skip_existing:
        before = len(articles)
        articles = [a for a in articles
                    if not state_store.is_crawled(conn, parse_article_id(a["url"]),
                                                  root=root)]
        log(f"[信息] 跳过状态库中已成功抓取 {before - len(articles)} 篇，"
            f"剩余 {len(articles)} 篇")
        if not articles:
            log("没有需要抓取的新文章。")
            return 0

    if args.max_articles and args.max_articles > 0:
        articles = articles[: args.max_articles]

    ok, failed = 0, 0
    for i, item in enumerate(articles, 1):
        url = item["url"]
        article_id = parse_article_id(url)
        log(f"\n[{i}/{len(articles)}] {item['title'] or '(无标题)'}")
        log(f"  URL：{url}")

        try:
            html = get_html(url, timeout=args.timeout)
        except Exception as e:  # noqa: BLE001
            log(f"  [请求失败] {e}")
            state_store.upsert_article(
                conn, article_id=article_id, url=url, title=item["title"],
                author_name=item["author_name"], author_id=item["author_id"],
                error=f"请求失败：{e}",
            )
            failed += 1
            continue

        detail = parse_article_html(html, url)

        # 页面里取不到时，回退到列表 / 命令行提供的作者信息
        detail["title"] = detail["title"] or item["title"]
        detail["author_name"] = detail["author_name"] or item["author_name"]
        detail["author_id"] = detail["author_id"] or item["author_id"]

        if detail["error"]:
            log(f"  [解析失败] {detail['error']}")
            save_debug_page(args.debug_dir, url, html)
            state_store.upsert_article(
                conn, article_id=article_id, url=url, title=detail["title"],
                author_name=detail["author_name"], author_id=detail["author_id"],
                publish_time=detail["publish_time"], publish_date=detail["publish_date"],
                error=detail["error"],
            )
            failed += 1
            continue

        article_dir = article_dir_for(
            root,
            author_name=detail["author_name"], author_id=detail["author_id"],
            publish_date=detail["publish_date"], title=detail["title"],
            article_id=article_id,
        )
        article_dir.mkdir(parents=True, exist_ok=True)

        content_path = write_content(article_dir, detail["content_text"])
        log(f"  正文 {len(detail['content_text'])} 字符 -> {content_path}")

        image_paths: list[Path] = []
        if not args.no_images and detail["images"]:
            image_paths = fetch_images(detail["images"], article_dir,
                                       timeout=max(args.timeout, 30))
            log(f"  图片 {len(image_paths)}/{len(detail['images'])} 张 -> "
                f"{article_dir / 'images'}")

        state_store.upsert_article(
            conn, article_id=article_id, url=url, title=detail["title"],
            author_name=detail["author_name"], author_id=detail["author_id"],
            publish_time=detail["publish_time"], publish_date=detail["publish_date"],
            dir_path=article_dir.relative_to(root).as_posix(),
            content_len=len(detail["content_text"]), image_count=len(image_paths),
            error=None,
        )
        ok += 1
        if i < len(articles):
            time.sleep(args.delay)

    if ok == 0:
        log(f"\n没有成功抓取任何文章（失败 {failed} 篇）。")
        return 1

    log(f"\n完成。成功 {ok} 篇，失败 {failed} 篇。"
        f"目录：{root / ARTICLES_DIRNAME}，状态库：{state_store.db_path(root)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
