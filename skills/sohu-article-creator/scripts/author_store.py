#!/usr/bin/env python3
"""网易号作者列表管理：读取、校验、去重、追加作者主页 URL。

默认在项目根目录（当前工作目录）下查找作者列表，兼容用户常见的
`authros.txt` 拼写；两个文件都不存在时，`add` 创建 `authors.txt`。

用法：
    python3 author_store.py check
    python3 author_store.py check --json
    python3 author_store.py add "https://www.163.com/dy/media/T1668942583212.html"
    python3 author_store.py --authors-file my_authors.txt add URL1 URL2

退出码：
    check: 0=有有效作者；2=文件不可读/只有非法项；3=不存在或为空（需要向用户索取 URL）
    add:   0=成功（含已存在）；2=任一 URL 非法/文件不可写
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

from netease_common import log, parse_author_id

DEFAULT_AUTHORS_NAMES = ("authors.txt", "authros.txt")
_AUTHOR_PATH_RE = re.compile(r"^/dy/media/([A-Za-z0-9]+)\.html$")

HEADER = """# 网易号作者主页列表（每行一个 URL）
# 示例：https://www.163.com/dy/media/T1668942583212.html
# 空行和以 # 开头的行会被忽略；行尾 # 后是注释。
# 也可以用 author_store.py add <URL> 追加。
"""


def normalize_author_url(raw: str) -> str:
    """把用户输入规范成 https://www.163.com/dy/media/T....html。"""
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("URL 为空")
    if "://" not in raw:
        raw = "https://" + raw
    parts = urlsplit(raw)
    host = (parts.hostname or "").lower()
    if host != "163.com" and not host.endswith(".163.com"):
        raise ValueError(f"不是网易号作者主页（域名应为 163.com）：{raw}")
    m = _AUTHOR_PATH_RE.match(parts.path)
    if not m:
        raise ValueError(f"URL 路径应为 /dy/media/T....html：{raw}")
    return f"https://www.163.com/dy/media/{m.group(1)}.html"


def read_authors_file(path: Path) -> tuple[list[dict], list[dict]]:
    """读取作者文件，返回 (有效作者, 非法行)。支持整行/行尾 # 注释。"""
    text = path.read_text(encoding="utf-8-sig")
    authors: list[dict] = []
    invalid: list[dict] = []
    seen: set[str] = set()

    for lineno, line in enumerate(text.splitlines(), 1):
        raw = line.split("#", 1)[0].strip()
        if not raw:
            continue
        try:
            url = normalize_author_url(raw)
        except ValueError as exc:
            invalid.append({"line": lineno, "raw": line.strip(), "error": str(exc)})
            continue
        author_id = parse_author_id(url) or url
        if author_id in seen:
            continue
        seen.add(author_id)
        authors.append({"url": url, "author_id": author_id, "line": lineno})
    return authors, invalid


def candidate_paths(explicit: str | Path | None = None) -> list[Path]:
    if explicit:
        return [Path(explicit).expanduser()]
    return [Path.cwd() / name for name in DEFAULT_AUTHORS_NAMES]


def load_authors(explicit: str | Path | None = None):
    """解析作者文件并读取内容。

    返回 (path, authors, invalid, warnings)。两个默认文件都存在时优先
    `authors.txt`；若它没有有效作者而另一个有，则改用另一个。
    """
    warnings: list[str] = []
    candidates = candidate_paths(explicit)
    existing = [p for p in candidates if p.is_file()]

    if not existing:
        missing = candidates[0] if candidates else None
        return missing, [], [], warnings

    loaded = []
    for path in existing:
        try:
            authors, invalid = read_authors_file(path)
        except OSError as exc:
            warnings.append(f"读取 {path} 失败：{exc}")
            continue
        loaded.append((path, authors, invalid))

    if not loaded:
        return existing[0], [], [], warnings

    chosen = next((item for item in loaded if item[1]), loaded[0])
    path, authors, invalid = chosen

    if len(loaded) > 1:
        names = "、".join(item[0].name for item in loaded)
        if authors:
            warnings.append(f"同时存在 {names}，使用 {path.name}")
        else:
            warnings.append(f"{names} 中都没有有效作者，使用 {path.name}")

    return path, authors, invalid, warnings


def build_payload(path, authors, invalid, warnings, *, needs_input: bool | None = None) -> dict:
    payload = {
        "path": str(path) if path else None,
        "exists": bool(path and path.is_file()),
        "valid_count": len(authors),
        "authors": authors,
        "invalid": invalid,
        "warnings": warnings,
        "needs_input": (not authors) if needs_input is None else needs_input,
    }
    return payload


def print_check_human(payload: dict) -> None:
    if payload["path"]:
        print(f"作者文件：{payload['path']}")
    for warning in payload["warnings"]:
        print(f"[提示] {warning}", file=sys.stderr)
    for item in payload["authors"]:
        print(f"  - {item['author_id']}  {item['url']}")
    for item in payload["invalid"]:
        print(f"  [非法] 第 {item['line']} 行：{item['raw']}  ({item['error']})",
              file=sys.stderr)
    if payload["needs_input"]:
        print("作者列表为空，请提供网易号作者主页 URL（形如 "
              "https://www.163.com/dy/media/T1668942583212.html）。")
    else:
        print(f"共 {payload['valid_count']} 位作者。")


def cmd_check(args) -> int:
    path, authors, invalid, warnings = load_authors(args.authors_file)
    payload = build_payload(path, authors, invalid, warnings)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print_check_human(payload)

    if not path or not path.is_file():
        return 3
    if not authors:
        return 2 if invalid else 3
    return 0


def cmd_add(args) -> int:
    # 先全部校验；任何一条非法都不写文件，避免半成功状态。
    normalized: list[str] = []
    errors: list[dict] = []
    for raw in args.urls:
        try:
            normalized.append(normalize_author_url(raw))
        except ValueError as exc:
            errors.append({"raw": raw, "error": str(exc)})

    if errors:
        payload = {"path": None, "added": [], "skipped_existing": [],
                   "errors": errors, "warnings": []}
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            for item in errors:
                print(f"[非法] {item['raw']}：{item['error']}", file=sys.stderr)
        return 2

    path, authors, invalid, warnings = load_authors(args.authors_file)
    if path is None:
        path = Path.cwd() / DEFAULT_AUTHORS_NAMES[0]

    existing_ids = {item["author_id"] for item in authors}
    added: list[str] = []
    skipped: list[str] = []
    warnings = list(warnings)
    if invalid:
        warnings.append(f"{path.name} 中有 {len(invalid)} 行非法内容，已忽略")

    needs_header = not path.is_file() or path.stat().st_size == 0
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("a", encoding="utf-8") as fh:
            if needs_header:
                fh.write(HEADER)
            for url in normalized:
                author_id = parse_author_id(url) or url
                if author_id in existing_ids:
                    skipped.append(url)
                    continue
                fh.write(url + "\n")
                existing_ids.add(author_id)
                added.append(url)
    except OSError as exc:
        log(f"[错误] 写入 {path} 失败：{exc}")
        return 2

    payload = {
        "path": str(path),
        "added": added,
        "skipped_existing": skipped,
        "errors": [],
        "valid_count": len(existing_ids),
        "warnings": warnings,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for url in added:
            print(f"[已添加] {url}")
        for url in skipped:
            print(f"[已存在] {url}")
        for warning in warnings:
            print(f"[提示] {warning}", file=sys.stderr)
        print(f"作者文件：{path}（共 {len(existing_ids)} 位作者）")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="读取/校验/追加网易号作者主页列表",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--authors-file", metavar="PATH",
                   help="显式指定作者文件（默认：当前目录 authors.txt / authros.txt）")
    sub = p.add_subparsers(dest="command", required=True)

    chk = sub.add_parser("check", help="检查作者文件并列出有效作者")
    chk.add_argument("--json", action="store_true", help="输出 JSON")
    chk.set_defaults(func=cmd_check)

    add = sub.add_parser("add", help="追加作者 URL（自动创建 authors.txt）")
    add.add_argument("urls", nargs="+", help="一个或多个网易号作者主页 URL")
    add.add_argument("--json", action="store_true", help="输出 JSON")
    add.set_defaults(func=cmd_add)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
