#!/usr/bin/env python3
"""
上传本地目录到微云（通用版）

将本地指定目录「递归收集的所有文件」一一对应地上传到微云指定目录下。

- 本地目录下的相对目录结构会在微云目标目录中按相同层级重建；
  即「本地目录 ↔ 微云目标目录」一一对应，平铺的 docx 直接落到目标目录。
- 上传走 MCP HTTP 接口（默认指向沙箱内的 auth-proxy），无需手动提供 token。
- 每个文件独立调用 weiyun.upload 的「预上传 → 分片上传」流程，512KB 分片。

用法：
    # 将本地 ./output/20261102 下的所有文件直接上传到微云目标目录
    python scripts/upload_to_weiyun.py --out ./output/20261102 --weiyun-path workbuddy/sohu/export/20261102

    # ✅ 推荐：父目录 + 自动时间戳子目录（YYYYMMDD_HHMMSS），避免覆盖历史批次
    python scripts/upload_to_weiyun.py --out ./output/20261102 --parent workbuddy/sohu/netease_export --timestamp

    # 微云目录不存在时自动按路径创建（--weiyun-path 用 / 分隔，从根逐级创建）
    python scripts/upload_to_weiyun.py --out ./output/20261102 --weiyun-path workbuddy/sohu/netease_export/20261102

    # 只上传路径中包含指定子串的文件（如 .docx）
    python scripts/upload_to_weiyun.py --out ./output/20261102 --weiyun-path workbuddy/sohu/export --only .docx
"""

import argparse
import base64
import hashlib
import json
import os
import random
import struct
import sys
import time

try:
    import requests
except ImportError:
    print("错误：需要 requests 库，请执行 pip install requests")
    sys.exit(1)


# ========== 纯 Python SHA1（支持读取内部寄存器状态）==========
# 微云上传协议要求「未经 finalization 的 SHA1 中间状态」，
# 这是 hashlib.sha1 无法提供的，故自带实现。

def _left_rotate(n, b):
    return ((n << b) | (n >> (32 - b))) & 0xFFFFFFFF


class SHA1:
    def __init__(self):
        self.h0, self.h1, self.h2, self.h3, self.h4 = (
            0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476, 0xC3D2E1F0)
        self._message_byte_length = 0
        self._unprocessed = b""

    def update(self, data):
        self._unprocessed += data
        self._message_byte_length += len(data)
        while len(self._unprocessed) >= 64:
            self._process_chunk(self._unprocessed[:64])
            self._unprocessed = self._unprocessed[64:]

    def _process_chunk(self, chunk):
        w = [0] * 80
        for i in range(16):
            w[i] = struct.unpack(">I", chunk[i * 4:(i + 1) * 4])[0]
        for i in range(16, 80):
            w[i] = _left_rotate(w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16], 1)
        a, b, c, d, e = self.h0, self.h1, self.h2, self.h3, self.h4
        for i in range(80):
            if i <= 19:
                f, k = (b & c) | ((~b) & d), 0x5A827999
            elif i <= 39:
                f, k = b ^ c ^ d, 0x6ED9EBA1
            elif i <= 59:
                f, k = (b & c) | (b & d) | (c & d), 0x8F1BBCDC
            else:
                f, k = b ^ c ^ d, 0xCA62C1D6
            temp = (_left_rotate(a, 5) + f + e + k + w[i]) & 0xFFFFFFFF
            e, d, c, b, a = d, c, _left_rotate(b, 30), a, temp
        self.h0 = (self.h0 + a) & 0xFFFFFFFF
        self.h1 = (self.h1 + b) & 0xFFFFFFFF
        self.h2 = (self.h2 + c) & 0xFFFFFFFF
        self.h3 = (self.h3 + d) & 0xFFFFFFFF
        self.h4 = (self.h4 + e) & 0xFFFFFFFF

    def get_state(self):
        assert len(self._unprocessed) == 0
        return b"".join(struct.pack("<I", h)
                        for h in (self.h0, self.h1, self.h2, self.h3, self.h4)).hex()

    def hexdigest(self):
        mbl, unp = self._message_byte_length, self._unprocessed
        unp += b"\x80"
        unp += b"\x00" * ((56 - len(unp) % 64) % 64)
        unp += struct.pack(">Q", mbl * 8)
        tmp = SHA1.__new__(SHA1)
        tmp.h0, tmp.h1, tmp.h2, tmp.h3, tmp.h4 = self.h0, self.h1, self.h2, self.h3, self.h4
        tmp._unprocessed, tmp._message_byte_length = b"", mbl
        while len(unp) >= 64:
            tmp._process_chunk(unp[:64])
            unp = unp[64:]
        return "{:08x}{:08x}{:08x}{:08x}{:08x}".format(
            tmp.h0, tmp.h1, tmp.h2, tmp.h3, tmp.h4)


BLOCK_SIZE = 524288  # 512KB


def calc_upload_params(file_path):
    """计算微云上传所需的 block_sha_list / file_sha / file_md5 / check_sha / check_data"""
    file_size = os.path.getsize(file_path)
    filename = os.path.basename(file_path)
    last_block_size = file_size % BLOCK_SIZE or BLOCK_SIZE
    check_block_size = last_block_size % 128 or 128
    before_block_size = file_size - last_block_size

    block_sha_list = []
    sha1, md5 = SHA1(), hashlib.md5()
    with open(file_path, "rb") as f:
        for _ in range(0, before_block_size, BLOCK_SIZE):
            data = f.read(BLOCK_SIZE)
            sha1.update(data); md5.update(data)
            block_sha_list.append(sha1.get_state())
        between = f.read(last_block_size - check_block_size)
        sha1.update(between); md5.update(between)
        check_sha = sha1.get_state()
        check_bytes = f.read(check_block_size)
        sha1.update(check_bytes); md5.update(check_bytes)
        file_sha = sha1.hexdigest()
        check_data = base64.b64encode(check_bytes).decode()
        block_sha_list.append(file_sha)
    return {
        "filename": filename, "file_size": file_size, "file_sha": file_sha,
        "file_md5": md5.hexdigest(), "block_sha_list": block_sha_list,
        "check_sha": check_sha, "check_data": check_data,
    }


# ========== MCP 调用 ==========
_request_id = 0


def mcp_call(mcp_url, tool_name, arguments, max_retries=3):
    global _request_id
    parsed = None
    for attempt in range(1, max_retries + 1):
        _request_id += 1
        payload = {"jsonrpc": "2.0", "id": _request_id, "method": "tools/call",
                   "params": {"name": tool_name, "arguments": arguments}}
        resp = requests.post(mcp_url, headers={"Content-Type": "application/json"},
                             json=payload, timeout=120)
        resp.raise_for_status()
        result = resp.json()
        content = result.get("result", {}).get("content", [])
        parsed = None
        for item in content:
            if item.get("type") == "text":
                parsed = json.loads(item["text"])
                break
        if parsed is None:
            parsed = result
        if parsed.get("retcode", 0) == 50000 and attempt < max_retries:
            time.sleep(random.uniform(2, 5))
            continue
        return parsed
    return parsed


def resolve_dir_by_path(mcp_url, path, create=True):
    """按 'a/b/c' 路径逐级定位或创建目录，返回最末级 dir_key。"""
    parts = [p for p in path.strip("/").split("/") if p]
    dir_key = None  # None = token 绑定根目录
    for name in parts:
        lst = mcp_call(mcp_url, "weiyun.list",
                       {"limit": 50, **({"dir_key": dir_key} if dir_key else {})})
        found = None
        for d in lst.get("dir_list", []):
            if d.get("dir_name") == name:
                found = d["dir_key"]
                break
        if found is None:
            if not create:
                raise RuntimeError(f"目录不存在: {name}（路径 {path}）")
            rsp = mcp_call(mcp_url, "weiyun.create_dir",
                           {"dir_name": name, **({"pdir_key": dir_key} if dir_key else {})})
            if rsp.get("error"):
                raise RuntimeError(f"创建目录失败 {name}: {rsp['error']}")
            found = rsp["dir_key"]
            print(f"  + 已创建目录 {name} -> {found}")
        dir_key = found
    return dir_key


def resolve_dir_under(mcp_url, parent_key, rel_dir):
    """在 parent_key 目录下，按 rel_dir（可含多级，用 / 或系统分隔符分隔）逐级
    定位或创建子目录，返回最末级目录的 dir_key。用于把本地相对路径的目录结构
    镜像到微云目标目录之下。"""
    parts = [p for p in rel_dir.replace(os.sep, "/").split("/") if p]
    dir_key = parent_key
    for name in parts:
        lst = mcp_call(mcp_url, "weiyun.list", {"limit": 50, "dir_key": dir_key})
        found = None
        for d in lst.get("dir_list", []):
            if d.get("dir_name") == name:
                found = d["dir_key"]
                break
        if found is None:
            rsp = mcp_call(mcp_url, "weiyun.create_dir",
                           {"dir_name": name, "pdir_key": dir_key})
            if rsp.get("error"):
                raise RuntimeError(f"创建目录失败 {name}: {rsp['error']}")
            found = rsp["dir_key"]
            print(f"  + 已创建目录 {name} -> {found}")
        dir_key = found
    return dir_key


# ========== 上传核心 ==========
def upload_file(file_path, mcp_url, pdir_key=None, max_rounds=200):
    file_size = os.path.getsize(file_path)
    filename = os.path.basename(file_path)
    params = calc_upload_params(file_path)
    pre_args = {k: params[k] for k in
                ("filename", "file_size", "file_sha", "file_md5",
                 "block_sha_list", "check_sha", "check_data")}
    if pdir_key:
        pre_args["pdir_key"] = pdir_key
    with open(file_path, "rb") as f:
        file_data = f.read()

    for _ in range(max_rounds):
        pre = mcp_call(mcp_url, "weiyun.upload", pre_args)
        if pre.get("error"):
            raise RuntimeError(f"预上传失败: {pre['error']}")
        if pre.get("file_exist"):
            return {"file_id": pre.get("file_id", ""), "filename": pre.get("filename", filename)}
        ch_list = pre.get("channel_list", [])
        uk, ex = pre.get("upload_key", ""), pre.get("ex", "")
        ch = next((c for c in ch_list if int(c.get("len", 0)) > 0), None)
        if ch is None:
            if int(pre.get("upload_state", 0)) == 2:
                return {"file_id": pre.get("file_id", ""), "filename": pre.get("filename", filename)}
            raise RuntimeError(f"无可上传通道 upload_state={pre.get('upload_state')}")
        off, length, cid = int(ch["offset"]), int(ch["len"]), int(ch["id"])
        actual = min(length, len(file_data) - off)
        chunk_b64 = base64.b64encode(file_data[off:off + actual]).decode()
        cl = [{"id": int(c["id"]), "offset": int(c["offset"]), "len": int(c["len"])} for c in ch_list]
        up = mcp_call(mcp_url, "weiyun.upload", {
            "filename": filename, "file_size": file_size, "file_sha": params["file_sha"],
            "block_sha_list": [], "check_sha": params["check_sha"], "upload_key": uk,
            "channel_list": cl, "channel_id": cid, "ex": ex, "file_data": chunk_b64,
        })
        if up.get("error"):
            raise RuntimeError(f"分片上传失败: {up['error']}")
        if int(up.get("upload_state", 0)) == 2:
            return {"file_id": up.get("file_id", ""), "filename": up.get("filename", filename)}
    raise RuntimeError(f"超过最大轮数 {max_rounds}")


def main():
    ap = argparse.ArgumentParser(description="上传 export/ 成品到微云（逐文件，不打包）",
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True,
                    help="本地待上传目录；递归收集其下所有文件，一一对应上传到微云目标目录"
                         "（保持相对目录结构，平铺文件直接落到目标目录）")
    ap.add_argument("--weiyun-dir", default=None, help="微云目标目录 dir_key（已存在）")
    ap.add_argument("--weiyun-path", default=None,
                    help="微云目标目录路径（用 / 分隔，逐级创建/定位），如 workbuddy/sohu/export/20261009")
    ap.add_argument("--parent", default=None,
                    help="微云父目录路径（配合 --timestamp，在其下自动建时间戳子目录），"
                         "如 workbuddy/sohu/export")
    ap.add_argument("--timestamp", nargs="?", const="%Y%m%d_%H%M%S", default=None,
                    metavar="FMT",
                    help="在目标目录下再建一个时间戳子目录（默认格式 %%Y%%m%%d_%%H%%M%%S，"
                         "如 20261009_000000）；可与 --parent / --weiyun-path 配合")
    ap.add_argument("--only", nargs="*", default=None,
                    help="只上传相对路径包含指定子串的文件，如 --only .docx 或 --only 20261102")
    ap.add_argument("--mcp-url", default=os.environ.get(
        "WEIYUN_MCP_URL", "http://weiyun.agent-gateway.auth-proxy.local/api/v3/mcpserver"))
    args = ap.parse_args()

    # 解析目标路径：--parent 优先（配合时间戳使用），否则用 --weiyun-path
    base_path = args.parent or args.weiyun_path
    if not args.weiyun_dir and not base_path:
        print("错误：需提供 --weiyun-path / --parent（或 --weiyun-dir）指定微云目标目录")
        sys.exit(1)

    if not os.path.isdir(args.out):
        print(f"错误：本地目录不存在: {args.out}")
        sys.exit(1)

    mcp_url = args.mcp_url

    # 拼出最终目标路径：带时间戳时在父目录下追加 YYYYMMDD_HHMMSS 子目录
    if args.timestamp:
        ts = time.strftime(args.timestamp if "%" in args.timestamp else "%Y%m%d_%H%M%S")
        if base_path:
            base_path = f"{base_path.rstrip('/')}/{ts}"
            print(f"目标路径（含时间戳）: {base_path}")
        else:
            print(f"错误：--timestamp 需配合 --parent 或 --weiyun-path 使用")
            sys.exit(1)

    root_key = args.weiyun_dir or resolve_dir_by_path(mcp_url, base_path)
    print(f"微云目标目录 key: {root_key}")

    # 递归收集本地文件，保持相对目录结构（本地目录 ↔ 微云目标目录 一一对应）
    files = []  # (abspath, relpath)
    for dirpath, _dirnames, filenames in os.walk(args.out):
        for fn in filenames:
            ab = os.path.join(dirpath, fn)
            rel = os.path.relpath(ab, args.out)
            files.append((ab, rel))
    files.sort(key=lambda x: x[1])

    if args.only:
        only_set = set(args.only)
        files = [f for f in files if any(s in f[1] for s in only_set)]

    if not files:
        print(f"错误：{args.out} 下没有可上传的文件（可能被 --only 过滤）")
        sys.exit(1)

    print(f"待上传文件数：{len(files)}")
    total_ok = 0
    total = 0
    for ab, rel in files:
        # 文件在微云中的目标目录 = root_key + 相对路径的目录部分（平铺文件直达 root_key）
        rel_dir = os.path.dirname(rel)
        target_key = root_key if not rel_dir or rel_dir == "." else \
            resolve_dir_under(mcp_url, root_key, rel_dir)
        fn = os.path.basename(ab)
        total += 1
        print(f"→ {rel}")
        try:
            upload_file(ab, mcp_url, pdir_key=target_key)
            print(f"  ✓ {fn} ({os.path.getsize(ab)} 字节)")
            total_ok += 1
        except Exception as e:
            print(f"  ✗ {fn} 失败: {e}")
        time.sleep(0.3)

    print(f"\n{'=' * 50}\n上传完成：成功 {total_ok}/{total}")
    if total_ok != total:
        sys.exit(1)


if __name__ == "__main__":
    main()
