"""网易号抓取脚本的公共工具：请求、日志、JSON 读写、图片下载、路径命名。

被 crawl_author.py（抓作者主页）、crawl_article.py（抓文章正文）和
state_store.py（状态库）共用。
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlsplit

import requests

# ================= 公共配置 =================
# 默认输出根目录：<当前工作目录>/workspaces
# 注意：不能用脚本所在目录 —— skill 的脚本位于 <项目>/skills/.../scripts 下，
# 数据应写到运行命令时的项目目录，而不是 skill 自己的目录。
DEFAULT_ROOT = Path.cwd() / "workspaces"
ARTICLES_DIRNAME = "articles"
STATE_DIRNAME = ".state"
STATE_DB_NAME = "state.db"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://www.163.com/",
}

DEFAULT_TIMEOUT = 15
DEFAULT_DELAY = 1.5
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
# ===========================================


def log(*args: Any, **kwargs: Any) -> None:
    """日志一律写 stderr，保证 stdout 可以安全地管道给下一个脚本。"""
    kwargs.setdefault("file", sys.stderr)
    print(*args, **kwargs)


def get_html(url: str, *, timeout: float = DEFAULT_TIMEOUT,
             headers: dict | None = None, verbose: bool = True) -> str:
    """请求 URL，强制 UTF-8 解码（网易号页面是 UTF-8，避免 requests 误判）。"""
    try:
        resp = requests.get(url, headers=headers or HEADERS, timeout=timeout)
    except requests.RequestException as e:
        raise RuntimeError(f"请求失败：{url} -> {e}") from e

    if verbose:
        log(f"[请求] {url} -> 状态码 {resp.status_code}, 内容长度 {len(resp.content)}")
    resp.raise_for_status()
    return resp.content.decode("utf-8", errors="ignore")


# ------------------------- JSON 读写 -------------------------

def read_text(path: str | Path) -> str:
    """读取文本文件；path 为 '-' 时从 stdin 读取。"""
    if str(path) == "-":
        return sys.stdin.read()
    return Path(path).read_text(encoding="utf-8")


def load_json(path: str | Path, default: Any = None) -> Any:
    """读取 JSON；文件不存在或解析失败时返回 default。path 为 '-' 时读 stdin。"""
    try:
        return json.loads(read_text(path))
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError) as e:
        log(f"[警告] 读取 {path} 失败：{e}")
        return default


def dump_json(obj: Any, path: str | Path) -> None:
    """写入 JSON（UTF-8、不转义中文）；path 为 '-' 时写到 stdout。"""
    text = json.dumps(obj, ensure_ascii=False, indent=2)
    if str(path) == "-":
        print(text)
        return
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")


# ------------------------- 文章列表归一化 -------------------------

def extract_article_list(raw: Any) -> list[dict]:
    """
    把各种来源的输入统一成 [{"title": str, "url": str}, ...]。

    支持：
      - crawl_author.py 的输出：{"articles": [...], ...}
      - 直接的列表：[{"title", "url"}] 或 ["url", ...]
      - 单个 dict：{"url": ...}
    """
    items: Any
    if isinstance(raw, dict):
        items = raw.get("articles") or raw.get("article_list") or raw.get("items")
        if items is None:
            items = [raw] if raw.get("url") else []
    elif isinstance(raw, list):
        items = raw
    else:
        return []

    result: list[dict] = []
    seen: set[str] = set()
    for item in items:
        if isinstance(item, str):
            url, title = item, ""
        elif isinstance(item, dict):
            url = str(item.get("url") or item.get("link") or "").strip()
            title = str(item.get("title") or "").strip()
        else:
            continue
        if not url:
            continue
        url = url.split("?")[0].split("#")[0]
        if url in seen:
            continue
        seen.add(url)
        entry = {"title": title, "url": url}
        if isinstance(item, dict):
            for key in ("author_name", "author_id"):
                if item.get(key):
                    entry[key] = str(item[key])
        result.append(entry)
    return result


# ------------------------- 路径 / ID 解析 -------------------------

_ILLEGAL_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_ARTICLE_ID_RE = re.compile(r"/dy/article/([A-Za-z0-9]+)\.html")
_AUTHOR_ID_RE = re.compile(r"/dy/media/([A-Za-z0-9]+)\.html")


def sanitize_component(text: str, *, limit: int = 80, fallback: str = "untitled") -> str:
    """把任意文本清洗成可作为文件/目录名的一段（保留中文）。"""
    text = _ILLEGAL_FILENAME_CHARS.sub("_", str(text or ""))
    text = re.sub(r"\s+", " ", text).strip().strip(".")
    if not text:
        text = fallback
    if len(text) > limit:
        text = text[:limit].rstrip()
    return text or fallback


def parse_article_id(url: str) -> str:
    """从文章 URL 提取文章 ID，如 .../dy/article/LAJJ61890556832G.html -> LAJJ61890556832G。"""
    m = _ARTICLE_ID_RE.search(url)
    if m:
        return m.group(1)
    # 兜底：用 URL 的 netloc+path 哈希，保证唯一且稳定
    parts = urlsplit(url)
    digest = hashlib.md5(f"{parts.netloc}{parts.path}".encode()).hexdigest()[:12]
    return f"url_{digest}"


def parse_author_id(author_url: str) -> str:
    """从作者主页 URL 提取作者 ID，如 .../dy/media/T1715569280443.html -> T1715569280443。"""
    m = _AUTHOR_ID_RE.search(author_url or "")
    return m.group(1) if m else ""


# ------------------------- 图片下载 -------------------------

def guess_image_ext(img_url: str, content_type: str) -> str:
    ext = Path(urlparse(img_url).path).suffix.lower()
    if ext in IMAGE_EXTS:
        return ext
    ct = (content_type or "").lower()
    if "png" in ct:
        return ".png"
    if "gif" in ct:
        return ".gif"
    if "webp" in ct:
        return ".webp"
    return ".jpg"


def download_image(img_url: str, dest_stem: Path, *, timeout: float = 30) -> Path | None:
    """
    把图片下载到 dest_stem + 自动判定的扩展名（如 .../images/001.jpg）。

    已存在则直接复用，返回最终文件路径；失败返回 None。
    """
    dest_stem = Path(dest_stem)
    try:
        dest_stem.parent.mkdir(parents=True, exist_ok=True)

        # 已下载过（扩展名可能不同）则复用
        for ext in sorted(IMAGE_EXTS):
            candidate = dest_stem.with_suffix(ext)
            if candidate.exists() and candidate.stat().st_size > 0:
                return candidate

        resp = requests.get(img_url, headers=HEADERS, timeout=timeout, stream=True)
        resp.raise_for_status()

        ext = guess_image_ext(img_url, resp.headers.get("Content-Type", ""))
        filepath = dest_stem.with_suffix(ext)
        with open(filepath, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        if filepath.stat().st_size == 0:
            filepath.unlink(missing_ok=True)
            log(f"    [图片为空] {img_url}")
            return None
        return filepath
    except Exception as e:  # noqa: BLE001 - 单张图片失败不应中断整批
        log(f"    [图片下载失败] {img_url} -> {e}")
        return None
