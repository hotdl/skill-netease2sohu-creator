#!/usr/bin/env python3
"""文章状态库（sqlite3）：记录每篇文章的抓取结果与「是否已采用」。

数据库位置：<root>/.state/state.db（root 默认 <当前工作目录>/workspaces）。

表 articles 主要字段：
    article_id    文章 ID（唯一）
    author_id     作者 ID
    author_name   作者昵称
    title         标题
    url           原文链接
    publish_time  发布时间（YYYY-MM-DD HH:MM:SS）
    publish_date  发布日期（yyyyMMdd，用于目录名）
    dir_path      文章目录（相对 root）
    content_len   正文字符数
    image_count   已下载图片数
    state         0=未采用，1=已采用（默认 0，抓取时不会覆盖已有值）
    error         抓取/解析失败原因（成功为 NULL）

命令行用法：
    python state_store.py stats
    python state_store.py list --state 0 --limit 20
    python state_store.py set LAJJ61890556832G 1
    python state_store.py show LAJJ61890556832G --json
    python state_store.py init
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from netease_common import (
    DEFAULT_ROOT,
    STATE_DB_NAME,
    STATE_DIRNAME,
    log,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id    TEXT NOT NULL UNIQUE,
    author_id     TEXT,
    author_name   TEXT,
    title         TEXT,
    url           TEXT NOT NULL,
    publish_time  TEXT,
    publish_date  TEXT,
    dir_path      TEXT,
    content_len   INTEGER NOT NULL DEFAULT 0,
    image_count   INTEGER NOT NULL DEFAULT 0,
    state         INTEGER NOT NULL DEFAULT 0,   -- 0 未采用 / 1 已采用
    error         TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_articles_state ON articles(state);
CREATE INDEX IF NOT EXISTS idx_articles_author ON articles(author_id);
CREATE INDEX IF NOT EXISTS idx_articles_publish ON articles(publish_date);
"""


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def db_path(root: str | Path | None = None) -> Path:
    base = Path(root) if root else DEFAULT_ROOT
    return base / STATE_DIRNAME / STATE_DB_NAME


def connect(root: str | Path | None = None) -> sqlite3.Connection:
    """打开（必要时创建）状态库，并确保表结构存在。"""
    path = db_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def upsert_article(conn: sqlite3.Connection, **fields) -> None:
    """
    新增或更新一篇文章的记录。

    已存在时只更新抓取相关字段，**不会覆盖 state**（人工标记的采用状态必须保留）。
    """
    record = {
        "article_id": fields["article_id"],
        "author_id": fields.get("author_id") or "",
        "author_name": fields.get("author_name") or "",
        "title": fields.get("title") or "",
        "url": fields.get("url") or "",
        "publish_time": fields.get("publish_time") or "",
        "publish_date": fields.get("publish_date") or "",
        "dir_path": fields.get("dir_path") or "",
        "content_len": int(fields.get("content_len") or 0),
        "image_count": int(fields.get("image_count") or 0),
        "error": fields.get("error"),
        "updated_at": now_str(),
    }
    conn.execute(
        """
        INSERT INTO articles (
            article_id, author_id, author_name, title, url,
            publish_time, publish_date, dir_path, content_len, image_count,
            error, state, created_at, updated_at
        ) VALUES (
            :article_id, :author_id, :author_name, :title, :url,
            :publish_time, :publish_date, :dir_path, :content_len, :image_count,
            :error, 0, :created_at, :updated_at
        )
        ON CONFLICT(article_id) DO UPDATE SET
            author_id    = excluded.author_id,
            author_name  = excluded.author_name,
            title        = excluded.title,
            url          = excluded.url,
            publish_time = excluded.publish_time,
            publish_date = excluded.publish_date,
            dir_path     = excluded.dir_path,
            content_len  = excluded.content_len,
            image_count  = excluded.image_count,
            error        = excluded.error,
            updated_at   = excluded.updated_at
        """,
        {**record, "created_at": now_str()},
    )
    conn.commit()


def get_article(conn: sqlite3.Connection, article_id: str) -> sqlite3.Row | None:
    cur = conn.execute("SELECT * FROM articles WHERE article_id = ?", (article_id,))
    return cur.fetchone()


def is_crawled(conn: sqlite3.Connection, article_id: str,
               root: str | Path | None = None) -> bool:
    """是否已有成功抓取记录（有正文目录、无错误）。

    允许 content_len=0：有些文章以图片为主、几乎没文字，但 content.txt
    仍然存在，也应视为已抓取。传入 root 时确认 content.txt 真实存在且非空，
    避免状态库里有记录、正文文件却被删掉时被误判。
    """
    row = get_article(conn, article_id)
    if not row or row["error"] or not row["dir_path"]:
        return False
    if root is not None:
        content = Path(root) / row["dir_path"] / "content.txt"
        try:
            if not content.is_file() or content.stat().st_size == 0:
                return False
        except OSError:
            return False
    return True


def set_state(conn: sqlite3.Connection, article_id: str, state: int) -> bool:
    """标记文章是否已采用，返回是否命中记录。"""
    cur = conn.execute(
        "UPDATE articles SET state = ?, updated_at = ? WHERE article_id = ?",
        (int(state), now_str(), article_id),
    )
    conn.commit()
    return cur.rowcount > 0


def list_articles(conn: sqlite3.Connection, *, state: int | None = None,
                  author_id: str | None = None, limit: int = 0) -> list[sqlite3.Row]:
    sql = "SELECT * FROM articles WHERE 1=1"
    params: list = []
    if state is not None:
        sql += " AND state = ?"
        params.append(int(state))
    if author_id:
        sql += " AND author_id = ?"
        params.append(author_id)
    sql += " ORDER BY publish_time DESC, id DESC"
    if limit and limit > 0:
        sql += " LIMIT ?"
        params.append(int(limit))
    return conn.execute(sql, params).fetchall()


def stats(conn: sqlite3.Connection) -> dict:
    total = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    adopted = conn.execute("SELECT COUNT(*) FROM articles WHERE state = 1").fetchone()[0]
    failed = conn.execute(
        "SELECT COUNT(*) FROM articles WHERE error IS NOT NULL AND error != ''"
    ).fetchone()[0]
    authors = conn.execute("SELECT COUNT(DISTINCT author_id) FROM articles").fetchone()[0]
    return {"total": total, "adopted": adopted, "not_adopted": total - adopted,
            "failed": failed, "authors": authors}


# ------------------------- CLI -------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="查看/维护文章状态库（<root>/.state/state.db）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--root", default=str(DEFAULT_ROOT),
                   help="输出根目录（默认：%(default)s）")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="初始化状态库（已存在则不动）")

    ls = sub.add_parser("list", help="列出文章")
    ls.add_argument("--state", type=int, choices=[0, 1], help="按采用状态过滤：0 未采用 / 1 已采用")
    ls.add_argument("--author-id", help="按作者 ID 过滤")
    ls.add_argument("--limit", type=int, default=20, help="最多显示多少条，0 表示全部（默认：20）")

    st = sub.add_parser("set", help="标记文章是否已采用")
    st.add_argument("article_id", help="文章 ID，如 LAJJ61890556832G")
    st.add_argument("state", type=int, choices=[0, 1], help="0=未采用，1=已采用")

    sh = sub.add_parser("show", help="按文章 ID 查看详情（可输出 JSON）")
    sh.add_argument("article_ids", nargs="+", help="文章 ID，可传多个")
    sh.add_argument("--json", action="store_true", help="输出 JSON")

    sub.add_parser("stats", help="统计各状态数量")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    conn = connect(args.root)

    if args.command == "init":
        log(f"状态库已就绪：{db_path(args.root)}")
        return 0

    if args.command == "stats":
        s = stats(conn)
        log(f"数据库：{db_path(args.root)}")
        print(f"总数：{s['total']}  已采用：{s['adopted']}  未采用：{s['not_adopted']}  "
              f"失败：{s['failed']}  作者数：{s['authors']}")
        return 0

    if args.command == "show":
        found: list[dict] = []
        missing: list[str] = []
        for article_id in args.article_ids:
            row = get_article(conn, article_id)
            if not row:
                missing.append(article_id)
                continue
            found.append({
                "article_id": row["article_id"],
                "author_id": row["author_id"],
                "author_name": row["author_name"],
                "title": row["title"],
                "url": row["url"],
                "publish_time": row["publish_time"],
                "publish_date": row["publish_date"],
                "dir_path": row["dir_path"],
                "dir": (str((Path(args.root) / row["dir_path"]).resolve())
                        if row["dir_path"] else ""),
                "content_len": row["content_len"],
                "image_count": row["image_count"],
                "state": row["state"],
                "error": row["error"] or "",
                "cached": is_crawled(conn, article_id, root=args.root),
            })
        payload = {"db": str(db_path(args.root)), "articles": found, "missing": missing}
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            for item in found:
                adopted = "已采用" if item["state"] == 1 else "未采用"
                cached = "已抓取" if item["cached"] else "未抓取"
                print(f"{item['article_id']}\t{adopted}\t{cached}\t"
                      f"{item['title']}\t{item['dir_path']}")
            if missing:
                log(f"未找到：{', '.join(missing)}")
        return 0 if found else 1

    if args.command == "list":
        rows = list_articles(conn, state=args.state, author_id=args.author_id,
                             limit=args.limit)
        if not rows:
            log("没有符合条件的记录。")
            return 0
        for r in rows:
            flag = "已采用" if r["state"] == 1 else "未采用"
            err = f"  [失败] {r['error']}" if r["error"] else ""
            print(f"{r['article_id']}\t{r['publish_date'] or '----------'}\t{flag}\t"
                  f"{r['author_name']}\t{r['title']}{err}")
        log(f"共 {len(rows)} 条（limit={args.limit}）")
        return 0

    if args.command == "set":
        if set_state(conn, args.article_id, args.state):
            log(f"已将 {args.article_id} 的 state 设为 {args.state}")
            return 0
        log(f"未找到文章：{args.article_id}")
        return 1

    return 2


if __name__ == "__main__":
    sys.exit(main())
