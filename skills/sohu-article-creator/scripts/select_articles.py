#!/usr/bin/env python3
"""把用户的选稿编号映射到最近一次列表快照，生成 crawl_article.py 可读的 selection.json。

支持：
    "1,3,5" / "1、3、5" / "1 3 5"
    "1-3" / "1到3"
    "全部" / "all"
    --author "荔子言" "1,3"   # 数字理解为该作者列表内的局部序号

默认跳过已采用文章；明确要重做时加 --force。

用法：
    python3 select_articles.py "1,3,5"
    python3 select_articles.py --author "荔子言" "1-3"
    python3 select_articles.py "全部"

退出码：0=成功（包括全部已采用而无需处理）；1=写文件失败；2=选择/作者参数错误；3=快照缺失或损坏。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from netease_common import DEFAULT_ROOT, log

FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")


def load_snapshot(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"找不到列表快照：{path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"读取列表快照失败：{path} -> {exc}") from exc

    articles = data.get("articles")
    if not isinstance(articles, list) or not articles:
        raise RuntimeError(f"列表快照里没有文章：{path}")
    if not all(isinstance(item, dict) and "index" in item for item in articles):
        raise RuntimeError(f"列表快照格式不正确：{path}")
    return data


def _author_labels(articles: list[dict]) -> list[str]:
    return sorted({f"{item.get('author_name') or item.get('author_id')}"
                   f"（{item.get('author_id')}）" for item in articles})


def filter_by_author(articles: list[dict], keyword: str) -> tuple[list[dict], list[str]]:
    keyword = (keyword or "").strip()
    exact = [item for item in articles
             if keyword in (item.get("author_name", ""), item.get("author_id", ""))]
    if exact:
        if len({item.get("author_id") for item in exact}) == 1:
            return exact, []
        return [], _author_labels(exact)

    partial = [item for item in articles
               if keyword in (item.get("author_name") or "")
               or keyword in (item.get("author_id") or "")]
    if partial:
        if len({item.get("author_id") for item in partial}) == 1:
            return partial, []
        return [], _author_labels(partial)
    return [], _author_labels(articles)


def normalize_selection(text: str) -> tuple[str, list[int]]:
    """返回 (模式, 编号列表)，模式为 all 或 numbers。"""
    raw = (text or "").translate(FULLWIDTH_DIGITS).strip()
    if not raw:
        raise ValueError("选择为空")
    if raw.lower() in ("全部", "all", "所有", "全选"):
        return "all", []
    if re.search(r"(全部|所有|全选|\ball\b)", raw, re.I):
        raise ValueError("「全部」不能和其他编号混用")

    raw = re.sub(r"[第篇号个]", "", raw)
    raw = re.sub(r"[到至]", "-", raw)
    raw = re.sub(r"[，、；;\s]+", ",", raw)
    numbers: list[int] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", token)
        if m:
            start, end = int(m.group(1)), int(m.group(2))
            if start > end:
                raise ValueError(f"区间起点大于终点：{token}")
            if end - start > 500:
                raise ValueError(f"区间过大：{token}")
            numbers.extend(range(start, end + 1))
            continue
        if not re.fullmatch(r"\d+", token):
            raise ValueError(f"无法识别选稿编号：{token}")
        numbers.append(int(token))
    if not numbers:
        raise ValueError("选择为空")
    # 去重且保持顺序
    seen: set[int] = set()
    unique = []
    for number in numbers:
        if number not in seen:
            seen.add(number)
            unique.append(number)
    return "numbers", unique


def resolve_articles(snapshot: dict, mode: str, numbers: list[int],
                     author: str | None) -> list[dict]:
    articles = snapshot["articles"]
    if author:
        available, candidates = filter_by_author(articles, author)
        if not available:
            if candidates:
                raise ValueError("作者不唯一，请写得更具体：" + "、".join(candidates))
            raise ValueError(f"快照里没有作者：{author}")
        # --author 下数字是该作者列表内的局部序号
        local = {i + 1: item for i, item in enumerate(available)}
        if mode == "all":
            return list(available)
        invalid = [n for n in numbers if n not in local]
        if invalid:
            raise ValueError(f"作者「{author}」只有 {len(local)} 篇，编号越界："
                             + ", ".join(map(str, invalid)))
        return [local[n] for n in numbers]

    by_index = {int(item["index"]): item for item in articles}
    if mode == "all":
        return [by_index[key] for key in sorted(by_index)]
    invalid = [n for n in numbers if n not in by_index]
    if invalid:
        valid = f"{min(by_index)}-{max(by_index)}"
        raise ValueError(f"编号越界：{', '.join(map(str, invalid))}（有效范围 {valid}）")
    return [by_index[n] for n in numbers]


def build_selection(snapshot: dict, snapshot_path: Path, args) -> dict:
    mode, numbers = normalize_selection(args.selection)
    requested = resolve_articles(snapshot, mode, numbers, args.author)

    skipped_adopted = []
    selected = []
    for item in requested:
        if item.get("adopted") and not args.force:
            skipped_adopted.append({
                "index": item.get("index"),
                "article_id": item.get("article_id"),
                "title": item.get("title"),
            })
            continue
        selected.append({
            "index": item.get("index"),
            "article_id": item.get("article_id"),
            "title": item.get("title"),
            "url": item.get("url"),
            "author_name": item.get("author_name"),
            "author_id": item.get("author_id"),
            "publish_time": item.get("publish_time"),
        })

    return {
        "schema_version": 1,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_snapshot": str(snapshot_path),
        "selection": args.selection,
        "author_filter": args.author or None,
        "force": bool(args.force),
        "requested_count": len(requested),
        "count": len(selected),
        "skipped_adopted": skipped_adopted,
        "articles": selected,
    }


def write_json_atomic(payload: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    tmp.replace(path)


def print_human(payload: dict, out_path: Path) -> None:
    if payload["skipped_adopted"]:
        print(f"跳过已采用 {len(payload['skipped_adopted'])} 篇：")
        for item in payload["skipped_adopted"]:
            print(f"  [{item['index']}] {item['title']}")
    if not payload["articles"]:
        print("没有需要处理的文章（可能都已采用）。如需重做，请加 --force。")
    else:
        print(f"已选择 {payload['count']} 篇：")
        for item in payload["articles"]:
            print(f"  [{item['index']}] {item['title']}  —— {item['author_name']}"
                  f"（{item['author_id']}）")
    print(f"选稿文件：{out_path}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="把选稿编号映射到最近一次列表快照",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("selection", help='选稿表达式，如 "1,3,5"、"1-3"、"全部"')
    p.add_argument("--root", default=str(DEFAULT_ROOT), metavar="DIR",
                   help="数据根目录（默认：%(default)s）")
    p.add_argument("--snapshot", metavar="PATH",
                   help="列表快照（默认：<root>/.state/latest_list.json）")
    p.add_argument("--out", metavar="PATH",
                   help="选稿输出（默认：<root>/.state/selection.json）")
    p.add_argument("--author", metavar="NAME_OR_ID",
                   help="按作者过滤；数字解释为该作者列表内的局部序号")
    p.add_argument("--force", action="store_true", help="包含已采用文章（用于重做）")
    p.add_argument("--json", action="store_true", help="输出 JSON 到 stdout")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.root).expanduser()
    snapshot_path = (Path(args.snapshot).expanduser() if args.snapshot
                     else root / ".state" / "latest_list.json")
    out_path = (Path(args.out).expanduser() if args.out
                else root / ".state" / "selection.json")

    try:
        snapshot = load_snapshot(snapshot_path)
        payload = build_selection(snapshot, snapshot_path, args)
    except RuntimeError as exc:
        log(f"[错误] {exc}")
        return 3
    except ValueError as exc:
        log(f"[错误] {exc}")
        return 2

    try:
        write_json_atomic(payload, out_path)
    except OSError as exc:
        log(f"[错误] 写选稿文件失败：{exc}")
        return 1

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print_human(payload, out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
